"""Tests for the plugin packaging.

None of this is exercised by the loop's own tests: a broken manifest or a hook
path that does not resolve fails at session start, inside the tool it is
breaking. These checks are cheap and catch the drift that matters -- a command
renamed in bin/patina but not in a skill, a hook pointing at a moved file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "patina"

#: Every subcommand bin/patina dispatches. Skills and hooks may only use these.
COMMANDS = {"review", "curator", "pending", "gate", "status"}

#: The one class of grant that may sit outside `patina ...`: reading the
#: transcript directory to find a live session. Path-bound and read-only, so it
#: cannot reach a file the loop would not read anyway. Anything added here
#: widens what a skill can run without asking -- keep it to rules that name
#: their directory, and prefer a permission prompt to a new entry.
READ_ONLY_EXCEPTIONS = {"Bash(ls ~/.claude/projects/*)"}


def read_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def skill_files() -> list:
    return sorted((ROOT / "skills").glob("*/SKILL.md"))


# --- manifest --------------------------------------------------------------


def test_manifest_is_valid_json_with_a_name():
    manifest = read_json(".claude-plugin/plugin.json")
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["description"].strip()
    assert manifest["version"].strip()


def test_plugin_components_are_at_the_root_not_inside_the_manifest_dir():
    """The documented common mistake: skills/ and hooks/ under .claude-plugin/
    are silently not loaded."""
    for name in ("skills", "hooks", "bin"):
        assert (ROOT / name).is_dir()
        assert not (ROOT / ".claude-plugin" / name).exists()


# --- hooks -----------------------------------------------------------------


def test_hooks_cover_the_three_events_the_loop_needs():
    hooks = read_json("hooks/hooks.json")["hooks"]
    assert set(hooks) == {"SessionEnd", "SessionStart", "PreToolUse"}


def test_the_skill_gate_is_synchronous_and_matches_the_skill_tool():
    """Its verdict is the point; an async gate would allow by default."""
    block = read_json("hooks/hooks.json")["hooks"]["PreToolUse"][0]
    assert block["matcher"] == "Skill"
    assert block["hooks"][0].get("async") is not True


def test_the_session_end_review_is_async():
    """It forks a model call. Synchronous, it would hold up every session end."""
    block = read_json("hooks/hooks.json")["hooks"]["SessionEnd"][0]
    assert block["hooks"][0]["async"] is True


@pytest.mark.parametrize("event", ["SessionEnd", "SessionStart", "PreToolUse"])
def test_hook_commands_resolve_through_the_plugin_root(event):
    for block in read_json("hooks/hooks.json")["hooks"][event]:
        for hook in block["hooks"]:
            command = hook["command"]
            assert "${CLAUDE_PLUGIN_ROOT}" in command
            assert "/bin/patina " in command
            verb = command.split("/bin/patina ", 1)[1].split()[0]
            assert verb in COMMANDS, f"{event} calls unknown subcommand {verb!r}"


# --- the dispatcher --------------------------------------------------------


def test_dispatcher_is_executable():
    assert (ROOT / "bin" / "patina").stat().st_mode & 0o111


def test_dispatcher_rejects_an_unknown_command():
    result = subprocess.run(
        [str(ROOT / "bin" / "patina"), "definitely-not-a-command"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unknown command" in result.stderr


def test_dispatcher_with_no_arguments_prints_usage():
    result = subprocess.run(
        [str(ROOT / "bin" / "patina")], capture_output=True, text=True
    )
    assert result.returncode == 64
    assert "usage: patina" in result.stderr


def test_dispatcher_reaches_the_real_scripts(tmp_path):
    """--help proves the path resolution found review.py, without a model call."""
    result = subprocess.run(
        [str(ROOT / "bin" / "patina"), "review", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--transcript" in result.stdout


def test_dispatcher_finds_scripts_in_the_flat_install_layout(tmp_path):
    """install.sh copies src/*.py into one directory with no src/ parent."""
    flat = tmp_path / "patina"
    flat.mkdir()
    for script in (ROOT / "src").glob("*.py"):
        (flat / script.name).write_bytes(script.read_bytes())
    (flat / "prompts").mkdir()
    copy = flat / "patina"
    copy.write_bytes((ROOT / "bin" / "patina").read_bytes())
    copy.chmod(0o755)

    result = subprocess.run(
        [str(copy), "review", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# --- the command skills ----------------------------------------------------


def test_every_skill_exists_and_parses():
    names = {path.parent.name for path in skill_files()}
    assert names == {
        "status",
        "pending",
        "approve",
        "reject",
        "curate",
        "pause",
        "patination",
    }


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skills_cannot_be_invoked_by_the_model(path):
    """These spend money and mutate the skill library. Claude deciding on its
    own that now is a good moment to approve the queue is the failure the queue
    exists to prevent."""
    text = path.read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in text


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skills_declare_a_description(path):
    frontmatter = path.read_text(encoding="utf-8").split("---")[1]
    assert "description:" in frontmatter


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skills_only_pre_approve_patina_commands(path):
    """A Bash grant wider than `patina ...` would hand the turn a general shell.

    READ_ONLY_EXCEPTIONS is the one narrow door out of that rule; everything
    else must name a patina subcommand.
    """
    frontmatter = path.read_text(encoding="utf-8").split("---")[1]
    for line in frontmatter.splitlines():
        if not line.startswith("allowed-tools:"):
            continue
        for rule in line.split(":", 1)[1].split(","):
            rule = rule.strip()
            if rule in READ_ONLY_EXCEPTIONS:
                continue
            assert rule.startswith("Bash(patina "), f"{path.parent.name}: {rule}"
            verb = rule[len("Bash(patina "):].split()[0].rstrip(")")
            assert verb in COMMANDS, f"{path.parent.name} pre-approves {verb!r}"


def test_the_read_only_skills_cannot_write(path=None):
    """status and pending are for looking. Neither may pre-approve a mutation."""
    for name in ("status", "pending"):
        text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---")[1]
        for forbidden in ("approve", "reject", "--pause", "--curate-only"):
            assert forbidden not in frontmatter, f"{name} pre-approves {forbidden}"


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
