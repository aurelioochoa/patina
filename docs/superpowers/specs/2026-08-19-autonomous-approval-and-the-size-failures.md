# Autonomous approval, and the two failures that had to be fixed first

2026-08-19. Amends the [2026-08-07 design](2026-08-07-claude-self-improvement-loop-design.md)
and its approval-gating addendum, which this document partly reverses. The
[2026-08-10 spec](2026-08-10-two-pass-review-and-measured-library.md) is unchanged.

## Why

Twelve days of running the loop against real sessions, measured from
`~/.claude/patina/audit.jsonl` rather than reasoned about:

| | |
|---|---|
| Reviews | 75 |
| Spend | $35.63 |
| Proposals queued | 46 |
| Proposals approved | **0** |
| Skills in `~/.claude/skills` written by the loop | **0** |

The library contained exactly one skill, `find-docs`, installed months earlier by hand. Every
mechanism in the design worked — the two-pass split, the quarantine, the queue, the lint, the
usage counters — and the loop had still never changed anything, because the last step is a
human reading 46 diffs and no human did.

Two failures were also live, and both silently dropped the largest sessions — the ones most
likely to carry a lesson.

## Part 1 — the size failures

### `OSError(7, 'Argument list too long')`, retried forever

The prompt went to `claude -p` as a single argv element. Linux caps one argv string at
`MAX_ARG_STRLEN` = 131072 bytes. `digest.DEFAULT_MAX_CHARS` was 120,000 *chars* and
`reflect.md` added 5,957 bytes on top. Measured, by rebuilding the prompts:

| session | reflect prompt | over the limit | logged |
|---|---|---|---|
| `7c0d9e8d` | 136,606 B | yes | `error` ×2 |
| `b1949266` | 133,233 B | yes | `error` |
| `5cd46c47` | 127,705 B | no | `budget-exhausted` |

Worse than the crash: the top-level `except` logged `error` and returned 0 without touching
the watermark or the attempt count, so the sweep picked the same transcript up the next day
and every day after. One session raised the identical error nine minutes apart.

**Decided.** The prompt goes on the child's stdin; nothing bounds stdin. `fork_command`'s
`prompt` argument becomes optional and is kept only for tests and for reading a command back.
The catch-all now spends an attempt through the existing `mark_failed`, so an unforeseen
exception terminates after `MAX_ATTEMPTS` instead of never.

The stdin change makes the E2BIG case impossible; the attempt accounting is for the *next*
unforeseen failure, which is the one that matters.

### The spend ceiling made large sessions permanently unreviewable

12 `budget-exhausted`, 9 `gave-up`, **$4.98 burned** (14% of all spend) on reviews that paid
~$0.55 and returned nothing. The handling was already correct — a budget failure is terminal,
because retrying spends the ceiling twice more for the same outcome — but the consequence was
that size alone disqualified a session forever.

Both failures had one root cause: **the digest was sized by a blunt character ceiling that was
simultaneously too close to the argv limit and too expensive for the budget**, and on overflow
it *truncated* — discarding content rather than condensing it.

**Decided.**

1. `DEFAULT_MAX_CHARS` 120,000 → 60,000 (≈15k tokens), sized for the budget rather than for
   the argv limit that no longer applies.
2. Overflow is compacted, not cut. A Haiku pass with no tools and one turn rewrites the
   *older* turns into dense prose; the verbatim tail is never touched, for the same reason
   `render` shrinks the tail last. `digest.py` stays pure — it exposes `summarise_older` and
   accepts an `older_override`, and the forking belongs to the caller.
3. Per-pass ceilings. One number for every fork was too low for reflect over a big session and
   many times too high for a Haiku compaction: `compact 0.25 / reflect 0.50 / place 0.75 /
   curate 0.50`, each falling back to the shared `MAX_USD`.
4. Compaction fails soft. Any failure falls back to the truncating render and logs
   `compact-failed`. A compaction that did not work is a worse review; a compaction that takes
   the review down with it is no review at all.

`max_chars` is resolved at call time rather than bound as a default argument — the same
mistake `guard._git` made with `SKILLS_DIR`.

### Measured after

| session | before | after |
|---|---|---|
| `7c0d9e8d` (6.0 MB) | `OSError(7)`, retried daily | exit 0, 6 lessons, 3 proposals |
| `5cd46c47` (12.1 MB) | `budget-exhausted`, $0.60 for nothing | exit 0, 2 lessons, 2 proposals, $0.40 |

The compaction on `5cd46c47` took 60,030 chars to 9,784 for $0.086, with nothing truncated.
The session is now both cheaper and working.

The first live run also found that the planned $0.10 compaction ceiling was one cent short of
the real cost ($0.108 to compact 123,862 characters). The fail-soft path caught it exactly as
designed; the default is now $0.25.

## Part 2 — autonomous approval

This reverses the primary control established in the 2026-08-07 addendum. That addendum's
reasoning was sound and is not being discarded — it is being answered.

### The queue is not free

The addendum argued that quarantine must be primary because a use-time prompt arrives at the
worst possible moment (consent fatigue) and because a skill's description enters the system
prompt whether or not it is ever invoked. Both are still true. What the addendum did not weigh
is the cost of a queue nobody drains: 46 proposals is not 46 decisions deferred, it is 46
decisions never made, at full price.

### Decided: a policy, not a blanket

`pending.auto_verdict(entry)` returns `None` to approve or the reason it waits. Every check in
it already existed for the human path; nothing here is a new judgement about quality.

| Check | Rule |
|---|---|
| A `never` verdict on the name | Outranks the policy, permanently |
| `blocking_findings()` | Must be empty; `--force` is unreachable from the policy |
| Claims | At least `auto_min_lessons` (2). One lesson is an anecdote |
| Confidence | Strongest claim must be `high` |
| Patch size | Over `auto_max_patch_lines` (120) is a rewrite, and waits |
| Archival | Only when `state["usage"]` shows the skill was never loaded |

Off by default. Anything failing any check stays exactly where it is today.

### Decided: auto-approval records `auto`, never `always`

`approve()` ended with `set_approval(skill, "always")`, which permanently silences
`skillgate`. Under autonomous mode that would retire the queue *and* the use-time gate in one
commit, for a skill no person ever read.

An auto-approval records a third verdict, `auto`, which `skillgate` treats as ask-once-per-
session. This is the addendum's own objection answered rather than ignored: the prompt now
fires only for auto-approved skills something actually tried to invoke — a small fraction of
what the loop writes — and at the moment the skill is about to act.

### Decided: a trial window, because the library must be able to shrink

The addendum's second argument — a description costs context whether or not the skill is
invoked — is what a use-time gate genuinely cannot answer. So auto-approved skills land on
probation, recorded in `state["auto_approved"]`. A skill nothing has loaded within
`auto_trial_days` (14) is archived by the curator on its next pass, without a proposal.
Archived, not deleted: contents and git history intact.

Recorded in state rather than stamped into the skill's frontmatter — the file is what the
model wrote and what the user may edit by hand, and a bookkeeping field the loop rewrites is
one more thing that can go wrong in frontmatter that has to stay loadable.

### Decided, then reversed same-day: the policy runs over the whole queue, unattended

First cut: `auto_approve_queue(only=[...])`, so the review and curate paths approved only what
they had just filed. The reasoning was that draining a standing backlog as a side effect of an
unrelated session is not the same decision as turning autonomous mode on.

That was wrong, and the user said so plainly: *"with autonomous mode on you should decide on
the backlog, that is the point of autonomous."* A queue that still requires a person to type
a command is the failure this whole change exists to remove — it is the old queue with extra
steps, and it would have left yesterday's proposals waiting while today's landed.

So the automatic paths pass no `only=` at all, and `curator.run()` applies the policy on every
scheduled pass — the daily sweep and the weekly curate, both already detached via
`spawn_detached` from the `SessionStart` hook. That matters more than the session-end path: it
means the queue drains in weeks where no session produces a proposal, without anything being
typed. The parameter survives as a primitive and is covered by tests; nothing automatic uses
it.

The pass has to be idempotent, since it now runs unattended forever. It is: a held entry stays
held with the same reason and stays queued, which is asserted directly.

### The promise that replaces the old one

The README promised *"Nothing reaches your skill library unless you say so."* That is now
conditional, and the replacement is four mechanisms rather than four reassurances:

> Nothing reaches your library unless it passes a policy you set. Everything that lands is one
> git commit you can revert. Anything that lands and goes unused is retired automatically. And
> the first time an auto-approved skill actually runs, you are asked.

The undo was already built: `approve()` has always called `guard.ensure_skills_repo()` and
`guard.commit()`. `~/.claude/skills` sat at one commit, `Baseline before autonomous writes`,
for twelve days, waiting for writes that never came.

## What was considered and left out

- **On by default.** Plug-and-play argues for it. No version of this has ever written to a
  real library, so the first one to do so should be switched on by someone who decided to.
- **Lifting `disable-model-invocation`** so a session can trigger its own review. A separate
  decision about who spends money.
- **Auto-merging same-skill proposals.** Already shipped in 3b56493 — `capture()` merges into
  an existing pending entry and records `supersedes`. The three duplicate
  `vendoring-third-party-web-builds` entries predate it.
- **Stamping the trial into frontmatter.** State is sufficient and cannot break a skill file.

## Expected effect

Large sessions become reviewable at all, and cheaper than the ones that used to fail. With
autonomous mode off, nothing else changes. With it on, the dry run over the real 46-entry
backlog takes 31 and holds 16 — the three with a standing `never`, the ones predating the
two-pass split with no claims recorded, the medium-confidence ones, and one whose name fails
lint. Whether 31 at once is the right first step is a judgement the dry run exists to let
someone make before it happens.
