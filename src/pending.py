#!/usr/bin/env python3
"""Quarantine queue — nothing reaches the live skill library unreviewed.

The review fork writes into a scratch copy of the library (``guard.WORK_DIR``),
never the real one. Afterwards :func:`capture` diffs the scratch tree against
the live tree and files every difference here as a pending entry. The live tree
is untouched until :func:`approve` moves an entry in.

This is quarantine as a property of what the fork can *reach*, not a check run
after the fact. It matters for a reason that is easy to miss: a skill's name and
description are injected into the system prompt every session whether or not the
skill is ever invoked. A bad skill sitting in the library costs context and
biases behaviour without the Skill tool ever firing. Gating invocation cannot
fix that; keeping it out of the library can.

    pending.py list
    pending.py show <id>
    pending.py refine <id>
    pending.py approve <id> [<id>...] | --all | --from <dir>
    pending.py reject  <id> [<id>...] | --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard  # noqa: E402
import lint  # noqa: E402

KIND_NEW = "new"
KIND_PATCH = "patch"
KIND_ARCHIVE = "archive"

#: Where the curator moves a skill it has retired. Archiving is the strongest
#: action available to it, and the only one that removes a skill from play.
ARCHIVE_DIR = "archive"

#: Top-level entries under the skills root that are not skills. The audit repo
#: lives in the same tree and is deliberately not copied into the work tree, so
#: every file under it looks like a skill whose files the fork deleted.
_NOT_A_SKILL = {ARCHIVE_DIR, ".git"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", text)[:60]


# ---------------------------------------------------------------------------
# Work tree
# ---------------------------------------------------------------------------


def prepare_work_tree() -> Path:
    """Fresh scratch copy of the library for the fork to edit.

    Two layers. First the live library, including skills the loop may not
    modify, so the fork can read them for context -- knowing what already exists
    is what keeps it patching an umbrella instead of inventing a duplicate.
    Changes to those are dropped at capture time.

    Then the queue on top. The fork sees the library as it *would* look if
    everything pending were approved, which is the only version of it that
    reflects what this loop has already learned. Without that layer every fork
    reads an unchanged library and reaches the same conclusion -- nothing here
    covers this, create a new skill -- so a library that stays empty while
    review lags produces one near-duplicate per session rather than one skill
    that grows. Overlaid whole-directory rather than merged, because that is
    what approval does: the entry's ``after`` tree replaces the live directory,
    files it dropped included.
    """
    shutil.rmtree(guard.WORK_DIR, ignore_errors=True)
    guard.WORK_DIR.mkdir(parents=True, exist_ok=True)
    if guard.SKILLS_DIR.is_dir():
        for child in guard.SKILLS_DIR.iterdir():
            if child.name in (".git", ARCHIVE_DIR):
                continue
            if child.is_dir():
                shutil.copytree(child, guard.WORK_DIR / child.name)
            else:
                shutil.copy2(child, guard.WORK_DIR / child.name)
    for skill, entry in proposals().items():
        source = guard.PENDING_DIR / entry["id"] / "after" / skill
        if not source.is_dir():
            continue
        target = guard.WORK_DIR / skill
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)
    return guard.WORK_DIR


def proposals() -> Dict[str, Dict[str, Any]]:
    """Queued content changes, keyed by skill. Newest wins on collision.

    Archive entries are excluded: they carry no ``after`` tree, and a skill on
    its way out is not a home for a new lesson.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for entry in entries():
        if entry.get("kind") == KIND_ARCHIVE:
            continue
        found[entry["skill"]] = entry
    return found


def _files(root: Path) -> Dict[str, Path]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _skill_name(relative: str) -> Optional[str]:
    parts = Path(relative).parts
    return parts[0] if len(parts) > 1 else None


def _baseline_files() -> Dict[str, Path]:
    """What the work tree looked like before the fork touched it.

    The live library with the queue laid over it -- the same two layers
    :func:`prepare_work_tree` builds. Change detection runs against this, not
    against the live tree: a queued proposal the fork left alone differs from
    the library in every byte, and diffing against the library alone would
    re-file it under this session's id every run.
    """
    base = _files(guard.SKILLS_DIR)
    for skill, entry in proposals().items():
        after = guard.PENDING_DIR / entry["id"] / "after" / skill
        if not after.is_dir():
            continue
        for relative in [r for r in base if _skill_name(r) == skill]:
            base.pop(relative)
        for relative, path in _files(after).items():
            base[f"{skill}/{relative}"] = path
    return base


def _is_live_auto_managed(skill: str) -> bool:
    path = guard.SKILLS_DIR / skill / "SKILL.md"
    if not path.exists():
        return False
    try:
        return guard.is_auto_managed(guard.parse_frontmatter(path.read_text(encoding="utf-8")))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(
    session_id: str, summary: str = "", claims: Optional[List[Dict[str, Any]]] = None
) -> List[str]:
    """File the fork's work-tree changes as pending entries.

    Returns the ids created. Changes to skills the loop does not own are dropped
    and reported, never applied.

    Three shapes of change, because the curator needs more than "some bytes in
    this file differ":

    - a file added or edited under a skill -- the common case;
    - a file *gone* from a skill that still exists -- what consolidation looks
      like once the absorbed content has been moved into the survivor. Walking
      only the work tree made these invisible, so a consolidation queued its
      additions and silently kept the duplicate content;
    - a whole skill moved under ``archive/`` -- what the curator's strongest
      action looks like. Walked naively this filed an entry for a skill named
      ``archive``, which on approval replaced the whole archive directory and
      left the original skill live.
    """
    live = _files(guard.SKILLS_DIR)
    work = _files(guard.WORK_DIR)
    base = _baseline_files()
    queued = proposals()

    live_skills = {s for s in (_skill_name(r) for r in live) if s}
    work_skills = {
        s
        for s in (_skill_name(r) for r in work)
        if s and s not in _NOT_A_SKILL
    }

    touched: Dict[str, Dict[str, Any]] = {}
    rejected: List[str] = []
    archived: List[str] = []
    dropped_deletions: List[str] = []
    refused: List[str] = []

    for relative, path in work.items():
        parts = Path(relative).parts
        if parts[0] == ".git":
            continue
        if parts[0] == ARCHIVE_DIR:
            # The curator retiring a skill. The live archive directory is never
            # copied into the work tree, so anything here is this run's work.
            name = parts[1] if len(parts) > 2 else None
            if not name or name in archived:
                continue
            if name not in live_skills or not _is_live_auto_managed(name):
                rejected.append(relative)
                continue
            archived.append(name)
            continue
        skill = _skill_name(relative)
        if not skill:
            continue
        existed = skill in live_skills
        if existed and not _is_live_auto_managed(skill):
            # A skill the user owns. The fork should not have touched it; if the
            # bytes differ, drop the change rather than queue it for review --
            # offering to approve an edit to a hand-written skill invites
            # exactly the mistake the marker exists to prevent.
            if relative not in live or path.read_bytes() != live[relative].read_bytes():
                rejected.append(relative)
            continue
        if relative in base and path.read_bytes() == base[relative].read_bytes():
            continue
        entry = touched.setdefault(
            skill, {"kind": KIND_PATCH if existed else KIND_NEW, "files": []}
        )
        entry["files"].append(relative)

    for relative in base:
        if relative in work:
            continue
        skill = _skill_name(relative)
        if not skill or skill in _NOT_A_SKILL or skill in archived:
            continue
        if skill not in work_skills:
            # Every file of the skill is gone and it was not archived. The
            # curator is told it may never delete; a fork that tried anyway has
            # its attempt dropped rather than queued, because approving it is
            # the one action in this system that is not recoverable from the
            # queue itself.
            dropped_deletions.append(relative)
            continue
        existed = skill in live_skills
        if existed and not _is_live_auto_managed(skill):
            rejected.append(relative)
            continue
        # A file dropped from a skill that only exists in the queue is the fork
        # revising its own unapproved proposal, not a patch against anything
        # live -- the entry stays NEW so approval still creates the skill.
        entry = touched.setdefault(
            skill, {"kind": KIND_PATCH if existed else KIND_NEW, "files": []}
        )
        entry["files"].append(relative)

    # A fork proposing an archival has no tool that can move a directory, so it
    # is asked to write the copy under archive/ and leave the original alone.
    # When it edits the original into a stub anyway, that edit is a second,
    # contradictory change against a skill already on its way out. The archive
    # entry wins.
    superseded = sorted(set(touched) & set(archived))
    for skill in superseded:
        touched.pop(skill, None)

    ids = []
    for skill, info in sorted(touched.items()):
        # A name the author has already refused. Re-proposing it is the loop
        # asking the same question again a week later, which is the one thing a
        # rejection is supposed to buy freedom from. Dropped loudly, not
        # silently: the lesson behind it is still in the audit log.
        if info["kind"] == KIND_NEW and approval_for(skill) == "never":
            refused.append(skill)
            continue
        ids.append(
            _write_entry(
                skill, info, session_id, summary, claims or [], prior=queued.get(skill)
            )
        )
    for skill in sorted(archived):
        ids.append(_write_archive_entry(skill, session_id, summary))
    if rejected:
        _log_rejected(rejected, session_id)
    if dropped_deletions:
        _log_rejected(dropped_deletions, session_id, event="dropped-deletion")
    if superseded:
        _log_rejected(superseded, session_id, event="superseded-by-archive")
    if refused:
        _log_rejected(refused, session_id, event="dropped-refused")
    return ids


def _merge_claims(
    prior: List[Dict[str, Any]], fresh: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Prior claims first, then anything new, deduplicated on the claim text.

    The evidence for a skill is cumulative: once two sessions have contributed
    to one proposal, dropping the first session's reasoning would leave the
    reviewer judging a file against half the case for it.
    """
    merged = list(prior)
    seen = {str(c.get("claim") or "").strip() for c in merged}
    for claim in fresh:
        text = str(claim.get("claim") or "").strip()
        if text and text not in seen:
            merged.append(claim)
            seen.add(text)
    return merged


def _write_entry(
    skill: str,
    info: Dict[str, Any],
    session_id: str,
    summary: str,
    claims: Optional[List[Dict[str, Any]]] = None,
    prior: Optional[Dict[str, Any]] = None,
) -> str:
    entry_id = f"{_slug(skill)}-{_slug(session_id)[:8]}"
    root = guard.PENDING_DIR / entry_id
    shutil.rmtree(root, ignore_errors=True)
    (root / "after").mkdir(parents=True, exist_ok=True)

    # This session started from the queued proposal, so the entry it produces
    # replaces it rather than sitting beside it. Two entries for one skill would
    # be two answers to the same question, and approving both applies whichever
    # ran last.
    sessions = list(prior.get("sessions") or [prior.get("session")]) if prior else []
    sessions = [s for s in sessions if s and s != session_id]
    supersedes = list(prior.get("supersedes") or []) if prior else []
    if prior and prior["id"] != entry_id:
        supersedes.append(prior["id"])
        shutil.rmtree(guard.PENDING_DIR / prior["id"], ignore_errors=True)
    if prior:
        claims = _merge_claims(prior.get("claims") or [], claims or [])

    # The whole proposed directory, not just the changed files. Approval
    # replaces the live directory with this one, which is also what makes a
    # file the fork removed actually disappear on approval -- it is absent here.
    source = guard.WORK_DIR / skill
    if source.is_dir():
        shutil.copytree(source, root / "after" / skill, dirs_exist_ok=True)

    diff_text = build_diff(skill, info["files"])
    (root / "diff.txt").write_text(diff_text, encoding="utf-8")

    meta = {
        "id": entry_id,
        "skill": skill,
        "kind": info["kind"],
        "files": info["files"],
        "session": session_id,
        # Every session that contributed, oldest first. The reviewer is looking
        # at one file assembled from several sessions and needs to be able to
        # find all of them.
        "sessions": sessions + [session_id],
        "supersedes": supersedes,
        "created_at": now(),
        "summary": summary[:500],
        # What the reflect pass concluded, verbatim. A diff answers "what
        # changed"; these answer "on what evidence", which is the question a
        # reviewer with no memory of the session actually needs answered.
        "claims": claims or [],
        "lint": _lint_entry(skill, info["files"]),
        "added_lines": sum(1 for l in diff_text.splitlines() if l.startswith("+")),
        "removed_lines": sum(1 for l in diff_text.splitlines() if l.startswith("-")),
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return entry_id


_FRONTMATTER_BLOCK = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def _body_of(text: str) -> str:
    return _FRONTMATTER_BLOCK.sub("", text, count=1)


def _other_descriptions(skill: str) -> Dict[str, str]:
    """Descriptions of every other skill in play, owned or not, live or queued.

    Two skills competing for one trigger is a problem whoever wrote them, and it
    is worth catching while both are still proposals -- once one is approved the
    collision costs a review cycle to undo.
    """
    found = {}
    sources = []
    if guard.SKILLS_DIR.is_dir():
        sources += sorted(guard.SKILLS_DIR.glob("*/SKILL.md"))
    for other, entry in proposals().items():
        sources.append(guard.PENDING_DIR / entry["id"] / "after" / other / "SKILL.md")
    for path in sources:
        if path.parent.name in (skill, ARCHIVE_DIR) or not path.is_file():
            continue
        try:
            frontmatter = guard.parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        description = str(frontmatter.get("description") or "").strip()
        if description:
            found[path.parent.name] = description
    return found


def _lint_tree(skill: str, root: Path) -> List[List[str]]:
    """Run the checks over a whole skill directory laid out as ``<root>/<skill>``."""
    files = [f"{skill}/{relative}" for relative in _files(root / skill)]
    return _lint_entry(skill, files, root=root)


def _lint_entry(
    skill: str, files: List[str], root: Optional[Path] = None
) -> List[List[str]]:
    """Run the mechanical checks over what the fork proposes for one skill.

    Cheap enough to run on every entry, and it runs here rather than at approval
    time so the findings are part of what the reader is looking at when they
    decide.
    """
    findings: List[lint.Finding] = []
    for relative in sorted(set(files)):
        path = (root or guard.WORK_DIR) / relative
        if not path.is_file():
            continue  # a deletion; nothing to lint
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts = Path(relative).parts
        if len(parts) == 2 and parts[1] == "SKILL.md":
            frontmatter = guard.parse_frontmatter(text)
            findings += lint.check_frontmatter(frontmatter)
            findings += lint.check_body(_body_of(text))
            description = str(frontmatter.get("description") or "").strip()
            if description:
                findings += lint.check_duplicate_trigger(
                    description, _other_descriptions(skill)
                )
            live = guard.SKILLS_DIR / relative
            if live.is_file():
                try:
                    findings += lint.check_deletion(
                        live.read_text(encoding="utf-8"), text
                    )
                except OSError:
                    pass
        elif relative.endswith(".md"):
            findings += lint.check_reference(text)
            findings += lint.check_link_depth(relative, text)
    return [[severity, message] for severity, message in findings]


def _live_description(skill: str) -> str:
    path = guard.SKILLS_DIR / skill / "SKILL.md"
    try:
        frontmatter = guard.parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return str(frontmatter.get("description", "")).strip()


def _write_archive_entry(skill: str, session_id: str, summary: str) -> str:
    """File a proposed archival.

    No ``after`` tree: approval moves the *live* directory, so what the fork
    left in the work tree is not the thing being applied. The entry carries the
    description instead, which is what the reader needs to judge it.
    """
    entry_id = f"archive-{_slug(skill)}-{_slug(session_id)[:8]}"
    root = guard.PENDING_DIR / entry_id
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": entry_id,
        "skill": skill,
        "kind": KIND_ARCHIVE,
        "files": [],
        "session": session_id,
        "created_at": now(),
        "summary": summary[:500],
        "description": _live_description(skill),
        "added_lines": 0,
        "removed_lines": 0,
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return entry_id


def build_diff(skill: str, files: List[str]) -> str:
    chunks = []
    for relative in sorted(files):
        old_path = guard.SKILLS_DIR / relative
        new_path = guard.WORK_DIR / relative
        old = old_path.read_text(encoding="utf-8", errors="replace").splitlines(True) \
            if old_path.exists() else []
        new = new_path.read_text(encoding="utf-8", errors="replace").splitlines(True) \
            if new_path.exists() else []
        chunks.extend(
            difflib.unified_diff(
                old, new, fromfile=f"live/{relative}", tofile=f"proposed/{relative}"
            )
        )
    return "".join(chunks)


def log(payload: Dict[str, Any]) -> None:
    """Append one line to the shared audit log.

    Never raises: a review that cannot write its own history is still a review,
    and this runs inside a hook.
    """
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(guard.STATE_DIR / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({**payload, "at": now()}) + "\n")
    except OSError:
        pass


def _log_rejected(
    paths: List[str], session_id: str, event: str = "dropped-protected-edit"
) -> None:
    log({"event": event, "session": session_id, "paths": paths})


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def entries() -> List[Dict[str, Any]]:
    if not guard.PENDING_DIR.is_dir():
        return []
    found = []
    for directory in sorted(guard.PENDING_DIR.iterdir()):
        meta = directory / "meta.json"
        if not meta.is_file():
            continue
        try:
            found.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(found, key=lambda e: e.get("created_at", ""))


def get(entry_id: str) -> Optional[Dict[str, Any]]:
    for entry in entries():
        if entry["id"] == entry_id or entry["skill"] == entry_id:
            return entry
    return None


def _approve_archive(entry: Dict[str, Any]) -> bool:
    """Move a live skill under ``archive/``.

    Recoverable by design: the directory keeps its contents and its git history,
    and moving it back is one ``mv``. No approval verdict is recorded -- the
    approvals map is a trust decision about a skill's *content*, and retiring a
    skill is not a statement that its content was ever wrong.
    """
    skill = entry["skill"]
    source = guard.SKILLS_DIR / skill
    if not source.is_dir():
        return False

    guard.ensure_skills_repo()
    target = guard.SKILLS_DIR / ARCHIVE_DIR / skill
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(source), str(target))

    guard.commit(
        f"archived: {skill}\n\n{entry.get('summary', '')}\n\n"
        f"Session: {entry.get('session', 'unknown')}"
    )
    shutil.rmtree(guard.PENDING_DIR / entry["id"], ignore_errors=True)
    return True


def blocking_findings(entry: Dict[str, Any]) -> List[str]:
    return lint.blocking([tuple(f) for f in (entry.get("lint") or []) if len(f) == 2])


def approve(
    entry_id: str,
    force: bool = False,
    source: Optional[Path] = None,
    automatic: bool = False,
) -> bool:
    """Move a pending entry into the live library and commit it.

    ``source`` applies a directory other than the entry's own ``after`` tree —
    what ``--refine`` produces once the draft has been worked on interactively.
    The entry's recorded checks describe the draft, so a refined directory is
    re-checked from scratch rather than approved on the strength of the
    proposal's clean bill of health.

    ``automatic`` means the policy approved this, not a person. Two things
    follow, and both matter more than they look:

    - The recorded verdict is ``auto``, never ``always``. ``always`` silences
      ``skillgate`` permanently, and retiring the queue *and* the use-time gate
      in one step would leave a skill nobody ever looked at with nothing
      standing between it and every future session.
    - The skill goes on probation. A description enters every system prompt
      whether or not the skill is ever invoked, so a library that only grows is
      a cost that only grows. The curator retires what probation does not
      justify.
    """
    entry = get(entry_id)
    if not entry:
        return False
    if source is not None:
        if not (source / "SKILL.md").is_file():
            print(f"{entry['id']}: no SKILL.md under {source}", file=sys.stderr)
            return False
        entry = dict(entry, lint=_lint_tree(entry["skill"], source.parent))
    blockers = blocking_findings(entry)
    if blockers and not force:
        # These are the findings that mean the file would not load, or would
        # load and never match anything. Approving one adds weight to every
        # future system prompt in exchange for nothing.
        print(f"{entry['id']}: refusing — the skill is malformed:", file=sys.stderr)
        for message in blockers:
            print(f"  {message}", file=sys.stderr)
        print("  approve --force to override", file=sys.stderr)
        return False
    if entry["kind"] == KIND_ARCHIVE:
        return _approve_archive(entry)
    root = guard.PENDING_DIR / entry["id"]
    if source is None:
        source = root / "after" / entry["skill"]
    if not source.is_dir():
        return False

    guard.ensure_skills_repo()
    target = guard.SKILLS_DIR / entry["skill"]
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)

    sessions = entry.get("sessions") or [entry.get("session", "unknown")]
    guard.commit(
        f"{'auto-approved' if automatic else 'approved'}: "
        f"{entry['skill']} ({entry['kind']})\n\n"
        f"{entry.get('summary', '')}\n\n"
        f"Session: {', '.join(str(s) for s in sessions if s)}"
    )
    set_approval(entry["skill"], "auto" if automatic else "always")
    if automatic:
        start_trial(entry["skill"])
        log({
            "event": "auto-approved",
            "skill": entry["skill"],
            "kind": entry["kind"],
            "id": entry["id"],
            "claims": len(entry.get("claims") or []),
            "sessions": [str(s) for s in sessions if s],
        })
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(refine_root() / entry["id"], ignore_errors=True)
    return True


# ---------------------------------------------------------------------------
# Refinement — the one point in the loop where a human is present
# ---------------------------------------------------------------------------

def refine_root() -> Path:
    """Where a draft is staged for interactive work.

    Outside ~/.claude for the same reason the fork's work tree is: Claude Code
    refuses Write under its own config directory, and a refinement session that
    silently saves nothing is worse than no refinement at all.

    Resolved at call time, never bound at import: a module constant would hold
    the real cache directory even after a test redirected ``guard.WORK_ROOT``,
    which is how the suite ends up writing to the user's machine.
    """
    return guard.WORK_ROOT / "refine"


def skill_creator_path() -> Optional[Path]:
    """Anthropic's skill-creator, if this machine has it.

    Checked at call time rather than declared as a dependency: patina has to
    work without it, and a missing plugin should degrade to ordinary approval
    with a one-line note, not an error.
    """
    candidates = [guard.CLAUDE_DIR / "skills" / "skill-creator" / "SKILL.md"]
    for kind in ("cache", "marketplaces"):
        candidates += sorted(
            (guard.CLAUDE_DIR / "plugins" / kind).glob(
                "*/**/skills/skill-creator/SKILL.md"
            )
        )
    for path in candidates:
        if path.is_file():
            return path.parent
    return None


def stage_for_refinement(entry: Dict[str, Any]) -> Optional[Path]:
    """Copy a queued draft somewhere it can be edited by hand.

    The queue entry itself is left untouched. Refinement can be abandoned, and
    an entry half-rewritten in place would be neither the proposal the loop made
    nor the skill the author wanted.
    """
    source = guard.PENDING_DIR / entry["id"] / "after" / entry["skill"]
    if not source.is_dir():
        return None
    target = refine_root() / entry["id"] / entry["skill"]
    shutil.rmtree(refine_root() / entry["id"], ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def reject(entry_id: str) -> bool:
    entry = get(entry_id)
    if not entry:
        return False
    shutil.rmtree(guard.PENDING_DIR / entry["id"], ignore_errors=True)
    if entry["kind"] == KIND_NEW:
        # Remember the refusal so the loop is not asked about it again next week.
        set_approval(entry["skill"], "never")
    return True


# ---------------------------------------------------------------------------
# Approvals (shared with skillgate.py)
# ---------------------------------------------------------------------------


def read_approvals() -> Dict[str, Any]:
    try:
        return json.loads(guard.APPROVALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_approvals(data: Dict[str, Any]) -> None:
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = guard.APPROVALS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(guard.APPROVALS_FILE)


def set_approval(skill: str, verdict: str) -> None:
    """verdict: always | never | (session handled per-session in skillgate)."""
    data = read_approvals()
    data.setdefault("skills", {})[skill] = {"verdict": verdict, "at": now()}
    write_approvals(data)


def approval_for(skill: str) -> Optional[str]:
    return (read_approvals().get("skills", {}).get(skill) or {}).get("verdict")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_CONFIDENCE_ORDER = ("high", "medium", "low")


def _top_confidence(claims: List[Dict[str, Any]]) -> str:
    levels = {str(c.get("confidence") or "").lower() for c in claims}
    for level in _CONFIDENCE_ORDER:
        if level in levels:
            return level
    return "unrated"


# ---------------------------------------------------------------------------
# Autonomous approval
# ---------------------------------------------------------------------------
#
# The queue was added after the loop worked, and twelve days of data said it is
# where the loop dies: 46 proposals, 0 approvals, nothing in the library. A
# proposal nobody reads is not safer than one that landed -- it is the same
# money spent for none of the benefit.
#
# So: a policy, not a blanket. What follows is the whole of what can reach the
# library without a person, and every check in it already existed for the
# human path. Nothing here is a new judgement about quality; it is the existing
# judgements, applied without waiting.


def _state_file() -> Path:
    # Resolved at call time. ``review`` owns this file, but importing it here
    # would be circular -- and worse, ``pending`` runs as __main__ under
    # bin/patina, so the import would produce a second copy of the module.
    return guard.STATE_DIR / "state.json"


def read_state() -> Dict[str, Any]:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: Dict[str, Any]) -> None:
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _state_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_state_file())


def _setting(state: Dict[str, Any], key: str, env_name: str, default):
    """A state.json key, overridable by ``PATINA_<env_name>``."""
    raw = guard.env(env_name)
    if raw is None:
        raw = state.get(key)
    if raw is None:
        return default
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


def autonomous(state: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the loop may approve without asking.

    Off by default, and deliberately: no version of this has ever written to a
    real library, and the first one to do so should be switched on by someone
    who decided to.
    """
    return _setting(state if state is not None else read_state(),
                    "autonomous", "AUTONOMOUS", False)


def auto_min_lessons(state: Dict[str, Any]) -> int:
    return max(1, _setting(state, "auto_min_lessons", "AUTO_MIN_LESSONS", 2))


def auto_max_patch_lines(state: Dict[str, Any]) -> int:
    return max(1, _setting(state, "auto_max_patch_lines", "AUTO_MAX_PATCH_LINES", 120))


def auto_trial_days(state: Dict[str, Any]) -> int:
    return max(1, _setting(state, "auto_trial_days", "AUTO_TRIAL_DAYS", 14))


def start_trial(skill: str) -> None:
    """Put an auto-approved skill on probation.

    Recorded in state rather than stamped into the skill's frontmatter: the
    file is what the model wrote and what the user may later edit by hand, and
    a bookkeeping field the loop rewrites is one more thing that can go wrong
    in a file whose frontmatter has to stay loadable.
    """
    state = read_state()
    state.setdefault("auto_approved", {})[skill] = {"since": now()}
    write_state(state)


def clear_trial(skill: str) -> None:
    """It was loaded. It earned its place."""
    state = read_state()
    trials = state.get("auto_approved") or {}
    if trials.pop(skill, None) is not None:
        write_state(state)


def expired_trials(state: Optional[Dict[str, Any]] = None) -> List[str]:
    """Auto-approved skills that were never loaded inside the trial window.

    The curator acts on these. Without it autonomous mode is a ratchet: every
    proposal that passes the policy adds weight to every later system prompt,
    and nothing ever takes any of it off again.
    """
    state = read_state() if state is None else state
    trials = state.get("auto_approved") or {}
    usage = state.get("usage") or {}
    window = dt.timedelta(days=auto_trial_days(state))
    now_ts = dt.datetime.now(dt.timezone.utc)

    expired = []
    for skill, row in trials.items():
        if (usage.get(skill) or {}).get("count"):
            continue  # loaded at least once; the trial is over and it passed
        try:
            since = dt.datetime.fromisoformat(str(row.get("since")))
        except (TypeError, ValueError):
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.timezone.utc)
        if now_ts - since >= window:
            expired.append(skill)
    return sorted(expired)


def auto_approve_queue(
    dry_run: bool = False, only: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Run the policy over the queue, or over ``only`` those entries.

    Returns what it took and what it left, with the reason for each. The
    reasons are the point: a policy nobody can inspect is not meaningfully
    different from no policy.

    ``only`` is what the review and curate paths pass -- the entries they just
    filed. Draining the whole backlog as a side effect of an unrelated session
    is not the same decision as turning autonomous mode on, and must not happen
    by accident. The CLI is where a person asks for that, deliberately.
    """
    state = read_state()
    taken, left = [], []
    for entry in entries():
        if only is not None and entry["id"] not in only:
            continue
        reason = auto_verdict(entry, state)
        if reason is not None:
            left.append((entry["id"], reason))
            continue
        if dry_run or approve(entry["id"], automatic=True):
            taken.append(entry["id"])
        else:
            left.append((entry["id"], "approve failed"))
    return {"approved": taken, "held": left, "dry_run": dry_run}


def _changed_lines(entry: Dict[str, Any]) -> int:
    diff = entry.get("diff") or ""
    return sum(
        1 for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def auto_verdict(
    entry: Dict[str, Any], state: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """``None`` to approve without asking, or the reason it has to wait.

    Ordered cheapest-and-most-absolute first, so the reason reported is the one
    that matters most rather than whichever check happened to run.
    """
    state = read_state() if state is None else state

    # A standing human decision. Nothing in the policy outranks it, ever.
    if approval_for(entry["skill"]) == "never":
        return "you rejected this skill name before"

    # Would not load, or would load and match nothing. --force exists for the
    # human path and is unreachable from here by construction.
    blockers = blocking_findings(entry)
    if blockers:
        return f"malformed: {blockers[0]}"

    kind = entry.get("kind")
    if kind == KIND_ARCHIVE:
        # Retiring a skill someone is using is not maintenance, it is a
        # surprise. Unused is the curator's own criterion and it is checkable.
        usage = (read_state().get("usage") or {}).get(entry["skill"]) or {}
        if usage.get("count"):
            return f"in use ({usage['count']} loads) -- retiring it needs a person"
        return None
    if kind not in (KIND_NEW, KIND_PATCH):
        return f"unrecognised kind {kind!r}"

    claims = entry.get("claims") or []
    if len(claims) < auto_min_lessons(state):
        return (
            f"{len(claims)} lesson(s), policy wants "
            f"{auto_min_lessons(state)} -- one is an anecdote"
        )
    if _top_confidence(claims) != "high":
        return f"strongest claim is {_top_confidence(claims)}, not high"

    if kind == KIND_PATCH:
        changed = _changed_lines(entry)
        if changed > auto_max_patch_lines(state):
            return (
                f"{changed} changed lines is a rewrite, not a patch "
                f"(limit {auto_max_patch_lines(state)})"
            )
    return None


def cmd_list() -> int:
    queue = entries()
    if not queue:
        print("Nothing pending. The live library is exactly what you approved.")
        return 0
    print(f"{len(queue)} pending:\n")
    labels = {KIND_NEW: "NEW  ", KIND_PATCH: "PATCH", KIND_ARCHIVE: "ARCHV"}
    for entry in queue:
        label = labels.get(entry["kind"], "?????")
        counts = (
            "retire from the library"
            if entry["kind"] == KIND_ARCHIVE
            else f"+{entry.get('added_lines', 0)}/-{entry.get('removed_lines', 0)} lines"
        )
        print(
            f"  {label} {entry['id']}\n"
            f"        skill: {entry['skill']}  {counts}\n"
            f"        from:  session {entry.get('session', '?')[:8]} "
            f"at {entry.get('created_at', '?')[:19]}"
        )
        if entry.get("summary"):
            print(f"        why:   {entry['summary'][:120]}")
        claims = entry.get("claims") or []
        if claims:
            best = _top_confidence(claims)
            sessions = entry.get("sessions") or []
            across = f" across {len(sessions)} sessions" if len(sessions) > 1 else ""
            print(
                f"        based on {len(claims)} lesson(s){across}, "
                f"strongest: {best}"
            )
        blockers = blocking_findings(entry)
        warnings = len(entry.get("lint") or []) - len(blockers)
        if blockers:
            print(f"        MALFORMED: {blockers[0]}")
        elif warnings > 0:
            print(f"        {warnings} check warning(s) — see show")
        print()
    print("Review with:  pending.py show <id>")
    print("Then:         pending.py approve <id>   |   pending.py reject <id>")
    return 0


def cmd_show(entry_id: str) -> int:
    entry = get(entry_id)
    if not entry:
        print(f"No pending entry: {entry_id}", file=sys.stderr)
        return 1
    print(f"id:      {entry['id']}")
    print(f"skill:   {entry['skill']}")
    print(f"kind:    {entry['kind']}")
    sessions = entry.get("sessions") or [entry.get("session")]
    print(f"session: {', '.join(str(s) for s in sessions if s)}")
    print(f"created: {entry.get('created_at')}")
    if entry.get("supersedes"):
        print(f"replaces: {', '.join(entry['supersedes'])}")
    if entry.get("summary"):
        print(f"summary: {entry['summary']}")
    print()

    findings = [tuple(f) for f in (entry.get("lint") or []) if len(f) == 2]
    if findings:
        print("checks:")
        print(lint.format_findings(findings))
        print()

    claims = entry.get("claims") or []
    if claims:
        # Before the diff, not after: judging whether a change is right starts
        # with what it claims to have learned, and the diff is the proposed
        # answer to that. Reading them the other way round invites approving a
        # well-formed edit that rests on nothing.
        print("what it says it learned:")
        for claim in claims:
            print(f"  [{claim.get('kind', '?')}, {claim.get('confidence', '?')}] "
                  f"{str(claim.get('claim', '')).strip()}")
            evidence = str(claim.get("evidence", "")).strip()
            if evidence:
                print(f"      evidence: {evidence[:300]}")
        print()

    root = guard.PENDING_DIR / entry["id"]
    if entry["kind"] == KIND_ARCHIVE:
        print(f"description: {entry.get('description', '(none)')}")
        print()
        print(
            f"Approving moves skills/{entry['skill']}/ to "
            f"skills/{ARCHIVE_DIR}/{entry['skill']}/.\n"
            "The directory keeps its contents and its git history; moving it "
            "back restores it."
        )
        return 0
    if entry["kind"] == KIND_NEW:
        for path in sorted((root / "after").rglob("*")):
            if path.is_file():
                print(f"--- {path.relative_to(root / 'after')} ---")
                print(path.read_text(encoding="utf-8", errors="replace"))
    else:
        diff = (root / "diff.txt").read_text(encoding="utf-8", errors="replace")
        print(diff or "(no textual diff)")
    return 0


def cmd_refine(entry_id: str) -> int:
    """Stage a draft for interactive work and say how to finish.

    The loop's writing pass runs with no user, no evals, and a spend ceiling, so
    what it files is a draft with the evidence attached — not a finished skill.
    Approval is the one moment in this system where a human is actually present,
    which makes it the right place to spend effort on the wording, the trigger,
    and whether the thing works at all. This command hands the draft to
    Anthropic's skill-creator for exactly that, and gets out of the way.
    """
    entry = get(entry_id)
    if not entry:
        print(f"No pending entry: {entry_id}", file=sys.stderr)
        return 1
    if entry["kind"] == KIND_ARCHIVE:
        print("An archival has nothing to refine — approve or reject it.",
              file=sys.stderr)
        return 1
    staged = stage_for_refinement(entry)
    if staged is None:
        print(f"{entry['id']}: no proposed files to refine", file=sys.stderr)
        return 1

    print(f"staged: {staged}")
    print()
    claims = entry.get("claims") or []
    if claims:
        print("What this draft claims to have learned:")
        for claim in claims:
            print(f"  [{claim.get('kind', '?')}, {claim.get('confidence', '?')}] "
                  f"{str(claim.get('claim', '')).strip()}")
        print()

    creator = skill_creator_path()
    if creator:
        print(f"skill-creator: {creator}")
        print(
            "Work on the staged copy with it — the draft is written, so this "
            "starts at\nthe evaluate-and-iterate half of its loop, not at the "
            "interview."
        )
        if "marketplaces" in creator.parts:
            # Cloned by `plugin marketplace add`, not installed. Readable, but
            # there is no Skill tool entry for it, so saying only "here it is"
            # would have the session hunt for a command that does not exist.
            print(
                "\nThat copy is a marketplace clone, not an installed plugin: "
                "read its\nSKILL.md and follow it directly, or install it "
                "first with\n"
                "  claude plugin install skill-creator@claude-plugins-official"
            )
    else:
        print("skill-creator is not installed on this machine. Either edit the")
        print("staged copy by hand, or install it and run this again:")
        print("  claude plugin install skill-creator@claude-plugins-official")
    print()
    print("When the staged copy is right:")
    print(f"  patina pending approve {entry['id']} --from {staged}")
    print("Or leave it: the queue entry is untouched until you approve it.")
    return 0


def cmd_decide(
    action: str,
    ids: List[str],
    every: bool,
    force: bool = False,
    source: Optional[str] = None,
) -> int:
    targets = [e["id"] for e in entries()] if every else ids
    if not targets:
        print("Nothing to do.", file=sys.stderr)
        return 1
    if source and len(targets) != 1:
        print("--from applies to exactly one entry.", file=sys.stderr)
        return 1
    done = 0
    for entry_id in targets:
        ok = (
            approve(entry_id, force=force, source=Path(source) if source else None)
            if action == "approve"
            else reject(entry_id)
        )
        if ok:
            print(f"{action}d: {entry_id}")
            done += 1
        elif not get(entry_id):
            print(f"not found: {entry_id}", file=sys.stderr)
    return 0 if done else 1


def cmd_auto(action: str) -> int:
    """The autonomous-mode switch and its dry run."""
    state = read_state()
    if action in ("on", "off"):
        state["autonomous"] = action == "on"
        write_state(state)
        log({"event": "autonomous", "enabled": state["autonomous"]})
        if action == "on":
            print(
                "Autonomous mode ON.\n\n"
                "From here, proposals that pass the policy land in your library "
                "without\nasking. What that means in practice:\n\n"
                f"  - Only high-confidence proposals with at least "
                f"{auto_min_lessons(state)} lessons behind them.\n"
                "  - Nothing malformed, and nothing whose name you have "
                "rejected before.\n"
                "  - Every one is a git commit in ~/.claude/skills you can "
                "revert.\n"
                f"  - Each is on trial for {auto_trial_days(state)} days: if "
                "nothing loads it, the\n    curator archives it again.\n"
                "  - The first time one actually runs, you are asked.\n\n"
                "This includes what is already queued. The policy runs over the "
                "whole queue\non every scheduled pass, in the background -- "
                "that is what makes it\nautonomous rather than a command you "
                "have to remember.\n\n"
                "  patina pending auto --dry-run    # what it would take, right now\n"
                "  patina pending auto off          # stop it"
            )
        else:
            print(
                "Autonomous mode OFF. Proposals wait for you in "
                "`patina pending list` again.\n"
                "Anything already approved stays; trials already running still "
                "expire."
            )
        return 0

    if action == "status":
        print(f"Autonomous mode: {'ON' if autonomous(state) else 'OFF'}")
        print(f"  minimum lessons:  {auto_min_lessons(state)}")
        print(f"  max patch lines:  {auto_max_patch_lines(state)}")
        print(f"  trial window:     {auto_trial_days(state)} days")
        trials = state.get("auto_approved") or {}
        if trials:
            usage = state.get("usage") or {}
            print(f"\nOn trial ({len(trials)}):")
            for skill, row in sorted(trials.items()):
                loads = (usage.get(skill) or {}).get("count", 0)
                print(f"  {skill:<40} since {str(row.get('since'))[:10]}  "
                      f"loads: {loads}")
            expired = expired_trials(state)
            if expired:
                print("\nTrial expired, the curator will archive: "
                      + ", ".join(expired))
        return 0

    # dry-run / run
    dry = action == "dry-run"
    queue = entries()
    if not queue:
        print("Nothing pending.")
        return 0
    outcome = auto_approve_queue(dry_run=dry)
    verb = "would approve" if dry else "approved"
    print(f"{len(outcome['approved'])} {verb}, {len(outcome['held'])} held.\n")
    for entry_id in outcome["approved"]:
        print(f"  PASS  {entry_id}")
    if outcome["approved"] and outcome["held"]:
        print()
    for entry_id, why in outcome["held"]:
        print(f"  HOLD  {entry_id}\n        {why}")
    if dry:
        print("\nNothing was applied. Run without --dry-run to apply.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("id")
    refine = sub.add_parser("refine")
    refine.add_argument("id")
    for name in ("approve", "reject"):
        action = sub.add_parser(name)
        action.add_argument("id", nargs="*")
        action.add_argument("--all", action="store_true")
        if name == "approve":
            action.add_argument(
                "--force", action="store_true",
                help="apply even when the skill fails a blocking check",
            )
            action.add_argument(
                "--from", dest="source", default=None,
                help="apply this directory instead of the queued draft "
                     "(what `refine` staged)",
            )
    auto = sub.add_parser(
        "auto", help="autonomous approval: on | off | status | --dry-run"
    )
    auto.add_argument("action", nargs="?", default="run",
                      choices=["on", "off", "status", "run"])
    auto.add_argument("--dry-run", action="store_true",
                      help="report what the policy would take, apply nothing")
    args = parser.parse_args()

    if args.command == "auto":
        action = "dry-run" if getattr(args, "dry_run", False) else args.action
        return cmd_auto(action)
    if args.command == "show":
        return cmd_show(args.id)
    if args.command == "refine":
        return cmd_refine(args.id)
    if args.command in ("approve", "reject"):
        return cmd_decide(
            args.command,
            args.id,
            args.all,
            force=getattr(args, "force", False),
            source=getattr(args, "source", None),
        )
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
