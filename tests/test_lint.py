"""Tests for the mechanical skill checks.

These rules exist because a model asked to notice that a description is 1,400
characters long will sometimes notice. Every rule here is one a ``len()`` can
decide, so it is decided by a ``len()``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lint  # noqa: E402


def messages(findings, severity=None):
    return [m for s, m in findings if severity is None or s == severity]


# --- frontmatter, the blocking tier ----------------------------------------


@pytest.mark.parametrize(
    "frontmatter,expected",
    [
        ({"description": "x" * 60}, "no name"),
        ({"name": "Has-Capitals", "description": "x" * 60}, "lowercase"),
        ({"name": "with spaces", "description": "x" * 60}, "lowercase"),
        ({"name": "a" * 65, "description": "x" * 60}, "max 64"),
        ({"name": "claude-helper", "description": "x" * 60}, "reserved"),
        ({"name": "anthropic-tools", "description": "x" * 60}, "reserved"),
        ({"name": "fine"}, "no description"),
        ({"name": "fine", "description": "x" * 1025}, "max 1024"),
    ],
)
def test_malformed_frontmatter_blocks(frontmatter, expected):
    findings = lint.check_frontmatter(frontmatter)
    assert any(expected in m for m in messages(findings, lint.BLOCK))


def test_good_frontmatter_is_clean():
    findings = lint.check_frontmatter({
        "name": "reviewing-migrations",
        "description": "Reviews database migrations for lock risk. Use when a "
                       "migration adds a column, index, or constraint.",
    })
    assert findings == []


def test_first_person_description_warns_but_does_not_block():
    findings = lint.check_frontmatter({
        "name": "helper",
        "description": "I can help you review database migrations for lock risk.",
    })
    assert not lint.blocking(findings)
    assert any("person" in m for m in messages(findings, lint.WARN))


def test_very_short_description_warns():
    findings = lint.check_frontmatter({"name": "helper", "description": "Does stuff."})
    assert any("short" in m for m in messages(findings, lint.WARN))


# --- size and structure ----------------------------------------------------


def test_overlong_body_warns():
    findings = lint.check_body("line\n" * 501)
    assert any("501 lines" in m for m in messages(findings))


def test_body_at_the_limit_is_clean():
    assert lint.check_body("line\n" * 500) == []


def test_long_reference_without_a_toc_warns():
    assert messages(lint.check_reference("line\n" * 200))


def test_long_reference_with_a_toc_is_clean():
    assert lint.check_reference("# Ref\n\n## Contents\n- a\n" + "line\n" * 200) == []


def test_short_reference_needs_no_toc():
    assert lint.check_reference("line\n" * 50) == []


def test_nested_reference_link_warns():
    findings = lint.check_link_depth("skill/references/a.md", "See [more](b.md).")
    assert any("one level deep" in m for m in messages(findings))


def test_links_from_skill_md_are_fine():
    """SKILL.md pointing at references/ is the pattern, not the problem."""
    assert lint.check_link_depth("SKILL.md", "See [more](references/b.md).") == []


def test_external_links_are_not_nesting():
    findings = lint.check_link_depth(
        "skill/references/a.md", "See [docs](https://example.com/x.md)."
    )
    assert findings == []


# --- duplicate triggers ----------------------------------------------------


def test_near_duplicate_description_warns():
    findings = lint.check_duplicate_trigger(
        "Reviews database migrations for lock risk before deploy.",
        {"migrations": "Reviews database migrations for lock risk before deploy."},
    )
    assert any("migrations" in m for m in messages(findings))


def test_unrelated_description_is_clean():
    findings = lint.check_duplicate_trigger(
        "Reviews database migrations for lock risk.",
        {"charts": "Builds accessible bar charts from a dataframe."},
    )
    assert findings == []


def test_stopwords_alone_do_not_trigger_a_duplicate():
    """'Use when the user asks' is in every description ever written."""
    findings = lint.check_duplicate_trigger(
        "Use when the user asks for it.",
        {"other": "Use when the user asks for anything at all."},
    )
    assert findings == []


# --- large deletions -------------------------------------------------------


def test_patch_that_guts_a_skill_warns():
    findings = lint.check_deletion("line\n" * 40, "line\n" * 10)
    assert any("30 of 40" in m for m in messages(findings))


def test_ordinary_edit_does_not_warn():
    assert lint.check_deletion("line\n" * 40, "line\n" * 38) == []


def test_tiny_files_are_exempt():
    """Losing three lines of a five-line file is not a rewrite worth flagging."""
    assert lint.check_deletion("a\nb\nc\nd\ne\n", "a\nb\n") == []


# --- helpers ---------------------------------------------------------------


def test_blocking_filters_to_the_blocking_tier():
    findings = [(lint.BLOCK, "bad"), (lint.WARN, "iffy")]
    assert lint.blocking(findings) == ["bad"]


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
