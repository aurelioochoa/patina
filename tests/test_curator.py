"""Tests for the interval gate, sweep selection, and curator fork."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import curator  # noqa: E402
import guard  # noqa: E402
import review as review_mod  # noqa: E402

from test_review import MARKED, make_stub, make_transcript, stub_writes  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(guard, "SKILLS_DIR", skills)
    monkeypatch.setattr(guard, "PROJECTS_DIR", projects)
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path / "state" / ".locks")
    monkeypatch.setattr(review_mod, "AUDIT_LOG", tmp_path / "state" / "audit.jsonl")
    monkeypatch.setattr(review_mod, "STATE_FILE", tmp_path / "state" / "state.json")
    monkeypatch.setattr(guard, "PENDING_DIR", tmp_path / "state" / "pending")
    monkeypatch.setattr(guard, "WORK_DIR", tmp_path / "work" / "skills")
    monkeypatch.setattr(guard, "WORK_MEMORY", tmp_path / "work" / "memory")
    monkeypatch.setattr(guard, "APPROVALS_FILE", tmp_path / "state" / "approvals.json")

    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv(guard.SENTINEL, raising=False)
    return type("Env", (), {
        "tmp": tmp_path, "skills": skills, "projects": projects, "bin": bin_dir,
    })


def audit(env) -> list[dict]:
    path = env.tmp / "state" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def add_transcript(env, project: str, name: str, age_minutes: float) -> Path:
    directory = env.projects / project
    directory.mkdir(parents=True, exist_ok=True)
    path = make_transcript(directory / f"{name}.jsonl", f"/home/u/{project}")
    stamp = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(minutes=age_minutes)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def marked_skill(env, name: str) -> Path:
    directory = env.skills / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(MARKED.format(name=name), encoding="utf-8")
    return path


# --- interval gate ---------------------------------------------------------


def test_due_when_never_run(env):
    assert curator.due({})


def test_not_due_within_interval(env):
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
    assert not curator.due({"last_curator_run": recent})


def test_due_after_interval(env):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    assert curator.due({"last_curator_run": old})


def test_paused_is_never_due(env):
    assert not curator.due({"paused": True})


def test_corrupt_timestamp_is_treated_as_due(env):
    assert curator.due({"last_curator_run": "not a date"})


def test_naive_timestamp_does_not_raise(env):
    naive = dt.datetime.now().replace(tzinfo=None).isoformat()
    assert curator.due({"last_curator_run": naive}) is False


def test_custom_interval_respected(env):
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    assert curator.due({"last_curator_run": recent, "interval_hours": 1})


# --- sweep selection -------------------------------------------------------


def test_sweep_finds_unreviewed_transcript(env):
    add_transcript(env, "proj", "sess-a", age_minutes=60)
    assert [p.stem for p in curator.stale_transcripts({})] == ["sess-a"]


def test_sweep_skips_already_reviewed(env):
    path = add_transcript(env, "proj", "sess-a", age_minutes=60)
    state = {"watermarks": {"sess-a": path.stat().st_mtime}}
    assert curator.stale_transcripts(state) == []


def test_sweep_reviews_again_when_transcript_grew(env):
    path = add_transcript(env, "proj", "sess-a", age_minutes=60)
    state = {"watermarks": {"sess-a": path.stat().st_mtime - 100}}
    assert [p.stem for p in curator.stale_transcripts(state)] == ["sess-a"]


def test_sweep_skips_still_active_session(env):
    """A transcript touched minutes ago is a live session, not a missed one."""
    add_transcript(env, "proj", "live", age_minutes=2)
    assert curator.stale_transcripts({}) == []


def test_sweep_skips_ancient_transcripts(env):
    add_transcript(env, "proj", "ancient", age_minutes=60 * 24 * 30)
    assert curator.stale_transcripts({}) == []


def test_sweep_cap_reports_what_it_dropped(env):
    for i in range(12):
        add_transcript(env, "proj", f"sess-{i:02d}", age_minutes=60 + i)
    make_stub(env.bin, "echo 'Nothing to save.'")
    curator.sweep({}, limit=3)
    capped = [e for e in audit(env) if e["event"] == "sweep-capped"][0]
    assert capped["pending"] == 12
    assert len(capped["deferred"]) == 9


def test_sweep_advances_watermarks(env):
    add_transcript(env, "proj", "sess-a", age_minutes=60)
    make_stub(env.bin, "echo 'Nothing to save.'")
    curator.sweep({})
    assert "sess-a" in review_mod.read_state().get("watermarks", {})


def test_transcript_cwd_extracted(env):
    path = add_transcript(env, "proj", "sess-a", age_minutes=60)
    assert curator._transcript_cwd(path) == "/home/u/proj"


def test_transcript_cwd_missing_file(env):
    assert curator._transcript_cwd(env.tmp / "nope.jsonl") is None


# --- curate ----------------------------------------------------------------


def test_curate_skips_empty_library(env):
    make_stub(env.bin, "echo 'should not run'")
    curator.curate()
    assert any(e.get("skipped") == "empty library" for e in audit(env))


def test_curate_runs_with_marked_skills(env):
    marked_skill(env, "learned")
    make_stub(env.bin, "echo 'Library is healthy'")
    curator.curate()
    entry = [e for e in audit(env) if e["event"] == "curate"][0]
    assert entry["exit"] == 0
    assert "healthy" in entry["reply"]


def test_curate_reverts_out_of_allowlist_writes(env):
    marked_skill(env, "learned")
    guard.ensure_skills_repo()
    guard.commit("baseline")
    make_stub(env.bin, stub_writes(
        env.skills / "invented" / "SKILL.md",
        "---\nname: invented\ndescription: no marker\n---\nbody",
        "made something",
    ))
    curator.curate()
    assert not (env.skills / "invented" / "SKILL.md").exists()


def test_curate_inventory_lists_size_and_age(env):
    marked_skill(env, "learned")
    text = curator.inventory()
    assert "learned" in text and "chars" in text and "since edit" in text


def test_curate_inventory_lists_support_files(env):
    path = marked_skill(env, "learned")
    references = path.parent / "references"
    references.mkdir()
    (references / "notes.md").write_text("notes", encoding="utf-8")
    assert "references/notes.md" in curator.inventory()


def test_curate_prompt_has_no_placeholders(env):
    marked_skill(env, "learned")
    prompt = curator.build_prompt()
    assert "{inventory}" not in prompt and "{skills_dir}" not in prompt


def test_curate_fork_carries_recursion_guards(env, monkeypatch):
    marked_skill(env, "learned")
    captured = {}
    real_run = subprocess.run

    def spy(command, **kwargs):
        if command and command[0] == "claude":
            captured["command"] = command
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(curator.subprocess, "run", spy)
    curator.curate()
    command = captured["command"]
    assert json.loads(command[command.index("--settings") + 1])["hooks"]["disableAllHooks"]
    assert captured["env"][guard.SENTINEL] == "1"


# --- run / hook mode -------------------------------------------------------


def test_run_stamps_state(env):
    make_stub(env.bin, "echo ok")
    curator.run()
    state = review_mod.read_state()
    assert state["last_curator_run"]
    assert state["run_count"] == 1


def test_run_preserves_sweep_watermarks(env):
    """Regression: stamping the run must not clobber watermarks the sweep set."""
    add_transcript(env, "proj", "sess-a", age_minutes=60)
    make_stub(env.bin, "echo 'Nothing to save.'")
    curator.run()
    assert "sess-a" in review_mod.read_state().get("watermarks", {})


def test_run_defers_when_locked(env):
    make_stub(env.bin, "echo ok")
    with guard.lock("curator"):
        curator.run()
    assert any(e["event"] == "curate-deferred" for e in audit(env))


def test_check_forks_when_due(env, monkeypatch):
    spawned = []
    monkeypatch.setattr(curator, "spawn_detached", lambda: spawned.append(True))
    monkeypatch.setattr(sys, "argv", ["curator.py", "--check"])
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: "{}"})())
    curator.main()
    assert spawned


def test_check_does_not_fork_when_recent(env, monkeypatch):
    review_mod.write_state(
        {"last_curator_run": dt.datetime.now(dt.timezone.utc).isoformat()}
    )
    spawned = []
    monkeypatch.setattr(curator, "spawn_detached", lambda: spawned.append(True))
    monkeypatch.setattr(sys, "argv", ["curator.py", "--check"])
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: "{}"})())
    curator.main()
    assert not spawned


def test_check_exits_immediately_in_child(env, monkeypatch):
    monkeypatch.setenv(guard.SENTINEL, "1")
    spawned = []
    monkeypatch.setattr(curator, "spawn_detached", lambda: spawned.append(True))
    monkeypatch.setattr(sys, "argv", ["curator.py", "--check"])
    assert curator.main() == 0
    assert not spawned


def test_pause_and_resume(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["curator.py", "--pause"])
    curator.main()
    assert review_mod.read_state()["paused"] is True
    monkeypatch.setattr(sys, "argv", ["curator.py", "--resume"])
    curator.main()
    assert review_mod.read_state()["paused"] is False


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


# --- curator quarantine ----------------------------------------------------


def test_curator_changes_are_queued_not_applied(env):
    """Consolidation is the most consequential thing this system does."""
    import pending

    marked_skill(env, "learned")
    original = (env.skills / "learned" / "SKILL.md").read_text(encoding="utf-8")
    make_stub(env.bin, f"echo 'curated note' >> '{guard.WORK_DIR}/learned/SKILL.md'; echo consolidated")
    curator.curate()

    assert (env.skills / "learned" / "SKILL.md").read_text(encoding="utf-8") == original
    queue = pending.entries()
    assert len(queue) == 1 and queue[0]["kind"] == "patch"
    assert queue[0]["session"].startswith("curator-")


def test_curator_cannot_touch_hand_written_skills(env):
    import pending

    handwritten = env.skills / "mine"
    handwritten.mkdir()
    from test_review import UNMARKED
    original = UNMARKED.format(name="mine")
    (handwritten / "SKILL.md").write_text(original, encoding="utf-8")
    marked_skill(env, "learned")

    make_stub(env.bin, f"echo 'MEDDLING' >> '{guard.WORK_DIR}/mine/SKILL.md'; echo done")
    curator.curate()

    assert (handwritten / "SKILL.md").read_text(encoding="utf-8") == original
    assert pending.entries() == []
