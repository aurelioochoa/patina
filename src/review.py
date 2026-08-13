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
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest as digest_mod  # noqa: E402
import guard  # noqa: E402
import pending  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
AUDIT_LOG = guard.STATE_DIR / "audit.jsonl"
STATE_FILE = guard.STATE_DIR / "state.json"

MODEL = guard.env("MODEL", "sonnet")
TIMEOUT_SECONDS = int(guard.env("TIMEOUT", "600"))
MAX_TURNS = 30

#: How many times a transcript may fail before the loop stops retrying it.
#: A failed review must be retried -- the first real failure in the wild was an
#: account limit, which is transient by definition, and marking that session
#: reviewed lost it permanently. But retrying forever is its own failure mode: a
#: digest that reliably breaks the fork would burn one fork per sweep until it
#: ages out. Three attempts, then let it go, loudly.
MAX_ATTEMPTS = 3

#: Below both of these, a session is not reviewed at all.
#:
#: The review prompt pushes hard to find something worth keeping, which is what
#: stops it defaulting to "Nothing to save." on sessions that did have a lesson.
#: Aimed at a three-message session, that same pressure manufactures one. The
#: gate is what makes the pressure safe: it applies only to sessions that did
#: enough to have produced a lesson.
#:
#: Either signal alone is enough. A long conversation that touched no tools is
#: exactly where preferences and corrections live; a heavy tool session with two
#: user turns is where techniques live.
DEFAULT_MIN_TOOL_CALLS = 8
DEFAULT_MIN_USER_TURNS = 5


def substance_gate(state: Dict[str, Any]) -> tuple:
    def read(key: str, fallback: int) -> int:
        try:
            return max(0, int(state.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    return (
        read("min_tool_calls", DEFAULT_MIN_TOOL_CALLS),
        read("min_user_turns", DEFAULT_MIN_USER_TURNS),
    )


def is_thin(built: "digest_mod.Digest", state: Dict[str, Any]) -> bool:
    min_tools, min_turns = substance_gate(state)
    return built.tool_calls < min_tools and built.user_turns < min_turns


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


def _mtime(transcript: Path) -> Optional[float]:
    try:
        return transcript.stat().st_mtime
    except OSError:
        return None


def mark_reviewed(session_id: str, transcript: Path) -> None:
    """Record that this transcript is done. Only ever called after a clean run."""
    stamp = _mtime(transcript)
    if stamp is None:
        return
    state = read_state()
    state.setdefault("watermarks", {})[session_id] = stamp
    state.get("attempts", {}).pop(session_id, None)
    write_state(state)


def record_usage(session_id: str, transcript: Path, skills: list) -> None:
    """Count the skills a session loaded.

    The only feedback this loop has. Without it the curator ages skills on
    wall-clock alone, which cannot tell a skill nobody needs from one that
    quietly does its job every week, and ``--status`` can report how much was
    written but never whether any of it was used.

    Recorded for every session, including ones too thin to review -- using a
    skill is evidence regardless of whether the session taught us anything.
    """
    if not skills:
        return
    state = read_state()
    stamp = _mtime(transcript)
    seen = state.setdefault("usage_seen", {})
    if stamp is not None and seen.get(session_id) == stamp:
        return  # already counted; a re-swept transcript must not count twice
    usage = state.setdefault("usage", {})
    stamped = now()
    for name in skills:
        row = usage.setdefault(name, {"count": 0, "last_used": None})
        row["count"] = int(row.get("count", 0)) + 1
        row["last_used"] = stamped
    if stamp is not None:
        seen[session_id] = stamp
    write_state(state)


def mark_failed(
    session_id: str, transcript: Path, reason: str, terminal: bool = False
) -> bool:
    """Count a failed review, leaving the transcript for the sweep to retry.

    Returns True when the attempt budget is spent and the transcript has been
    watermarked to stop the retries.

    ``terminal`` spends the whole budget at once, for a failure where the next
    attempt is guaranteed to fail the same way -- a spend ceiling below what
    this transcript costs is the case in the wild. Retrying that is not
    resilience, it is paying the ceiling twice more to learn what the first run
    already established.
    """
    state = read_state()
    attempts = state.setdefault("attempts", {})
    count = MAX_ATTEMPTS if terminal else int(attempts.get(session_id, 0) or 0) + 1
    attempts[session_id] = count

    exhausted = count >= MAX_ATTEMPTS
    if exhausted:
        attempts.pop(session_id, None)
        stamp = _mtime(transcript)
        if stamp is not None:
            state.setdefault("watermarks", {})[session_id] = stamp
        log(
            {
                "event": "gave-up",
                "session": session_id,
                "reason": reason,
                "attempts": count,
            }
        )
    write_state(state)
    return exhausted


def _describe(path: Path, suffix: str = "") -> str:
    try:
        frontmatter = guard.parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    description = str(frontmatter.get("description", "")).strip()
    return f"- {path.parent.name}: {description[:160]}{suffix}"


#: Ceiling on either half of the skill listing in a prompt. A backlog is
#: exactly when this matters -- an unreviewed queue is unbounded, and a prompt
#: that grows a line per proposal quietly raises the price of every review.
MAX_LISTED_SKILLS = 40


def _listing(lines: List[str]) -> str:
    if len(lines) <= MAX_LISTED_SKILLS:
        return "\n".join(lines)
    hidden = len(lines) - MAX_LISTED_SKILLS
    return "\n".join(lines[:MAX_LISTED_SKILLS]) + (
        f"\n- ...and {hidden} more not shown. The list is truncated, so a name "
        "missing from it is not proof the skill does not exist."
    )


def describe_writable() -> str:
    """The library the fork is working against: what is live, and what is queued.

    The queued half is not decoration. A proposal waiting for review is already
    in the fork's work tree, and a fork that cannot see it listed will read the
    live library, find nothing that covers this session's lesson, and create a
    sibling of the skill it should have extended. That is how one library ends
    up with six overlapping git skills, none of them approved.
    """
    live = [text for text in (_describe(p) for p in guard.writable_skills()) if text]
    queued = []
    for skill, entry in sorted(pending.proposals().items()):
        path = guard.PENDING_DIR / entry["id"] / "after" / skill / "SKILL.md"
        session = str(entry.get("session") or "")[:8]
        text = _describe(path, suffix=f"  [queued, from session {session}]")
        if text:
            queued.append(text)

    blocks = []
    if live:
        blocks.append("In the library, approved:\n" + _listing(live))
    if queued:
        blocks.append(
            "Proposed by an earlier session and waiting for the author's "
            "review. These are already in your working copy and are yours to "
            "extend — a lesson that belongs in one of them belongs THERE, not "
            "in a new skill beside it:\n" + _listing(queued)
        )
    if not blocks:
        return (
            "(none yet — the library is empty and nothing is queued. Create the "
            "first class-level skill if this session earned one.)"
        )
    return "\n\n".join(blocks)


#: What the reflect pass must return. Structured output is not a formatting
#: nicety here: it is what lets the loop branch on "did this session teach
#: anything" without grepping prose for a phrase, and what lets each queued
#: proposal carry the claim and evidence that motivated it.
LESSONS_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "technique", "correction", "pitfall"],
                    },
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "suggested_target": {"type": "string"},
                },
                "required": ["kind", "claim", "evidence", "confidence"],
            },
        },
        "note": {"type": "string"},
    },
    "required": ["lessons"],
}

#: The reflect pass has no tools and one thing to say.
REFLECT_MAX_TURNS = 3


def build_reflect_prompt(digest_text: str, session_id: str) -> str:
    template = (PROMPTS_DIR / "reflect.md").read_text(encoding="utf-8")
    return guard.FORK_MARKER + "\n\n" + (
        template.replace("{writable_skills}", describe_writable())
        .replace("{session_id}", session_id or "unknown")
        .replace("{digest}", digest_text)
    )


def render_lessons(lessons: list) -> str:
    if not lessons:
        return "(none)"
    blocks = []
    for index, lesson in enumerate(lessons, start=1):
        target = str(lesson.get("suggested_target") or "").strip()
        blocks.append(
            f"{index}. [{lesson.get('kind', '?')}, "
            f"confidence {lesson.get('confidence', '?')}] "
            f"{lesson.get('claim', '').strip()}\n"
            f"   Evidence: {str(lesson.get('evidence', '')).strip()}\n"
            f"   Suggested home: {target or '(none suggested)'}"
        )
    return "\n\n".join(blocks)


def build_place_prompt(lessons: list, session_id: str, note: str = "") -> str:
    template = (PROMPTS_DIR / "place.md").read_text(encoding="utf-8")
    rendered = render_lessons(lessons)
    if note:
        rendered += f"\n\nFrom the first pass: {note.strip()}"
    return guard.FORK_MARKER + "\n\n" + (
        # The fork is pointed at the scratch copy, never the live library. It is
        # told so explicitly: a skill author who thinks their edit ships
        # immediately writes differently from one who knows it faces review.
        template.replace("{skills_dir}", str(guard.WORK_DIR))
        .replace("{writable_skills}", describe_writable())
        .replace("{session_id}", session_id or "unknown")
        .replace("{lessons}", rendered)
    )


def extract_lessons(outcome: "guard.ForkResult") -> tuple:
    """Pull the lesson list out of whatever the reflect fork returned.

    Structured output is requested, so the normal path is a dict. The fallback
    exists because a model told to emit JSON sometimes emits JSON in prose, and
    losing a whole review to a stray code fence is a worse failure than parsing
    loosely.
    """
    payload = outcome.structured
    if not isinstance(payload, dict):
        text = outcome.text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[-1] if "\n" in text else text
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return [], ""
    if not isinstance(payload, dict):
        return [], ""
    lessons = payload.get("lessons")
    if not isinstance(lessons, list):
        return [], str(payload.get("note") or "")
    clean = [
        lesson
        for lesson in lessons
        if isinstance(lesson, dict) and str(lesson.get("claim") or "").strip()
    ]
    return clean, str(payload.get("note") or "")


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
    queued = pending.proposals()
    for path in guard.WORK_DIR.rglob("SKILL.md"):
        skill = path.parent.name
        if (guard.SKILLS_DIR / skill / "SKILL.md").exists():
            continue  # a patch, not a creation -- leave its frontmatter alone
        if skill in queued:
            # Seeded from the queue, so it was created by an earlier session and
            # already carries that session's id. Restamping it would both
            # misattribute the skill and, because the bytes changed, re-file an
            # untouched proposal under whichever session ran last.
            continue
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


def run_fork(
    prompt: str,
    *,
    tools: Optional[list] = None,
    schema: Optional[Dict[str, Any]] = None,
    max_turns: int = MAX_TURNS,
) -> subprocess.CompletedProcess:
    """Spawn the restricted headless child.

    ``--add-dir`` is a harness-level boundary and the first line of defence;
    ``guard.verify_writes`` afterwards is the one that actually decides.
    """
    work = guard.WORK_DIR
    command = guard.fork_command(
        prompt,
        model=MODEL,
        max_turns=max_turns,
        add_dirs=[work] if tools != [] else [],
        tools=tools,
        schema=schema,
    )
    return subprocess.run(
        command,
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=guard.child_env(),
        check=False,
    )


def _log_budget(session_id: str, returncode: int, outcome: "guard.ForkResult") -> None:
    """Record a fork that stopped at the spend ceiling rather than crashing.

    Retrying spends two more forks on a number that is too low, so this needs to
    be visible in --status as its own thing, not filed as an unexplained failure.
    """
    if returncode != 0 and outcome.hit_budget:
        log({
            "event": "budget-exhausted",
            "session": session_id,
            "limit_usd": guard.MAX_BUDGET_USD,
        })


def _record_failure(
    session_id: str,
    transcript: Path,
    outcome: "guard.ForkResult",
    reason: str,
) -> None:
    """Decide what a failed fork costs the transcript's retry budget.

    Three failures that look identical in the exit code and are not:

    - **Rate limited.** The account is out of usage for now. Nothing about this
      transcript caused it and nothing about it will be different next time, so
      it must not spend an attempt -- the first real failure in the wild was
      exactly this, and counting it as an error is how a reviewable session gets
      thrown away. The watermark stays put and the sweep comes back to it.
    - **Budget ceiling.** This transcript costs more to review than the ceiling
      allows. Deterministic, so retrying spends the ceiling twice more for the
      same outcome. Given up on immediately, loudly, with the ceiling named.
    - **Anything else.** An ordinary attempt, retried up to ``MAX_ATTEMPTS``.
    """
    if outcome.hit_rate_limit:
        log({"event": "rate-limited", "session": session_id, "reason": reason})
        return
    mark_failed(session_id, transcript, reason, terminal=outcome.hit_budget)


def _total_cost(*outcomes) -> Optional[float]:
    known = [o.cost_usd for o in outcomes if o and o.cost_usd is not None]
    return sum(known) if known else None


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
    state = read_state()
    thin = is_thin(built, state)

    if dry_run:
        pending.prepare_work_tree()
        example = [{
            "kind": "preference",
            "claim": "(what the first pass distils goes here)",
            "evidence": "(a quote from the session)",
            "confidence": "high",
            "suggested_target": "",
        }]
        print("=" * 72 + "\n=== PASS 1: reflect (no tools)\n" + "=" * 72 + "\n")
        print(build_reflect_prompt(built.text, session_id))
        print("\n" + "=" * 72 + "\n=== PASS 2: place (runs only if pass 1 found "
              "anything)\n" + "=" * 72 + "\n")
        print(build_place_prompt(example, session_id))
        min_tools, min_turns = substance_gate(state)
        print(
            f"\n--- gate: tool_calls={built.tool_calls} (min {min_tools}) "
            f"user_turns={built.user_turns} (min {min_turns}) -> "
            f"{'SKIP, too thin' if thin else 'review'}\n"
            f"--- would fork: model={MODEL} messages={built.message_count} "
            f"truncated={built.truncated}",
            file=sys.stderr,
        )
        return 0

    record_usage(session_id, transcript, built.skills_loaded)

    if thin:
        log({
            "event": "skipped",
            "reason": "thin session",
            "session": session_id,
            "tool_calls": built.tool_calls,
            "user_turns": built.user_turns,
        })
        # Watermarked: the session was considered and found too small. Leaving
        # it unmarked would have the sweep reconsider it every day until it
        # aged out, reaching the same conclusion each time.
        mark_reviewed(session_id, transcript)
        return 0

    pending.prepare_work_tree()

    with guard.lock(f"review-{guard.project_slug(cwd)}") as acquired:
        if not acquired:
            # Another session is reviewing the same project. Leave the watermark
            # unadvanced so the sweep retries this transcript later.
            log({"event": "deferred", "reason": "lock held", "session": session_id})
            return 0

        guard.ensure_skills_repo()
        started = now()

        # Pass 1: read the session, distil lessons, touch nothing. No tools and
        # no --add-dir, so the pass that sees raw transcript text -- web pages,
        # file contents, command output, anything a session happened to read --
        # has no way to act on it. Only its structured findings go forward.
        try:
            first = run_fork(
                build_reflect_prompt(built.text, session_id),
                tools=[],
                schema=LESSONS_SCHEMA,
                max_turns=REFLECT_MAX_TURNS,
            )
        except subprocess.TimeoutExpired:
            log({"event": "timeout", "session": session_id, "pass": "reflect",
                 "seconds": TIMEOUT_SECONDS})
            mark_failed(session_id, transcript, "timeout")
            return 0
        except FileNotFoundError:
            log({"event": "error", "reason": "claude binary not found"})
            return 0
        except ValueError as exc:
            # subprocess refuses an argv element it cannot pass to exec. The one
            # that happens in practice is a NUL byte in the digest; digest.clean
            # strips those now, so reaching this means a new shape of unpassable
            # prompt. Retrying it would fail identically.
            log({"event": "error", "reason": repr(exc), "session": session_id,
                 "pass": "reflect"})
            mark_failed(session_id, transcript, "unpassable prompt", terminal=True)
            return 0

        reflected = guard.parse_fork_result(first.stdout)
        _log_budget(session_id, first.returncode, reflected)
        if first.returncode != 0:
            log({"event": "reflect-failed", "session": session_id,
                 "exit": first.returncode, "cost_usd": reflected.cost_usd,
                 "stderr": (first.stderr or "")[:1000]})
            _record_failure(
                session_id, transcript, reflected, f"reflect exit {first.returncode}"
            )
            return 0

        lessons, note = extract_lessons(reflected)
        if reflected.structured is None:
            # The schema asked for structured output and the run succeeded, so
            # its absence means the contract changed under us. Without this the
            # symptom is every session reporting "nothing to save" forever --
            # a loop that has silently stopped learning looks exactly like a
            # library with nothing left to learn.
            log({
                "event": "reflect-unstructured",
                "session": session_id,
                "recovered": bool(lessons),
            })

        if not lessons:
            # The session taught nothing. This is the common case and it costs
            # one cheap tool-less pass, not a full write-capable fork.
            log({
                "event": "review",
                "session": session_id,
                "cwd": cwd,
                "started": started,
                "model": MODEL,
                "messages": built.message_count,
                "digest_chars": len(built.text),
                "truncated": built.truncated,
                "exit": 0,
                "cost_usd": reflected.cost_usd,
                "lessons": 0,
                "queued": [],
                "violations": [],
                "reply": (note or "Nothing to save.")[:2000],
            })
            mark_reviewed(session_id, transcript)
            return 0

        # Pass 2: place the lessons. This one can write, and never sees the
        # transcript.
        try:
            second = run_fork(build_place_prompt(lessons, session_id, note))
        except subprocess.TimeoutExpired:
            log({"event": "timeout", "session": session_id, "pass": "place",
                 "seconds": TIMEOUT_SECONDS})
            mark_failed(session_id, transcript, "timeout")
            return 0
        except FileNotFoundError:
            log({"event": "error", "reason": "claude binary not found"})
            return 0
        except ValueError as exc:
            log({"event": "error", "reason": repr(exc), "session": session_id,
                 "pass": "place"})
            mark_failed(session_id, transcript, "unpassable prompt", terminal=True)
            return 0

        result = second
        outcome = guard.parse_fork_result(second.stdout)
        _log_budget(session_id, second.returncode, outcome)

        # The fork never saw SKILLS_DIR, so there is nothing to revert there.
        # verify_writes stays as a cheap assertion that this remains true.
        violations = guard.verify_writes()
        reply = outcome.text
        normalize_new_skills(session_id)
        queued = pending.capture(session_id, summary=reply[:500], claims=lessons)
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
                "cost_usd": _total_cost(reflected, outcome),
                "lessons": len(lessons),
                "commit": sha,
                "queued": queued,
                "violations": violations,
                "reply": reply[:2000],
                "stderr": (result.stderr or "")[:1000] if result.returncode else "",
            }
        )

        # Only a clean run counts as reviewed. A fork that died -- an account
        # limit, a crash -- leaves the watermark alone so the sweep comes back
        # to it, up to MAX_ATTEMPTS.
        if result.returncode == 0:
            mark_reviewed(session_id, transcript)
        else:
            _record_failure(
                session_id, transcript, outcome, f"place exit {result.returncode}"
            )
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

    def within_30_days(entry: Dict[str, Any]) -> bool:
        stamp = _parse_time(entry.get("at"))
        return bool(stamp and stamp > cutoff)

    recent = [e for e in reviews if within_30_days(e)]
    violations = [e for e in reviews if e.get("violations")]
    spend = sum(
        float(e["cost_usd"])
        for e in entries
        if within_30_days(e) and isinstance(e.get("cost_usd"), (int, float))
    )
    budget_stops = [e for e in entries if e.get("event") == "budget-exhausted"]

    print(f"Total reviews:        {len(reviews)}")
    print(f"Last 30 days:         {len(recent)}")
    print(f"Last run:             {reviews[-1]['at'] if reviews else 'never'}")
    print(f"Spend, last 30 days:  ${spend:.2f} (ceiling ${guard.MAX_BUDGET_USD}/fork)")
    print(f"Runs with violations: {len(violations)}")
    if violations:
        print("\nAllowlist violations (reverted):")
        for entry in violations[-5:]:
            for path in entry["violations"]:
                print(f"  {entry['at']}  {path}")
    if budget_stops:
        print(
            f"\n{len(budget_stops)} run(s) stopped at the spend ceiling. "
            "Raise PATINA_MAX_USD to review those sessions -- they are not "
            "retried,\nbecause the next attempt would stop at the same number."
        )
    limited = [e for e in entries if e.get("event") == "rate-limited"]
    if limited:
        print(
            f"\n{len(limited)} run(s) hit an account usage limit. Those "
            "transcripts kept their place in\nthe queue and will be retried by "
            "the sweep; no attempt was spent."
        )
    refused = [e for e in entries if e.get("event") == "dropped-refused"]
    if refused:
        names = sorted({name for e in refused for name in (e.get("paths") or [])})
        print(
            f"\n{len(refused)} proposal(s) dropped for a name you had already "
            "rejected: " + ", ".join(names[:5])
        )
    thin = [e for e in entries if e.get("reason") == "thin session"]
    nothing = sum(1 for e in reviews if "nothing to save" in (e.get("reply") or "").lower())
    print(f"\nNo-op reviews:        {nothing} of {len(reviews)}")
    print(f"Skipped as too thin:  {len(thin)}")
    if reviews and nothing == len(reviews):
        print("  All reviews were no-ops. Check the prompt is reaching the model.")
    unstructured = [e for e in entries if e.get("event") == "reflect-unstructured"]
    if unstructured:
        print(
            f"  {len(unstructured)} run(s) returned no structured output. If that "
            "is all of\n  them, --json-schema is not being honoured and every "
            "review will read\n  as a no-op whether or not it found anything."
        )

    # The question this loop exists to answer. Everything above measures how
    # hard it worked; this measures whether any of it landed.
    usage = read_state().get("usage", {}) or {}
    owned = [path.parent.name for path in guard.writable_skills()]
    if owned:
        used = [name for name in owned if (usage.get(name) or {}).get("count")]
        print(f"\nSkills in the library: {len(owned)}")
        print(f"Ever loaded since:     {len(used)}")
        unused = sorted(set(owned) - set(used))
        if unused:
            print("Never loaded:          " + ", ".join(unused[:10]))
            if len(unused) > 10:
                print(f"                       and {len(unused) - 10} more")
            print(
                "  A skill nobody loads is usually a description problem, not a\n"
                "  content problem. The curator sees these counts too."
            )
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
