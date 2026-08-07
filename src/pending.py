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

KIND_NEW = "new"
KIND_PATCH = "patch"


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


def capture(session_id: str, summary: str = "") -> List[str]:
    """File the fork's work-tree changes as pending entries.

    Returns the ids created. Changes to skills the loop does not own are dropped
    and reported, never applied.
    """
    live = _files(guard.SKILLS_DIR)
    work = _files(guard.WORK_DIR)

    touched: Dict[str, Dict[str, Any]] = {}
    rejected: List[str] = []

    for relative, path in work.items():
        skill = _skill_name(relative)
        if not skill:
            continue
        existed = skill in {_skill_name(r) for r in live if _skill_name(r)}
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

    ids = []
    for skill, info in sorted(touched.items()):
        ids.append(_write_entry(skill, info, session_id, summary))
    if rejected:
        _log_rejected(rejected, session_id)
    return ids


def _write_entry(skill: str, info: Dict[str, Any], session_id: str, summary: str) -> str:
    entry_id = f"{_slug(skill)}-{_slug(session_id)[:8]}"
    root = guard.PENDING_DIR / entry_id
    shutil.rmtree(root, ignore_errors=True)
    (root / "after").mkdir(parents=True, exist_ok=True)

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
        "added_lines": sum(1 for l in diff_text.splitlines() if l.startswith("+")),
        "removed_lines": sum(1 for l in diff_text.splitlines() if l.startswith("-")),
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


def _log_rejected(paths: List[str], session_id: str) -> None:
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(guard.STATE_DIR / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event": "dropped-protected-edit",
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


def approve(entry_id: str) -> bool:
    """Move a pending entry into the live library and commit it."""
    entry = get(entry_id)
    if not entry:
        return False
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


def cmd_list() -> int:
    queue = entries()
    if not queue:
        print("Nothing pending. The live library is exactly what you approved.")
        return 0
    print(f"{len(queue)} pending:\n")
    for entry in queue:
        label = "NEW  " if entry["kind"] == KIND_NEW else "PATCH"
        print(
            f"  {label} {entry['id']}\n"
            f"        skill: {entry['skill']}  "
            f"+{entry.get('added_lines', 0)}/-{entry.get('removed_lines', 0)} lines\n"
            f"        from:  session {entry.get('session', '?')[:8]} "
            f"at {entry.get('created_at', '?')[:19]}"
        )
        if entry.get("summary"):
            print(f"        why:   {entry['summary'][:120]}")
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

    root = guard.PENDING_DIR / entry["id"]
    if entry["kind"] == KIND_NEW:
        for path in sorted((root / "after").rglob("*")):
            if path.is_file():
                print(f"--- {path.relative_to(root / 'after')} ---")
                print(path.read_text(encoding="utf-8", errors="replace"))
    else:
        diff = (root / "diff.txt").read_text(encoding="utf-8", errors="replace")
        print(diff or "(no textual diff)")
    return 0


def cmd_decide(action: str, ids: List[str], every: bool) -> int:
    targets = [e["id"] for e in entries()] if every else ids
    if not targets:
        print("Nothing to do.", file=sys.stderr)
        return 1
    handler = approve if action == "approve" else reject
    done = 0
    for entry_id in targets:
        if handler(entry_id):
            print(f"{action}d: {entry_id}")
            done += 1
        else:
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
    args = parser.parse_args()

    if args.command == "show":
        return cmd_show(args.id)
    if args.command in ("approve", "reject"):
        return cmd_decide(args.command, args.id, args.all)
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
