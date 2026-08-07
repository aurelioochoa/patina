"""Tests for the safety kernel.

The write-confinement tests are the important ones. Everything else in this
project failing means the loop does not learn; these failing means it can
corrupt skills the user wrote by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import guard  # noqa: E402

MARKED = """---
name: marked
description: A skill the loop owns.
metadata:
  autoManaged: true
---

Body.
"""

UNMARKED = """---
name: unmarked
description: A skill the user wrote by hand.
---

Body.
"""

NESTED = """---
name: nested
description: Marker one level deeper.
metadata:
  selfImprove:
    autoManaged: true
---

Body.
"""


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """A throwaway skills tree wired into guard's module-level constants."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(guard, "SKILLS_DIR", root)
    monkeypatch.setattr(guard, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path / "state" / ".locks")
    return root


def make_skill(root: Path, name: str, content: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


# --- recursion guard -------------------------------------------------------


def test_sentinel_detected(monkeypatch):
    monkeypatch.delenv(guard.SENTINEL, raising=False)
    assert not guard.is_child()
    monkeypatch.setenv(guard.SENTINEL, "1")
    assert guard.is_child()


def test_child_env_sets_sentinel():
    assert guard.child_env()[guard.SENTINEL] == "1"


def test_child_settings_disables_hooks():
    import json

    assert json.loads(guard.CHILD_SETTINGS)["hooks"]["disableAllHooks"] is True


# --- frontmatter -----------------------------------------------------------


def test_parse_frontmatter_absent():
    assert guard.parse_frontmatter("no frontmatter here") == {}


def test_marker_top_level():
    assert guard.is_auto_managed({"autoManaged": True})


def test_marker_under_metadata():
    assert guard.is_auto_managed(guard.parse_frontmatter(MARKED))


def test_marker_two_levels_deep():
    assert guard.is_auto_managed(guard.parse_frontmatter(NESTED))


def test_unmarked_is_not_auto_managed():
    assert not guard.is_auto_managed(guard.parse_frontmatter(UNMARKED))


def test_minimal_parser_matches_on_marker():
    """The fallback path must reach the same verdict as PyYAML."""
    parsed = guard._parse_yaml_minimal(MARKED.split("---")[1])
    assert guard.is_auto_managed(parsed)


def test_string_true_accepted():
    assert guard.is_auto_managed({"metadata": {"autoManaged": "true"}})


# --- project slug ----------------------------------------------------------


@pytest.mark.parametrize(
    "cwd,expected",
    [
        ("/home/aurelio", "-home-aurelio"),
        ("/home/aurelio/Repos/kidtopiaplay.com", "-home-aurelio-Repos-kidtopiaplay-com"),
        ("/tmp/claude-1000/-home-aurelio/x", "-tmp-claude-1000--home-aurelio-x"),
    ],
)
def test_project_slug_matches_observed_names(cwd, expected):
    assert guard.project_slug(cwd) == expected


# --- write surface ---------------------------------------------------------


def test_writable_skills_only_returns_marked(skills):
    make_skill(skills, "marked", MARKED)
    make_skill(skills, "unmarked", UNMARKED)
    names = [p.parent.name for p in guard.writable_skills()]
    assert names == ["marked"]


def test_writable_skills_empty_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "SKILLS_DIR", tmp_path / "absent")
    assert guard.writable_skills() == []


def test_new_skill_directory_is_allowed(skills):
    """Creating a skill is the point -- a path with no SKILL.md yet passes."""
    assert not guard.violates_allowlist(skills / "brand-new" / "SKILL.md", "/home/u")


def test_existing_unmarked_skill_is_a_violation(skills):
    make_skill(skills, "unmarked", UNMARKED)
    assert guard.violates_allowlist(skills / "unmarked" / "SKILL.md", "/home/u")


def test_existing_marked_skill_is_allowed(skills):
    make_skill(skills, "marked", MARKED)
    assert not guard.violates_allowlist(skills / "marked" / "SKILL.md", "/home/u")


def test_reference_file_under_marked_skill_allowed(skills):
    make_skill(skills, "marked", MARKED)
    assert not guard.violates_allowlist(
        skills / "marked" / "references" / "notes.md", "/home/u"
    )


def test_reference_file_under_unmarked_skill_is_a_violation(skills):
    make_skill(skills, "unmarked", UNMARKED)
    assert guard.violates_allowlist(
        skills / "unmarked" / "references" / "notes.md", "/home/u"
    )


def test_path_outside_tree_is_a_violation(skills, tmp_path):
    assert guard.violates_allowlist(tmp_path / "elsewhere" / "evil.md", "/home/u")


def test_memory_dir_for_active_project_allowed(skills, tmp_path):
    memory = guard.memory_dir("/home/u/proj")
    memory.mkdir(parents=True)
    assert not guard.violates_allowlist(memory / "fact.md", "/home/u/proj")


def test_other_projects_memory_is_a_violation(skills, tmp_path):
    other = guard.memory_dir("/home/u/other")
    other.mkdir(parents=True)
    assert guard.violates_allowlist(other / "fact.md", "/home/u/proj")


# --- git audit repo --------------------------------------------------------


@pytest.fixture
def repo(skills, monkeypatch):
    guard.ensure_skills_repo()
    return skills


def test_ensure_repo_is_idempotent(repo):
    first = guard.head_sha()
    guard.ensure_skills_repo()
    assert guard.head_sha() == first


def test_verify_writes_reverts_new_unmarked_skill(repo):
    """The fork creating a skill without the marker must not survive."""
    make_skill(repo, "sneaky", UNMARKED)
    violations = guard.verify_writes("/home/u")
    assert len(violations) == 1
    assert not (repo / "sneaky" / "SKILL.md").exists()


def test_verify_writes_reverts_edit_to_unmarked_skill(repo):
    path = make_skill(repo, "handwritten", UNMARKED)
    guard.commit("baseline")
    path.write_text(UNMARKED + "\nCORRUPTED BY THE LOOP\n", encoding="utf-8")
    violations = guard.verify_writes("/home/u")
    assert len(violations) == 1
    assert "CORRUPTED" not in path.read_text(encoding="utf-8")


def test_verify_writes_keeps_marked_skill_edit(repo):
    path = make_skill(repo, "marked", MARKED)
    guard.commit("baseline")
    path.write_text(MARKED + "\nA genuine lesson.\n", encoding="utf-8")
    assert guard.verify_writes("/home/u") == []
    assert "genuine lesson" in path.read_text(encoding="utf-8")


def test_verify_writes_allows_new_marked_skill(repo):
    make_skill(repo, "learned", MARKED)
    assert guard.verify_writes("/home/u") == []
    assert (repo / "learned" / "SKILL.md").exists()


def test_commit_returns_none_when_nothing_changed(repo):
    guard.commit("baseline")
    assert guard.commit("nothing to do") is None


def test_commit_returns_sha_on_change(repo):
    make_skill(repo, "learned", MARKED)
    sha = guard.commit("learned something")
    assert sha and len(sha) == 40


def test_git_never_touches_the_real_skills_dir(repo, monkeypatch):
    """Regression: ``_git`` once bound SKILLS_DIR as a default argument.

    A default argument is evaluated at import, so monkeypatching the module
    constant did not redirect it -- the suite silently ran git against the
    user's real ~/.claude/skills. Assert the redirect actually holds.
    """
    calls = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        if args and args[0] == "git":
            calls.append(kwargs.get("cwd"))
        return real_run(args, **kwargs)

    monkeypatch.setattr(guard.subprocess, "run", spy)
    make_skill(repo, "learned", MARKED)
    guard.commit("learned something")
    guard.verify_writes("/home/u")

    assert calls, "expected git invocations"
    for cwd in calls:
        assert str(repo) in str(cwd)
        assert str(guard.HOME / ".claude" / "skills") != str(cwd)


# --- snapshots -------------------------------------------------------------


def test_snapshot_detects_added_and_modified(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "a.md").write_text("one", encoding="utf-8")
    before = guard.snapshot_dir(directory)
    (directory / "a.md").write_text("two", encoding="utf-8")
    (directory / "b.md").write_text("new", encoding="utf-8")
    diff = guard.diff_snapshot(before, guard.snapshot_dir(directory))
    assert [Path(p).name for p in diff["modified"]] == ["a.md"]
    assert [Path(p).name for p in diff["added"]] == ["b.md"]


def test_snapshot_of_missing_dir_is_empty(tmp_path):
    assert guard.snapshot_dir(tmp_path / "absent") == {}


# --- locking ---------------------------------------------------------------


def test_lock_acquired_when_free(skills):
    with guard.lock("target") as acquired:
        assert acquired


def test_lock_denied_while_held(skills):
    with guard.lock("target") as first:
        assert first
        with guard.lock("target", timeout=0.5) as second:
            assert not second


def test_lock_released_after_use(skills):
    with guard.lock("target"):
        pass
    with guard.lock("target", timeout=0.5) as acquired:
        assert acquired


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
