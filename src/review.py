#!/usr/bin/env python3
"""SessionEnd entry point — review a finished session and keep what matters.

Reads the hook payload on stdin, digests the transcript, and forks a detached
headless ``claude -p`` restricted to the writable skill set. Verifies the fork's
writes afterwards, commits them to the local audit repo, and appends to the
audit log.

The hook ALWAYS exits 0. A broken review must never disrupt the user's session.

Usage as a hook (stdin carries the payload):
    review.py

Usage by hand:
    review.py --transcript path/to.jsonl --dry-run
    review.py --transcript path/to.jsonl
    review.py --status
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest as digest_mod  # noqa: E402
import guard  # noqa: E402
import pending  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
AUDIT_LOG = guard.STATE_DIR / "audit.jsonl"
STATE_FILE = guard.STATE_DIR / "state.json"

MODEL = os.environ.get("CLAUDE_SELF_IMPROVE_MODEL", "sonnet")
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_SELF_IMPROVE_TIMEOUT", "600"))
MAX_TURNS = "30"

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
DENIED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log(entry: Dict[str, Any]) -> None:
    """Append one line to the audit log.

    Every run is logged, including no-ops. A loop that has quietly stopped
    learning is otherwise indistinguishable from one with nothing to learn.
    """
    entry.setdefault("at", now())
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: Dict[str, Any]) -> None:
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def describe_writable() -> str:
    skills = guard.writable_skills()
    if not skills:
        return (
            "(none yet — the library is empty. Create the first class-level "
            "skill if this session earned one.)"
        )
    lines = []
    for path in skills:
        frontmatter = guard.parse_frontmatter(path.read_text(encoding="utf-8"))
        description = str(frontmatter.get("description", "")).strip()
        lines.append(f"- {path.parent.name}: {description[:160]}")
    return "\n".join(lines)


def build_prompt(digest_text: str, cwd: str, session_id: str) -> str:
    template = (PROMPTS_DIR / "review.md").read_text(encoding="utf-8")
    return (
        # The fork is pointed at the scratch copy, never the live library. It is
        # told so explicitly: a skill author who thinks their edit ships
        # immediately writes differently from one who knows it faces review.
        template.replace("{memory_dir}", str(guard.memory_dir(cwd)))
        .replace("{skills_dir}", str(guard.WORK_DIR))
        .replace("{writable_skills}", describe_writable())
        .replace("{session_id}", session_id or "unknown")
        .replace("{today}", dt.date.today().isoformat())
        .replace("{digest}", digest_text)
    )


_CREATED_FROM = re.compile(r"^(\s*createdFrom:).*$", re.MULTILINE)
_METADATA_LINE = re.compile(r"^metadata:\s*$", re.MULTILINE)
_FRONTMATTER_OPEN = re.compile(r"\A---\r?\n")


def normalize_new_skills(session_id: str) -> None:
    """Force the ownership marker and real session id onto newly written skills.

    Two corrections, both because the prompt cannot be trusted to be followed:

    - ``createdFrom``: asked for the session id, the model writes a plausible
      label of its own ("kidtopiaplay-2026-07-launch"), breaking the only link
      from a bad skill back to the session that produced it.
    - ``autoManaged``: a skill the loop wrote but did not mark would, once
      approved, be permanently unpatchable by the loop -- it would look
      hand-written forever. If the loop created it, it owns it.
    """
    for path in guard.WORK_DIR.rglob("SKILL.md"):
        skill = path.parent.name
        if (guard.SKILLS_DIR / skill / "SKILL.md").exists():
            continue  # a patch, not a creation -- leave its frontmatter alone
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = text
        if session_id:
            if "createdFrom:" in updated:
                updated = _CREATED_FROM.sub(rf"\1 {session_id}", updated, count=1)
            elif _METADATA_LINE.search(updated):
                updated = _METADATA_LINE.sub(
                    f"metadata:\n  createdFrom: {session_id}", updated, count=1
                )
        if not guard.is_auto_managed(guard.parse_frontmatter(updated)):
            if _METADATA_LINE.search(updated):
                updated = _METADATA_LINE.sub(
                    "metadata:\n  autoManaged: true", updated, count=1
                )
            elif _FRONTMATTER_OPEN.match(updated):
                updated = _FRONTMATTER_OPEN.sub(
                    "---\nmetadata:\n  autoManaged: true\n", updated, count=1
                )
        if updated != text:
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                continue


def run_fork(prompt: str, cwd: str) -> subprocess.CompletedProcess:
    """Spawn the restricted headless child.

    ``--add-dir`` is a harness-level boundary and the first line of defence;
    ``guard.verify_writes`` afterwards is the one that actually decides.
    """
    memory = guard.memory_dir(cwd)
    memory.mkdir(parents=True, exist_ok=True)
    work = guard.WORK_DIR

    command = [
        "claude",
        "-p",
        prompt,
        "--model",
        MODEL,
        "--settings",
        guard.CHILD_SETTINGS,
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        MAX_TURNS,
        "--add-dir",
        str(work),
        "--add-dir",
        str(memory),
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--disallowedTools",
        *DENIED_TOOLS,
    ]
    return subprocess.run(
        command,
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=guard.child_env(),
        check=False,
    )


def review(
    transcript: Path,
    session_id: str,
    cwd: str,
    dry_run: bool = False,
) -> int:
    built = digest_mod.build(transcript)
    if built.message_count == 0:
        log({"event": "skipped", "reason": "empty transcript", "session": session_id})
        return 0

    cwd = built.cwd or cwd or str(Path.cwd())
    pending.prepare_work_tree()
    prompt = build_prompt(built.text, cwd, session_id)

    if dry_run:
        print(prompt)
        print(
            f"\n--- would fork: model={MODEL} messages={built.message_count} "
            f"prompt={len(prompt)} chars truncated={built.truncated}",
            file=sys.stderr,
        )
        return 0

    with guard.lock(f"review-{guard.project_slug(cwd)}") as acquired:
        if not acquired:
            # Another session is reviewing the same project. Leave the watermark
            # unadvanced so the sweep retries this transcript later.
            log({"event": "deferred", "reason": "lock held", "session": session_id})
            return 0

        guard.ensure_skills_repo()
        memory_before = guard.snapshot_dir(guard.memory_dir(cwd))
        started = now()

        try:
            result = run_fork(prompt, cwd)
        except subprocess.TimeoutExpired:
            log({"event": "timeout", "session": session_id, "seconds": TIMEOUT_SECONDS})
            return 0
        except FileNotFoundError:
            log({"event": "error", "reason": "claude binary not found"})
            return 0

        # The fork never saw SKILLS_DIR, so there is nothing to revert there.
        # verify_writes stays as a cheap assertion that this remains true.
        violations = guard.verify_writes(cwd)
        reply = (result.stdout or "").strip()
        normalize_new_skills(session_id)
        queued = pending.capture(session_id, summary=reply[:500])
        memory_diff = guard.diff_snapshot(
            memory_before, guard.snapshot_dir(guard.memory_dir(cwd))
        )
        sha = None

        log(
            {
                "event": "review",
                "session": session_id,
                "cwd": cwd,
                "started": started,
                "model": MODEL,
                "messages": built.message_count,
                "digest_chars": len(built.text),
                "truncated": built.truncated,
                "exit": result.returncode,
                "commit": sha,
                "queued": queued,
                "violations": violations,
                "memory": memory_diff,
                "reply": reply[:2000],
                "stderr": (result.stderr or "")[:1000] if result.returncode else "",
            }
        )

        state = read_state()
        state.setdefault("watermarks", {})[session_id] = transcript.stat().st_mtime
        write_state(state)
        return 0


def show_status() -> int:
    if not AUDIT_LOG.exists():
        print("No runs recorded yet.")
        return 0
    entries = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    reviews = [e for e in entries if e.get("event") == "review"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    recent = [
        e
        for e in reviews
        if _parse_time(e.get("at")) and _parse_time(e.get("at")) > cutoff
    ]
    violations = [e for e in reviews if e.get("violations")]

    print(f"Total reviews:        {len(reviews)}")
    print(f"Last 30 days:         {len(recent)}")
    print(f"Last run:             {reviews[-1]['at'] if reviews else 'never'}")
    print(f"Runs with violations: {len(violations)}")
    if violations:
        print("\nAllowlist violations (reverted):")
        for entry in violations[-5:]:
            for path in entry["violations"]:
                print(f"  {entry['at']}  {path}")
    nothing = sum(1 for e in reviews if "nothing to save" in (e.get("reply") or "").lower())
    print(f"\nNo-op reviews:        {nothing} of {len(reviews)}")
    if reviews and nothing == len(reviews):
        print("  All reviews were no-ops. Check the prompt is reaching the model.")
    return 0


def _parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", help="review a specific transcript")
    parser.add_argument("--dry-run", action="store_true", help="print prompt, fork nothing")
    parser.add_argument("--status", action="store_true", help="report loop health")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--cwd", default="")
    args = parser.parse_args()

    if args.status:
        return show_status()

    # The child sets this. Without the check a hook-spawned claude would fire
    # the same hook and fork again.
    if guard.is_child():
        return 0

    if args.transcript:
        transcript = Path(args.transcript)
        session_id = args.session_id or transcript.stem
        cwd = args.cwd or str(Path.cwd())
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0
        raw = payload.get("transcript_path")
        if not raw:
            return 0
        transcript = Path(raw)
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")

    if not transcript.exists():
        return 0

    try:
        return review(transcript, session_id, cwd, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — the hook must never break a session
        log({"event": "error", "reason": repr(exc), "session": session_id})
        return 0


if __name__ == "__main__":
    sys.exit(main())
