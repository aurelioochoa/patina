"""Tests for the PreToolUse backstop.

The payload shape here is not invented -- it was captured from a real hook
firing against Claude Code 2.1.224:

    {"tool_name": "Skill",
     "tool_input": {"skill": "find-docs", "args": "React useEffect"},
     "session_id": "6564a3bc-..."}
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import guard  # noqa: E402
import pending  # noqa: E402
import skillgate  # noqa: E402

from test_review import MARKED, UNMARKED  # noqa: E402

SESSION = "6564a3bc-8e3c-4a18-8506-1f200cb9636f"


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(guard, "SKILLS_DIR", skills)
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "APPROVALS_FILE", tmp_path / "state" / "approvals.json")
    monkeypatch.setattr(guard, "PENDING_DIR", tmp_path / "state" / "pending")
    monkeypatch.setattr(guard, "WORK_DIR", tmp_path / "work" / "skills")
    monkeypatch.setattr(guard, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path / "state" / ".locks")
    return skills


def install(skills: Path, name: str, template: str = MARKED) -> Path:
    directory = skills / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(template.format(name=name), encoding="utf-8")
    return path


def payload(skill: str, session: str = SESSION) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Skill",
        "tool_input": {"skill": skill, "args": ""},
        "session_id": session,
    }


# --- the four states -------------------------------------------------------


def test_unset_asks(env):
    install(env, "learned")
    decision, reason = skillgate.decide(payload("learned"))
    assert decision == "ask"
    assert "self-improvement loop" in reason


def test_always_allows(env):
    install(env, "learned")
    pending.set_approval("learned", "always")
    assert skillgate.decide(payload("learned"))[0] == "allow"


def test_never_denies(env):
    install(env, "learned")
    pending.set_approval("learned", "never")
    decision, reason = skillgate.decide(payload("learned"))
    assert decision == "deny"
    assert "previously declined" in reason


def test_session_scoped_allow(env):
    install(env, "learned")
    skillgate.set_session_verdict(SESSION, "learned", "always")
    assert skillgate.decide(payload("learned"))[0] == "allow"


def test_session_scoped_allow_does_not_leak_to_other_sessions(env):
    install(env, "learned")
    skillgate.set_session_verdict(SESSION, "learned", "always")
    assert skillgate.decide(payload("learned", session="a-different-session"))[0] == "ask"


def test_session_scoped_deny(env):
    install(env, "learned")
    skillgate.set_session_verdict(SESSION, "learned", "never")
    assert skillgate.decide(payload("learned"))[0] == "deny"


def test_global_verdict_beats_session(env):
    install(env, "learned")
    pending.set_approval("learned", "always")
    skillgate.set_session_verdict(SESSION, "learned", "never")
    assert skillgate.decide(payload("learned"))[0] == "allow"


# --- scope -----------------------------------------------------------------


def test_hand_written_skill_is_never_gated(env):
    """The user wrote it. None of our business."""
    install(env, "handwritten", UNMARKED)
    assert skillgate.decide(payload("handwritten"))[0] == "allow"


def test_unknown_skill_is_allowed(env):
    assert skillgate.decide(payload("does-not-exist"))[0] == "allow"


def test_plugin_skill_is_allowed(env):
    assert skillgate.decide(payload("superpowers:brainstorming"))[0] == "allow"


def test_missing_skill_name_is_allowed(env):
    assert skillgate.decide({"tool_name": "Skill", "tool_input": {}})[0] == "allow"


# --- resilience ------------------------------------------------------------


def test_malformed_stdin_allows(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: "not json"})())
    skillgate.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_internal_error_allows_rather_than_blocking(env, monkeypatch, capsys):
    """A bug in the gate must not lock the user out of their own skills."""
    monkeypatch.setattr(skillgate, "decide", lambda p: (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: "{}"})())
    skillgate.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_output_shape_matches_hook_contract(env, monkeypatch, capsys):
    install(env, "learned")
    monkeypatch.setattr(
        sys, "stdin", type("S", (), {"read": lambda self: json.dumps(payload("learned"))})()
    )
    skillgate.main()
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] in ("allow", "deny", "ask")
    assert isinstance(out["permissionDecisionReason"], str)


# --- integration with the queue --------------------------------------------


def test_approving_via_queue_makes_the_gate_allow(env):
    """Approve once in the review queue; the gate stops asking."""
    install(env, "learned")
    assert skillgate.decide(payload("learned"))[0] == "ask"
    pending.set_approval("learned", "always")
    assert skillgate.decide(payload("learned"))[0] == "allow"


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
