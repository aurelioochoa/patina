#!/usr/bin/env python3
"""SessionStart entry point — interval-gated sweep and library maintenance.

Ports Hermes' no-daemon design: nothing is scheduled. Every session start runs a
fast check (a state file read, no subprocess); if the interval has elapsed, the
real work is forked detached and this process returns immediately so the user's
session is never held up.

Two jobs behind the one interval check:

1. **Sweep** — review transcripts that finished without their SessionEnd hook
   firing (hard kill, terminal closed, crash). This is what makes SessionEnd
   sufficient rather than merely usual.
2. **Curate** — maintenance over the autoManaged library: consolidate overlaps,
   split overgrown skills, archive stale ones. Never deletes.

Usage as a hook (stdin carries the payload):
    curator.py --check

Usage by hand:
    curator.py --run           # run now regardless of interval
    curator.py --sweep-only
    curator.py --status
    curator.py --pause / --resume
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard  # noqa: E402
import review as review_mod  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

DEFAULT_INTERVAL_HOURS = 24 * 7  # matches Hermes DEFAULT_INTERVAL_HOURS
MODEL = os.environ.get("CLAUDE_SELF_IMPROVE_MODEL", "sonnet")
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_SELF_IMPROVE_CURATOR_TIMEOUT", "900"))

#: Transcripts older than this are never swept. A months-old session is not a
#: missed lesson, it is history.
SWEEP_MAX_AGE_DAYS = 14

#: Do not sweep a transcript still being written to.
SWEEP_MIN_IDLE_MINUTES = 30


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def read_state() -> Dict[str, Any]:
    return review_mod.read_state()


def write_state(state: Dict[str, Any]) -> None:
    review_mod.write_state(state)


def interval_hours(state: Dict[str, Any]) -> float:
    try:
        return float(state.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def due(state: Dict[str, Any]) -> bool:
    if state.get("paused"):
        return False
    last = state.get("last_curator_run")
    if not last:
        return True
    try:
        previous = dt.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.timezone.utc)
    return now() - previous >= dt.timedelta(hours=interval_hours(state))


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def stale_transcripts(state: Dict[str, Any]) -> List[Path]:
    """Transcripts modified since their last review, old enough to be finished."""
    watermarks = state.get("watermarks", {}) or {}
    if not guard.PROJECTS_DIR.is_dir():
        return []

    cutoff_old = now() - dt.timedelta(days=SWEEP_MAX_AGE_DAYS)
    cutoff_idle = now() - dt.timedelta(minutes=SWEEP_MIN_IDLE_MINUTES)
    found = []
    for path in guard.PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        modified = dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
        if modified < cutoff_old or modified > cutoff_idle:
            continue
        if float(watermarks.get(path.stem, 0)) >= mtime:
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def sweep(state: Dict[str, Any], limit: int = 10) -> int:
    """Review transcripts whose SessionEnd hook never fired."""
    pending = stale_transcripts(state)
    if not pending:
        return 0
    if len(pending) > limit:
        # Never silently truncate: a capped sweep that reports nothing reads as
        # "everything covered" when it is not.
        review_mod.log(
            {
                "event": "sweep-capped",
                "pending": len(pending),
                "limit": limit,
                "deferred": [p.stem for p in pending[limit:]],
            }
        )
        pending = pending[:limit]

    reviewed = 0
    for path in pending:
        cwd = _transcript_cwd(path) or str(Path.home())
        try:
            review_mod.review(path, path.stem, cwd)
            reviewed += 1
        except Exception as exc:  # noqa: BLE001
            review_mod.log({"event": "error", "reason": repr(exc), "session": path.stem})
    review_mod.log({"event": "sweep", "reviewed": reviewed, "pending": len(pending)})
    return reviewed


def _transcript_cwd(path: Path) -> Optional[str]:
    """First cwd recorded in a transcript, without parsing the whole file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("cwd"):
                    return str(record["cwd"])
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Curate
# ---------------------------------------------------------------------------


def inventory() -> str:
    skills = guard.writable_skills()
    if not skills:
        return "(none — the library is empty, nothing to curate)"
    lines = []
    for path in skills:
        try:
            text = path.read_text(encoding="utf-8")
            age_days = (now() - dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc
            )).days
        except OSError:
            continue
        frontmatter = guard.parse_frontmatter(text)
        description = str(frontmatter.get("description", "")).strip()
        extras = sorted(
            p.relative_to(path.parent).as_posix()
            for p in path.parent.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        lines.append(
            f"- {path.parent.name} ({len(text)} chars, {age_days}d since edit"
            + (f", support files: {', '.join(extras)}" if extras else "")
            + f")\n    {description[:200]}"
        )
    return "\n".join(lines)


def build_prompt() -> str:
    template = (PROMPTS_DIR / "curator.md").read_text(encoding="utf-8")
    return template.replace("{inventory}", inventory()).replace(
        "{skills_dir}", str(guard.SKILLS_DIR)
    )


def run_fork(prompt: str) -> subprocess.CompletedProcess:
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
        "40",
        "--add-dir",
        str(guard.SKILLS_DIR),
        "--allowedTools",
        *review_mod.ALLOWED_TOOLS,
        "--disallowedTools",
        *review_mod.DENIED_TOOLS,
    ]
    return subprocess.run(
        command,
        cwd=str(guard.SKILLS_DIR),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=guard.child_env(),
        check=False,
    )


def curate() -> int:
    if not guard.writable_skills():
        review_mod.log({"event": "curate", "skipped": "empty library"})
        return 0

    guard.ensure_skills_repo()
    try:
        result = run_fork(build_prompt())
    except subprocess.TimeoutExpired:
        review_mod.log({"event": "curate-timeout", "seconds": TIMEOUT_SECONDS})
        return 0
    except FileNotFoundError:
        review_mod.log({"event": "error", "reason": "claude binary not found"})
        return 0

    # The curator writes only inside the skills tree, so cwd here is irrelevant
    # to the check -- pass home so the memory root never widens the allowlist.
    violations = guard.verify_writes(str(Path.home()))
    reply = (result.stdout or "").strip()
    sha = guard.commit(f"curator: library maintenance\n\n{reply[:500]}")
    review_mod.log(
        {
            "event": "curate",
            "exit": result.returncode,
            "commit": sha,
            "violations": violations,
            "reply": reply[:2000],
            "stderr": (result.stderr or "")[:1000] if result.returncode else "",
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run(sweep_only: bool = False) -> int:
    with guard.lock("curator", timeout=1.0) as acquired:
        if not acquired:
            review_mod.log({"event": "curate-deferred", "reason": "lock held"})
            return 0
        state = read_state()
        sweep(state)
        if not sweep_only:
            curate()
        state = read_state()  # sweep advanced watermarks; re-read before stamping
        state["last_curator_run"] = now().isoformat()
        state["run_count"] = int(state.get("run_count", 0)) + 1
        write_state(state)
    return 0


def spawn_detached() -> None:
    """Re-invoke ourselves with --run, fully detached.

    The SessionStart hook must return in milliseconds. Everything real happens
    in a process that outlives it.
    """
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def show_status() -> int:
    state = read_state()
    pending = stale_transcripts(state)
    print(f"Paused:            {bool(state.get('paused'))}")
    print(f"Interval (hours):  {interval_hours(state)}")
    print(f"Last curator run:  {state.get('last_curator_run', 'never')}")
    print(f"Run count:         {state.get('run_count', 0)}")
    print(f"Due now:           {due(state)}")
    print(f"Transcripts pending sweep: {len(pending)}")
    for path in pending[:10]:
        print(f"  {path.stem}  {path.parent.name}")
    print()
    return review_mod.show_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="hook mode: fork if due")
    parser.add_argument("--run", action="store_true", help="run now, in this process")
    parser.add_argument("--sweep-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if guard.is_child():
        return 0

    if args.status:
        return show_status()
    if args.pause or args.resume:
        state = read_state()
        state["paused"] = bool(args.pause)
        write_state(state)
        print("paused" if args.pause else "resumed")
        return 0
    if args.run or args.sweep_only:
        return run(sweep_only=args.sweep_only)

    if args.check:
        # Hook mode. Drain stdin so the caller never blocks on a full pipe.
        try:
            sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            if due(read_state()):
                spawn_detached()
        except Exception as exc:  # noqa: BLE001
            review_mod.log({"event": "error", "reason": repr(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
