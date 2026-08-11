#!/usr/bin/env python3
"""SessionStart entry point — interval-gated sweep and library maintenance.

Ports Hermes' no-daemon design: nothing is scheduled. Every session start runs a
fast check (a state file read, no subprocess); if the interval has elapsed, the
real work is forked detached and this process returns immediately so the user's
session is never held up.

Two jobs on two intervals:

1. **Sweep** — daily. Review transcripts that finished without their SessionEnd
   hook firing (hard kill, terminal closed, crash). This is what makes
   SessionEnd sufficient rather than merely usual, and it only holds if the
   sweep runs often enough to outpace the sessions it is covering for.
2. **Curate** — weekly. Maintenance over the autoManaged library: consolidate
   overlaps, split overgrown skills, archive stale ones. Never deletes.

Usage as a hook (stdin carries the payload):
    curator.py --check

Usage by hand:
    curator.py --run           # run now regardless of interval
    curator.py --curate-only   # curate without sweeping (rehearsals)
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
import pending  # noqa: E402
import review as review_mod  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

DEFAULT_INTERVAL_HOURS = 24 * 7  # matches Hermes DEFAULT_INTERVAL_HOURS
MODEL = guard.env("MODEL", "sonnet")
TIMEOUT_SECONDS = int(guard.env("CURATOR_TIMEOUT", "900"))

#: Transcripts older than this are never swept. A months-old session is not a
#: missed lesson, it is history.
SWEEP_MAX_AGE_DAYS = 14

#: Do not sweep a transcript still being written to.
SWEEP_MIN_IDLE_MINUTES = 30

#: The sweep runs on its own, much shorter interval than the curate pass.
#: Sharing the weekly interval capped coverage at SWEEP_LIMIT transcripts a
#: week, which is slower than sessions accumulate: the backlog grew until
#: transcripts aged past SWEEP_MAX_AGE_DAYS and were dropped unread. Daily, the
#: same per-run cap clears seven times as many while keeping each burst small.
DEFAULT_SWEEP_INTERVAL_HOURS = 24

#: Forks per sweep. Each is a real model call, so this is a spend ceiling as
#: much as a batch size; state key ``sweep_limit`` overrides it.
DEFAULT_SWEEP_LIMIT = 10


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


def sweep_interval_hours(state: Dict[str, Any]) -> float:
    try:
        return float(state.get("sweep_interval_hours", DEFAULT_SWEEP_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_SWEEP_INTERVAL_HOURS


def sweep_limit(state: Dict[str, Any]) -> int:
    try:
        return max(1, int(state.get("sweep_limit", DEFAULT_SWEEP_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_SWEEP_LIMIT


def _elapsed(state: Dict[str, Any], key: str, hours: float) -> bool:
    if state.get("paused"):
        return False
    last = state.get(key)
    if not last:
        return True
    try:
        previous = dt.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.timezone.utc)
    return now() - previous >= dt.timedelta(hours=hours)


def due(state: Dict[str, Any]) -> bool:
    return _elapsed(state, "last_curator_run", interval_hours(state))


def sweep_due(state: Dict[str, Any]) -> bool:
    """Whether the backstop sweep should run, independently of curation."""
    return _elapsed(state, "last_sweep_run", sweep_interval_hours(state))


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
        # The SessionEnd path is protected by the env sentinel; the sweep finds
        # transcripts by glob and has to recognise the loop's own forks itself.
        if guard.is_own_transcript(path):
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def sweep(state: Dict[str, Any], limit: Optional[int] = None) -> int:
    """Review transcripts whose SessionEnd hook never fired."""
    limit = sweep_limit(state) if limit is None else limit
    queue = stale_transcripts(state)
    if not queue:
        return 0
    if len(queue) > limit:
        # Never silently truncate: a capped sweep that reports nothing reads as
        # "everything covered" when it is not.
        review_mod.log(
            {
                "event": "sweep-capped",
                "pending": len(queue),
                "limit": limit,
                "deferred": [p.stem for p in queue[limit:]],
            }
        )
        queue = queue[:limit]

    reviewed = 0
    for path in queue:
        cwd = _transcript_cwd(path) or str(Path.home())
        try:
            review_mod.review(path, path.stem, cwd)
            reviewed += 1
        except Exception as exc:  # noqa: BLE001
            review_mod.log({"event": "error", "reason": repr(exc), "session": path.stem})
    review_mod.log({"event": "sweep", "reviewed": reviewed, "pending": len(queue)})
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


def _usage_note(usage: Dict[str, Any], skill: str) -> str:
    """How often this skill has actually been loaded.

    The curator's archive decision used to rest on file age alone, which cannot
    tell a skill nobody needs from one that quietly does its job every month.
    """
    row = usage.get(skill) or {}
    count = int(row.get("count", 0) or 0)
    if not count:
        return "never used"
    last = str(row.get("last_used") or "")[:10]
    return f"used {count}x, last {last}" if last else f"used {count}x"


def inventory() -> str:
    skills = guard.writable_skills()
    if not skills:
        return "(none — the library is empty, nothing to curate)"
    usage = read_state().get("usage", {}) or {}
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
            f"- {path.parent.name} ({len(text)} chars, {age_days}d since edit, "
            + _usage_note(usage, path.parent.name)
            + (f", support files: {', '.join(extras)}" if extras else "")
            + f")\n    {description[:200]}"
        )
    return "\n".join(lines)


def build_prompt() -> str:
    template = (PROMPTS_DIR / "curator.md").read_text(encoding="utf-8")
    return guard.FORK_MARKER + "\n\n" + template.replace(
        "{inventory}", inventory()
    ).replace("{skills_dir}", str(guard.WORK_DIR))


#: The curator's report. Structured so ``--status`` can say what it did rather
#: than quoting a paragraph of prose at the reader.
ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["consolidated", "split", "archived", "rewrote"],
                    },
                    "skill": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "skill", "reason"],
            },
        },
        "largest_risk": {"type": "string"},
    },
    "required": ["actions"],
}


def run_fork(prompt: str) -> subprocess.CompletedProcess:
    command = guard.fork_command(
        prompt,
        model=MODEL,
        max_turns=40,
        add_dirs=[guard.WORK_DIR],
        schema=ACTIONS_SCHEMA,
    )
    return subprocess.run(
        command,
        cwd=str(guard.WORK_DIR),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=guard.child_env(),
        check=False,
    )


def _describe_actions(actions: List[Dict[str, Any]], risk: str) -> str:
    if not actions:
        return f"Library is healthy — no action taken. Largest risk: {risk or 'none named'}"
    return "; ".join(
        f"{a.get('kind', '?')} {a.get('skill', '?')}: {a.get('reason', '')}"
        for a in actions
    )


def curate() -> int:
    if not guard.writable_skills():
        review_mod.log({"event": "curate", "skipped": "empty library"})
        return 0

    guard.ensure_skills_repo()
    pending.prepare_work_tree()
    try:
        result = run_fork(build_prompt())
    except subprocess.TimeoutExpired:
        review_mod.log({"event": "curate-timeout", "seconds": TIMEOUT_SECONDS})
        return 0
    except FileNotFoundError:
        review_mod.log({"event": "error", "reason": "claude binary not found"})
        return 0

    violations = guard.verify_writes()
    outcome = guard.parse_fork_result(result.stdout)
    report = outcome.structured if isinstance(outcome.structured, dict) else {}
    actions = report.get("actions") if isinstance(report.get("actions"), list) else []
    # Prefer the rendered description over the raw text: with a schema in play
    # the text *is* the JSON, and a queue entry whose reason reads as a wall of
    # escaped braces is a queue entry nobody reviews.
    reply = (
        _describe_actions(actions, str(report.get("largest_risk") or ""))
        if report
        else outcome.text
    )
    # Consolidations and archivals go through the same review queue as
    # everything else. A curator that could silently merge two skills would be
    # a bigger hole than the one the queue was built to close.
    queued = pending.capture(f"curator-{now().date().isoformat()}", summary=reply[:500])
    review_mod.log(
        {
            "event": "curate",
            "exit": result.returncode,
            "cost_usd": outcome.cost_usd,
            "actions": actions,
            "largest_risk": str(report.get("largest_risk") or "")[:500],
            "queued": queued,
            "violations": violations,
            "reply": reply[:2000],
            "stderr": (result.stderr or "")[:1000] if result.returncode else "",
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run(sweep_only: bool = False, curate_only: bool = False) -> int:
    """Sweep, curate, or both.

    ``curate_only`` exists for rehearsals. The sweep reads whatever
    ``PATINA_PROJECTS_DIR`` points at, which defaults to the real
    ``~/.claude/projects`` even when the skills and state directories have been
    redirected -- so a rehearsal meaning to exercise the curator forks a batch
    of real reviews before it gets there.
    """
    with guard.lock("curator", timeout=1.0) as acquired:
        if not acquired:
            review_mod.log({"event": "curate-deferred", "reason": "lock held"})
            return 0
        state = read_state()
        if not curate_only:
            sweep(state)
        if not sweep_only:
            curate()
        state = read_state()  # sweep advanced watermarks; re-read before stamping
        if not curate_only:
            state["last_sweep_run"] = now().isoformat()
        # A sweep-only run must not stamp the curate clock. Stamping it would
        # push curation out by a full interval every day, so it would never
        # run again.
        if not sweep_only:
            state["last_curator_run"] = now().isoformat()
            state["run_count"] = int(state.get("run_count", 0)) + 1
        write_state(state)
    return 0


def spawn_detached(sweep_only: bool = False) -> None:
    """Re-invoke ourselves with --run, fully detached.

    The SessionStart hook must return in milliseconds. Everything real happens
    in a process that outlives it.
    """
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--sweep-only" if sweep_only else "--run",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def _last_curate_entry() -> Optional[Dict[str, Any]]:
    if not review_mod.AUDIT_LOG.exists():
        return None
    found = None
    for line in review_mod.AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "curate" and "skipped" not in entry:
            found = entry
    return found


def show_status() -> int:
    state = read_state()
    queue = stale_transcripts(state)
    print(f"Paused:            {bool(state.get('paused'))}")
    print(f"Interval (hours):  {interval_hours(state)}")
    print(f"Last curator run:  {state.get('last_curator_run', 'never')}")
    print(f"Run count:         {state.get('run_count', 0)}")
    print(f"Due now:           {due(state)}")
    print()
    print(f"Sweep interval:    {sweep_interval_hours(state)}h, "
          f"up to {sweep_limit(state)} per run")
    print(f"Last sweep run:    {state.get('last_sweep_run', 'never')}")
    print(f"Sweep due now:     {sweep_due(state)}")
    print(f"Transcripts pending sweep: {len(queue)}")
    for path in queue[:10]:
        print(f"  {path.stem}  {path.parent.name}")

    last = _last_curate_entry()
    if last:
        actions = last.get("actions") or []
        print(f"\nLast curation:     {len(actions)} action(s)")
        for action in actions[:10]:
            print(f"  {action.get('kind', '?')} {action.get('skill', '?')}"
                  f" — {action.get('reason', '')[:100]}")
        if not actions and last.get("largest_risk"):
            print(f"  healthy; largest risk noted: {last['largest_risk'][:120]}")
    print()
    return review_mod.show_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="hook mode: fork if due")
    parser.add_argument("--run", action="store_true", help="run now, in this process")
    parser.add_argument("--sweep-only", action="store_true",
                        help="catch up on missed sessions, skip curation")
    parser.add_argument("--curate-only", action="store_true",
                        help="curate now, do not sweep (what a rehearsal wants)")
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
    if args.run or args.sweep_only or args.curate_only:
        return run(sweep_only=args.sweep_only, curate_only=args.curate_only)

    if args.check:
        # Hook mode. Drain stdin so the caller never blocks on a full pipe.
        try:
            sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            state = read_state()
            # The full run sweeps too, so never fork both.
            if due(state):
                spawn_detached()
            elif sweep_due(state):
                spawn_detached(sweep_only=True)
        except Exception as exc:  # noqa: BLE001
            review_mod.log({"event": "error", "reason": repr(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
