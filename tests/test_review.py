"""Integration tests for the review entry point.

A stub ``claude`` on PATH stands in for the fork, so these run offline and
deterministically. The point is not to test the model -- it is to prove that
whatever the fork writes, the guard has the last word.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import guard  # noqa: E402
import review  # noqa: E402

MARKED = """---
name: {name}
description: Marked skill.
metadata:
  autoManaged: true
---

Body.
"""

UNMARKED = """---
name: {name}
description: Hand-written by the user.
---

Body.
"""


def make_stub(bin_dir: Path, body: str) -> None:
    """Install a fake ``claude`` that runs `body` as shell."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def stub_writes(target: Path, content: str, reply: str = "done") -> str:
    """Shell that writes `content` to `target`.

    Uses a quoted heredoc rather than printf: skill frontmatter opens with
    ``---``, which printf parses as an option and rejects.
    """
    return (
        f"mkdir -p '{target.parent}'\n"
        f"cat > '{target}' <<'SKILL_EOF'\n{content}\nSKILL_EOF\n"
        f"echo '{reply}'"
    )


def make_transcript(path: Path, cwd: str, turns: int = 3) -> Path:
    records = []
    for i in range(turns):
        records.append(
            {
                "type": "user",
                "cwd": cwd,
                "timestamp": "2026-08-07T00:00:00Z",
                "message": {"role": "user", "content": f"do thing {i}"},
            }
        )
        records.append(
            {
                "type": "assistant",
                "cwd": cwd,
                "timestamp": "2026-08-07T00:01:00Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated skills tree, memory tree, state dir, and a stub claude."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(guard, "SKILLS_DIR", skills)
    monkeypatch.setattr(guard, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path / "state" / ".locks")
    monkeypatch.setattr(review, "AUDIT_LOG", tmp_path / "state" / "audit.jsonl")
    monkeypatch.setattr(review, "STATE_FILE", tmp_path / "state" / "state.json")

    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv(guard.SENTINEL, raising=False)
    return type(
        "Env",
        (),
        {
            "tmp": tmp_path,
            "skills": skills,
            "bin": bin_dir,
            "cwd": "/home/u/proj",
            "transcript": make_transcript(tmp_path / "t.jsonl", "/home/u/proj"),
        },
    )


def audit(env) -> list[dict]:
    path = env.tmp / "state" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- the guard has the last word -------------------------------------------


def test_fork_writing_outside_allowlist_is_reverted(env):
    """The fork creates an unmarked skill. It must not survive."""
    make_stub(
        env.bin,
        stub_writes(
            env.skills / "sneaky" / "SKILL.md",
            UNMARKED.format(name="sneaky"),
            "created a skill",
        ),
    )
    review.review(env.transcript, "sess-1", env.cwd)

    assert not (env.skills / "sneaky" / "SKILL.md").exists()
    entry = [e for e in audit(env) if e["event"] == "review"][0]
    assert entry["violations"], "violation should be recorded, not silently swallowed"


def test_fork_editing_unmarked_skill_is_reverted(env):
    handwritten = env.skills / "handwritten"
    handwritten.mkdir()
    original = UNMARKED.format(name="handwritten")
    (handwritten / "SKILL.md").write_text(original, encoding="utf-8")
    guard.ensure_skills_repo()
    guard.commit("baseline")

    make_stub(env.bin, f"echo 'CORRUPTED' >> '{handwritten}/SKILL.md'; echo done")
    review.review(env.transcript, "sess-2", env.cwd)

    assert (handwritten / "SKILL.md").read_text(encoding="utf-8") == original


def test_fork_writing_marked_skill_survives(env):
    marked = env.skills / "learned"
    marked.mkdir()
    (marked / "SKILL.md").write_text(MARKED.format(name="learned"), encoding="utf-8")
    guard.ensure_skills_repo()
    guard.commit("baseline")

    make_stub(env.bin, f"echo 'A genuine lesson.' >> '{marked}/SKILL.md'; echo saved")
    review.review(env.transcript, "sess-3", env.cwd)

    assert "genuine lesson" in (marked / "SKILL.md").read_text(encoding="utf-8")
    entry = [e for e in audit(env) if e["event"] == "review"][0]
    assert not entry["violations"]
    assert entry["commit"], "a surviving write should be committed"


def test_new_marked_skill_survives(env):
    make_stub(
        env.bin,
        stub_writes(
            env.skills / "new-thing" / "SKILL.md",
            MARKED.format(name="new-thing"),
            "created",
        ),
    )
    review.review(env.transcript, "sess-4", env.cwd)
    assert (env.skills / "new-thing" / "SKILL.md").exists()


# --- resilience ------------------------------------------------------------


def test_missing_claude_binary_does_not_raise(env):
    """PATH keeps git — only ``claude`` goes missing.

    Emptying PATH entirely would remove git too and test the wrong thing.
    """
    git_dir = Path(subprocess.check_output(["which", "git"], text=True).strip()).parent
    os.environ["PATH"] = str(git_dir)
    assert review.review(env.transcript, "sess-5", env.cwd) == 0
    assert any(e.get("reason") == "claude binary not found" for e in audit(env))


def test_fork_failure_is_logged_not_raised(env):
    make_stub(env.bin, "echo 'boom' >&2; exit 1")
    assert review.review(env.transcript, "sess-6", env.cwd) == 0
    entry = [e for e in audit(env) if e["event"] == "review"][0]
    assert entry["exit"] == 1
    assert "boom" in entry["stderr"]


def test_timeout_is_logged_not_raised(env, monkeypatch):
    monkeypatch.setattr(review, "TIMEOUT_SECONDS", 1)
    make_stub(env.bin, "sleep 30")
    assert review.review(env.transcript, "sess-7", env.cwd) == 0
    assert any(e["event"] == "timeout" for e in audit(env))


def test_empty_transcript_is_skipped(env):
    empty = env.tmp / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    make_stub(env.bin, "echo 'should not run'")
    review.review(empty, "sess-8", env.cwd)
    assert any(e["event"] == "skipped" for e in audit(env))


def test_lock_contention_defers(env):
    make_stub(env.bin, "echo ok")
    with guard.lock(f"review-{guard.project_slug(env.cwd)}"):
        review.review(env.transcript, "sess-9", env.cwd)
    assert any(e["event"] == "deferred" for e in audit(env))


# --- recursion guard -------------------------------------------------------


def test_child_sentinel_short_circuits_main(env, monkeypatch):
    monkeypatch.setenv(guard.SENTINEL, "1")
    monkeypatch.setattr(sys, "argv", ["review.py"])
    make_stub(env.bin, "echo 'should not run'")
    assert review.main() == 0
    assert audit(env) == []


def test_fork_command_carries_recursion_guards(env, monkeypatch):
    captured = {}

    real_run = subprocess.run

    def spy(command, **kwargs):
        # guard and review share the same subprocess module object, so this spy
        # sees git calls too. Capture only the fork, and let git run for real.
        if command and command[0] == "claude":
            captured["command"] = command
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(review.subprocess, "run", spy)
    review.review(env.transcript, "sess-10", env.cwd)

    command = captured["command"]
    settings = command[command.index("--settings") + 1]
    assert json.loads(settings)["hooks"]["disableAllHooks"] is True
    assert captured["env"][guard.SENTINEL] == "1"
    assert "--strict-mcp-config" in command
    assert "Bash" in command  # denied
    assert command[command.index("--model") + 1] == review.MODEL


# --- prompt ----------------------------------------------------------------


def test_dry_run_forks_nothing(env, capsys):
    make_stub(env.bin, "echo 'should not run'")
    review.review(env.transcript, "sess-11", env.cwd, dry_run=True)
    assert "Session digest" in capsys.readouterr().out
    assert audit(env) == []


def test_prompt_has_no_unreplaced_placeholders(env):
    prompt = review.build_prompt("DIGEST", env.cwd, "sess-12")
    for token in ("{memory_dir}", "{skills_dir}", "{writable_skills}",
                  "{session_id}", "{today}", "{digest}"):
        assert token not in prompt


def test_prompt_lists_writable_skills(env):
    marked = env.skills / "learned"
    marked.mkdir()
    (marked / "SKILL.md").write_text(MARKED.format(name="learned"), encoding="utf-8")
    unmarked = env.skills / "handwritten"
    unmarked.mkdir()
    (unmarked / "SKILL.md").write_text(UNMARKED.format(name="handwritten"), encoding="utf-8")

    prompt = review.build_prompt("DIGEST", env.cwd, "sess-13")
    assert "- learned:" in prompt
    assert "- handwritten:" not in prompt


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
