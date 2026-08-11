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
    pending.py approve <id> [<id>...] | --all
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
    """Fresh scratch copy of the live library for the fork to edit.

    The whole library is copied, including skills the loop may not modify, so
    the fork can read them for context -- knowing what already exists is what
    keeps it patching an umbrella instead of inventing a duplicate. Changes to
    those are dropped at capture time.
    """
    shutil.rmtree(guard.WORK_DIR, ignore_errors=True)
    guard.WORK_DIR.mkdir(parents=True, exist_ok=True)
    if guard.SKILLS_DIR.is_dir():
        for child in guard.SKILLS_DIR.iterdir():
            if child.name in (".git", "archive"):
                continue
            if child.is_dir():
                shutil.copytree(child, guard.WORK_DIR / child.name)
            else:
                shutil.copy2(child, guard.WORK_DIR / child.name)
    return guard.WORK_DIR


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
        if relative in live and path.read_bytes() == live[relative].read_bytes():
            continue
        entry = touched.setdefault(
            skill, {"kind": KIND_PATCH if existed else KIND_NEW, "files": []}
        )
        entry["files"].append(relative)

    for relative in live:
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
        if not _is_live_auto_managed(skill):
            rejected.append(relative)
            continue
        entry = touched.setdefault(skill, {"kind": KIND_PATCH, "files": []})
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
        ids.append(_write_entry(skill, info, session_id, summary, claims or []))
    for skill in sorted(archived):
        ids.append(_write_archive_entry(skill, session_id, summary))
    if rejected:
        _log_rejected(rejected, session_id)
    if dropped_deletions:
        _log_rejected(dropped_deletions, session_id, event="dropped-deletion")
    if superseded:
        _log_rejected(superseded, session_id, event="superseded-by-archive")
    return ids


def _write_entry(
    skill: str,
    info: Dict[str, Any],
    session_id: str,
    summary: str,
    claims: Optional[List[Dict[str, Any]]] = None,
) -> str:
    entry_id = f"{_slug(skill)}-{_slug(session_id)[:8]}"
    root = guard.PENDING_DIR / entry_id
    shutil.rmtree(root, ignore_errors=True)
    (root / "after").mkdir(parents=True, exist_ok=True)

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
    """Descriptions of every other live skill, owned or not.

    Two skills competing for one trigger is a problem whoever wrote them.
    """
    found = {}
    if not guard.SKILLS_DIR.is_dir():
        return found
    for path in sorted(guard.SKILLS_DIR.glob("*/SKILL.md")):
        if path.parent.name in (skill, ARCHIVE_DIR):
            continue
        try:
            frontmatter = guard.parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        description = str(frontmatter.get("description") or "").strip()
        if description:
            found[path.parent.name] = description
    return found


def _lint_entry(skill: str, files: List[str]) -> List[List[str]]:
    """Run the mechanical checks over what the fork proposes for one skill.

    Cheap enough to run on every entry, and it runs here rather than at approval
    time so the findings are part of what the reader is looking at when they
    decide.
    """
    findings: List[lint.Finding] = []
    for relative in sorted(set(files)):
        path = guard.WORK_DIR / relative
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


def _log_rejected(
    paths: List[str], session_id: str, event: str = "dropped-protected-edit"
) -> None:
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(guard.STATE_DIR / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event": event,
                "at": now(),
                "session": session_id,
                "paths": paths,
            }) + "\n")
    except OSError:
        pass


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


def approve(entry_id: str, force: bool = False) -> bool:
    """Move a pending entry into the live library and commit it."""
    entry = get(entry_id)
    if not entry:
        return False
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
    source = root / "after" / entry["skill"]
    if not source.is_dir():
        return False

    guard.ensure_skills_repo()
    target = guard.SKILLS_DIR / entry["skill"]
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)

    guard.commit(
        f"approved: {entry['skill']} ({entry['kind']})\n\n"
        f"{entry.get('summary', '')}\n\nSession: {entry.get('session', 'unknown')}"
    )
    set_approval(entry["skill"], "always")
    shutil.rmtree(root, ignore_errors=True)
    return True


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
            print(f"        based on {len(claims)} lesson(s), strongest: {best}")
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
    print(f"session: {entry.get('session')}")
    print(f"created: {entry.get('created_at')}")
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


def cmd_decide(action: str, ids: List[str], every: bool, force: bool = False) -> int:
    targets = [e["id"] for e in entries()] if every else ids
    if not targets:
        print("Nothing to do.", file=sys.stderr)
        return 1
    done = 0
    for entry_id in targets:
        ok = approve(entry_id, force=force) if action == "approve" else reject(entry_id)
        if ok:
            print(f"{action}d: {entry_id}")
            done += 1
        elif not get(entry_id):
            print(f"not found: {entry_id}", file=sys.stderr)
    return 0 if done else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("id")
    for name in ("approve", "reject"):
        action = sub.add_parser(name)
        action.add_argument("id", nargs="*")
        action.add_argument("--all", action="store_true")
        if name == "approve":
            action.add_argument(
                "--force", action="store_true",
                help="apply even when the skill fails a blocking check",
            )
    args = parser.parse_args()

    if args.command == "show":
        return cmd_show(args.id)
    if args.command in ("approve", "reject"):
        return cmd_decide(
            args.command, args.id, args.all, force=getattr(args, "force", False)
        )
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
