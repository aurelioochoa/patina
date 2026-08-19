"""Autonomous mode: the policy, the trial, and the gate that survives them.

The queue was where this loop died -- 46 proposals, 0 approvals, nothing in the
library. These tests are about the thing that replaces it: a policy that can be
read, a probation that takes things back, and a use-time gate that still fires
for something no person ever read.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import guard  # noqa: E402
import pending  # noqa: E402
import skillgate  # noqa: E402

MARKED = """---
name: {name}
description: A marked skill that does a specific thing worth describing.
metadata:
  autoManaged: true
  createdFrom: sess-0000
---

Body.
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(guard, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path / "state" / ".locks")
    monkeypatch.setattr(guard, "PENDING_DIR", tmp_path / "state" / "pending")
    monkeypatch.setattr(guard, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(guard, "WORK_DIR", tmp_path / "work" / "skills")
    monkeypatch.setattr(guard, "APPROVALS_FILE", tmp_path / "state" / "approvals.json")
    (tmp_path / "skills").mkdir()
    for name in ("AUTONOMOUS", "AUTO_MIN_LESSONS", "AUTO_MAX_PATCH_LINES",
                 "AUTO_TRIAL_DAYS"):
        monkeypatch.delenv(f"PATINA_{name}", raising=False)
        monkeypatch.delenv(f"CLAUDE_SELF_IMPROVE_{name}", raising=False)
    return tmp_path


def claims(n: int, confidence: str = "high") -> list:
    return [
        {"kind": "correction", "claim": f"claim {i}", "evidence": f"quote {i}",
         "confidence": confidence}
        for i in range(n)
    ]


def queue_entry(env, skill="learned", kind=pending.KIND_NEW, n_claims=2,
                confidence="high", diff="", body=None) -> dict:
    """Write a queue entry straight to disk, the shape capture() produces."""
    root = guard.PENDING_DIR / f"{skill}-sess0"
    (root / "after" / skill).mkdir(parents=True)
    (root / "after" / skill / "SKILL.md").write_text(
        body or MARKED.format(name=skill), encoding="utf-8"
    )
    meta = {
        "id": f"{skill}-sess0",
        "skill": skill,
        "kind": kind,
        "files": [f"{skill}/SKILL.md"],
        "session": "sess0",
        "sessions": ["sess0"],
        "created_at": pending.now(),
        "summary": "did a thing",
        "claims": claims(n_claims, confidence),
        "lint": [],
        "diff": diff,
    }
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


# --- the policy ------------------------------------------------------------


def test_a_well_evidenced_proposal_passes(env):
    queue_entry(env)
    assert pending.auto_verdict(pending.entries()[0]) is None


def test_a_rejected_name_outranks_the_policy(env):
    """A standing human decision is not a tiebreaker, it is the answer."""
    queue_entry(env, skill="declined")
    pending.set_approval("declined", "never")
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and "rejected" in reason


def test_a_malformed_skill_never_auto_approves(env):
    """--force exists for the human path and is unreachable from here."""
    entry = queue_entry(env)
    root = guard.PENDING_DIR / entry["id"]
    meta = json.loads((root / "meta.json").read_text())
    meta["lint"] = [["block", "no frontmatter"]]
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and reason.startswith("malformed")


def test_one_lesson_is_an_anecdote(env):
    queue_entry(env, n_claims=1)
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and "policy wants 2" in reason


def test_medium_confidence_waits_for_a_person(env):
    queue_entry(env, confidence="medium")
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and "not high" in reason


def test_a_patch_that_is_really_a_rewrite_waits(env):
    big = "\n".join(f"+line {i}" for i in range(200))
    queue_entry(env, kind=pending.KIND_PATCH, diff=big)
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and "rewrite, not a patch" in reason


def test_a_small_patch_passes(env):
    small = "\n".join(f"+line {i}" for i in range(10))
    queue_entry(env, kind=pending.KIND_PATCH, diff=small)
    assert pending.auto_verdict(pending.entries()[0]) is None


def test_archiving_an_unused_skill_is_maintenance(env):
    queue_entry(env, skill="stale", kind=pending.KIND_ARCHIVE, n_claims=0)
    assert pending.auto_verdict(pending.entries()[0]) is None


def test_archiving_a_skill_in_use_needs_a_person(env):
    queue_entry(env, skill="popular", kind=pending.KIND_ARCHIVE, n_claims=0)
    pending.write_state({"usage": {"popular": {"count": 4}}})
    reason = pending.auto_verdict(pending.entries()[0])
    assert reason and "in use" in reason


# --- the switch ------------------------------------------------------------


def test_autonomous_is_off_until_someone_turns_it_on(env):
    assert pending.autonomous() is False
    pending.cmd_auto("on")
    assert pending.autonomous() is True
    pending.cmd_auto("off")
    assert pending.autonomous() is False


def test_the_env_var_overrides_the_stored_setting(env, monkeypatch):
    pending.cmd_auto("off")
    monkeypatch.setenv("PATINA_AUTONOMOUS", "1")
    assert pending.autonomous() is True


def test_a_dry_run_applies_nothing(env):
    queue_entry(env)
    outcome = pending.auto_approve_queue(dry_run=True)
    assert outcome["approved"] == ["learned-sess0"]
    assert not (guard.SKILLS_DIR / "learned").exists()
    assert pending.entries(), "the entry must still be queued"


def test_only_restricts_the_pass_to_named_entries(env):
    """A review must not drain the standing backlog as a side effect."""
    queue_entry(env, skill="fresh")
    queue_entry(env, skill="backlog")
    outcome = pending.auto_approve_queue(only=["fresh-sess0"])
    assert outcome["approved"] == ["fresh-sess0"]
    assert [e["id"] for e in pending.entries()] == ["backlog-sess0"]


# --- what an auto-approval leaves behind -----------------------------------


def test_auto_approval_records_auto_not_always(env):
    """`always` silences the use-time gate permanently. Retiring the queue and
    the gate in one step would leave a skill nobody read unguarded."""
    queue_entry(env)
    assert pending.approve("learned-sess0", automatic=True)
    assert pending.approval_for("learned") == "auto"
    assert (guard.SKILLS_DIR / "learned" / "SKILL.md").exists()


def test_human_approval_still_records_always(env):
    queue_entry(env)
    assert pending.approve("learned-sess0")
    assert pending.approval_for("learned") == "always"
    assert "learned" not in (pending.read_state().get("auto_approved") or {})


def test_auto_approval_starts_a_trial(env):
    queue_entry(env)
    pending.approve("learned-sess0", automatic=True)
    assert "learned" in pending.read_state()["auto_approved"]


def test_every_auto_approval_is_a_revertible_commit(env):
    """The whole design rests on this. If the git history is empty, autonomous
    mode has no undo and must not be trusted."""
    queue_entry(env)
    pending.approve("learned-sess0", automatic=True)
    log = guard._git("log", "--oneline").stdout
    assert "auto-approved: learned" in log


# --- the trial takes things back -------------------------------------------


def test_an_unused_trial_expires(env):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    pending.write_state({"auto_approved": {"ignored": {"since": old}}})
    assert pending.expired_trials() == ["ignored"]


def test_a_loaded_skill_passes_its_trial(env):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    pending.write_state({
        "auto_approved": {"useful": {"since": old}},
        "usage": {"useful": {"count": 3}},
    })
    assert pending.expired_trials() == []


def test_a_young_trial_is_left_alone(env):
    pending.write_state({"auto_approved": {"new": {"since": pending.now()}}})
    assert pending.expired_trials() == []


def test_the_curator_archives_what_the_trial_did_not_justify(env):
    import curator

    skill = guard.SKILLS_DIR / "ignored"
    skill.mkdir()
    (skill / "SKILL.md").write_text(MARKED.format(name="ignored"), encoding="utf-8")
    guard.ensure_skills_repo()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    pending.write_state({"auto_approved": {"ignored": {"since": old}}})

    assert curator.retire_expired_trials() == 1
    assert not skill.exists()
    # Archived, not deleted: contents and history intact.
    assert (guard.SKILLS_DIR / pending.ARCHIVE_DIR / "ignored" / "SKILL.md").exists()
    assert "ignored" not in pending.read_state()["auto_approved"]


# --- the gate is the last thing standing -----------------------------------


def test_the_gate_asks_once_for_an_auto_approved_skill(env):
    skill = guard.SKILLS_DIR / "learned"
    skill.mkdir()
    (skill / "SKILL.md").write_text(MARKED.format(name="learned"), encoding="utf-8")
    pending.set_approval("learned", "auto")

    decision, reason = skillgate.decide(
        {"tool_input": {"skill": "learned"}, "session_id": "s1"}
    )
    assert decision == "ask"
    assert "no person has read it" in reason


def test_the_gate_stays_quiet_for_a_human_approved_skill(env):
    skill = guard.SKILLS_DIR / "blessed"
    skill.mkdir()
    (skill / "SKILL.md").write_text(MARKED.format(name="blessed"), encoding="utf-8")
    pending.set_approval("blessed", "always")

    decision, _ = skillgate.decide(
        {"tool_input": {"skill": "blessed"}, "session_id": "s1"}
    )
    assert decision == "allow"


def test_a_session_verdict_still_settles_an_auto_skill(env):
    skill = guard.SKILLS_DIR / "learned"
    skill.mkdir()
    (skill / "SKILL.md").write_text(MARKED.format(name="learned"), encoding="utf-8")
    pending.set_approval("learned", "auto")
    skillgate.set_session_verdict("s1", "learned", "always")

    decision, _ = skillgate.decide(
        {"tool_input": {"skill": "learned"}, "session_id": "s1"}
    )
    assert decision == "allow", "asked once per session, not once per invocation"


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
