"""Mechanical checks on a proposed skill.

Everything here is a rule that can be decided by looking at the text, which is
why it is not left to the weekly curator pass: a model asked to notice that a
description is 1,400 characters long will sometimes notice. A ``len()`` always
does, costs nothing, and runs at the moment the change is queued rather than up
to a week later.

The rules come from Anthropic's skill-authoring guidance. Two tiers:

- ``BLOCK`` -- the skill would be rejected by the harness or would fail to load.
  Approving one puts a file in the library that does nothing except take up
  room in the system prompt.
- ``WARN`` -- the skill loads but is likely to misfire: too long to be worth
  reading, a description that will not match anything, a reference file the
  model will only ever half-read.

Pure functions over text. No I/O, no guard import, so this stays testable in
isolation and callable from anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

BLOCK = "block"
WARN = "warn"

Finding = Tuple[str, str]

#: Frontmatter limits the harness enforces.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
_NAME_RE = re.compile(r"^[a-z0-9-]+$")

#: Reserved in skill names.
RESERVED_WORDS = ("anthropic", "claude")

#: Past this a SKILL.md stops being an overview and starts being the reference
#: material it should be pointing at.
MAX_BODY_LINES = 500

#: A reference file longer than this needs a table of contents: a partial read
#: of the top is a common way for it to be consumed, and without one that read
#: shows none of what the file contains.
TOC_REQUIRED_LINES = 100

#: A description written as speech to the user rather than about the skill.
#: The description is injected into the system prompt, where a mix of voices
#: measurably hurts selection.
_FIRST_OR_SECOND_PERSON = re.compile(
    r"\b(i can|i will|i'll|you can use|you should use|use me|helps you)\b",
    re.IGNORECASE,
)

#: Markdown links to other files, used for the nesting-depth check.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: Words too common to count as evidence that two descriptions overlap.
_STOPWORDS = {
    "use", "when", "the", "a", "an", "and", "or", "for", "to", "of", "in", "on",
    "with", "this", "that", "it", "is", "are", "be", "by", "from", "as", "at",
    "skill", "user", "claude", "asks", "any", "all", "into", "after", "before",
}


def _tokens(text: str) -> set:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def check_frontmatter(frontmatter: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()

    if not name:
        findings.append((BLOCK, "no name in frontmatter"))
    else:
        if len(name) > MAX_NAME_CHARS:
            findings.append((BLOCK, f"name is {len(name)} chars, max {MAX_NAME_CHARS}"))
        if not _NAME_RE.match(name):
            findings.append(
                (BLOCK, f"name {name!r} must be lowercase letters, numbers and hyphens")
            )
        for word in RESERVED_WORDS:
            if word in name.lower():
                findings.append((BLOCK, f"name contains the reserved word {word!r}"))

    if not description:
        findings.append((BLOCK, "no description — the skill can never be selected"))
    else:
        if len(description) > MAX_DESCRIPTION_CHARS:
            findings.append(
                (BLOCK, f"description is {len(description)} chars, "
                        f"max {MAX_DESCRIPTION_CHARS}")
            )
        if _FIRST_OR_SECOND_PERSON.search(description):
            findings.append(
                (WARN, "description is written in first or second person; it is "
                       "injected into the system prompt and should describe the "
                       "skill, not address the reader")
            )
        if len(description) < 40:
            findings.append(
                (WARN, "description is very short — it needs both what the skill "
                       "does and when to use it")
            )
    return findings


def check_body(body: str) -> List[Finding]:
    findings: List[Finding] = []
    lines = body.splitlines()
    if len(lines) > MAX_BODY_LINES:
        findings.append(
            (WARN, f"SKILL.md body is {len(lines)} lines, past the {MAX_BODY_LINES} "
                   "where it should be pointing at references/ instead")
        )
    return findings


def check_reference(text: str) -> List[Finding]:
    lines = text.splitlines()
    if len(lines) <= TOC_REQUIRED_LINES:
        return []
    head = "\n".join(lines[:30]).lower()
    if "## contents" in head or "## table of contents" in head:
        return []
    return [(
        WARN,
        f"reference file is {len(lines)} lines with no table of contents; a "
        "partial read of the top will not show what is in it",
    )]


def check_link_depth(relative_path: str, text: str) -> List[Finding]:
    """Links in a reference file that point at yet another file.

    Nested references get partially read -- the model previews rather than
    reads -- so the content at depth two is the content nobody sees.
    """
    if relative_path.count("/") == 0:
        return []
    findings = []
    for target in _LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if target.endswith(".md"):
            findings.append((
                WARN,
                f"{relative_path} links to {target}; keep references one level "
                "deep from SKILL.md so they are read whole",
            ))
            break
    return findings


def check_duplicate_trigger(
    description: str, others: Dict[str, str], threshold: float = 0.7
) -> List[Finding]:
    """Does this description already describe a skill in the library?

    Two skills competing for the same trigger is the failure mode an accreting
    library drifts into, and the point to catch it is before the second one is
    approved -- not a week later when the curator has to merge them.
    """
    mine = _tokens(description)
    if len(mine) < 3:
        return []
    for name, other in others.items():
        theirs = _tokens(other)
        if not theirs:
            continue
        overlap = len(mine & theirs) / len(mine)
        if overlap >= threshold:
            return [(
                WARN,
                f"description overlaps heavily with the existing skill {name!r} "
                f"({int(overlap * 100)}% of its terms); these will compete for "
                "the same trigger",
            )]
    return []


def check_deletion(before: str, after: str, ratio: float = 0.5) -> List[Finding]:
    """A patch that removes most of what was there.

    Legitimate during consolidation, alarming otherwise, and always worth
    reading closely -- so this warns rather than blocks.
    """
    old_lines = len(before.splitlines())
    if old_lines < 10:
        return []
    removed = old_lines - len(after.splitlines())
    if removed / old_lines >= ratio:
        return [(
            WARN,
            f"this patch removes {removed} of {old_lines} lines — read it as a "
            "rewrite, not an edit",
        )]
    return []


def blocking(findings: List[Finding]) -> List[str]:
    return [message for severity, message in findings if severity == BLOCK]


def format_findings(findings: List[Finding]) -> str:
    return "\n".join(f"  [{severity}] {message}" for severity, message in findings)
