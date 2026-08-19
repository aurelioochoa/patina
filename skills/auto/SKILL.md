---
name: auto
description: Turns patina's autonomous approval on or off, and reports what a policy would take.
disable-model-invocation: true
argument-hint: "[on | off | status | --dry-run]"
allowed-tools: Bash(patina pending auto), Bash(patina pending auto *), Bash(patina pending list), Bash(patina status)
---

Autonomous mode lets proposals that pass a policy reach the skill library
without you reading them first. Handle `$ARGUMENTS` as follows.

**With no arguments, or `status`** — run `patina pending auto status` and report
it. Say plainly whether it is on, what the thresholds are, and what is currently
on trial. If anything's trial has expired, name it: the curator will archive it
on its next pass.

**`--dry-run`** — run `patina pending auto --dry-run`. Nothing is applied. Report
both halves and do not bury the second one: what the policy would take, and what
it would hold with the reason for each. The reasons are the point. If the run
would take a large number at once, say so with the number rather than
summarising it as "several".

**`on`** — this is the consequential one. Before running it, put the trade in
front of the user in your own words:

- What lands without asking: high-confidence proposals with at least the
  minimum number of lessons behind them, lint-clean, and no name they have
  rejected before. Everything else still queues.
- What they get instead of the queue: a git commit per write in
  `~/.claude/skills` that `git revert` undoes, a trial window after which the
  curator archives anything nothing loaded, and a prompt the first time an
  auto-approved skill actually runs.
- What it costs: skill descriptions enter the system prompt of every later
  session whether or not the skill is invoked.
- **What it covers**: the whole queue, including whatever is already waiting in
  it, applied on every scheduled background pass — not only proposals filed
  from here on. If they have a backlog, say how large it is, because turning
  this on is what decides it.

If they have a queue they have not seen, run `--dry-run` first and report it
before running `on`. Then run `patina pending auto on`.

**`off`** — run `patina pending auto off`. Anything already approved stays where
it is; trials already running still expire. Say both.

**Applying the policy right now** (`patina pending auto`, no flag) — this is
only needed to avoid waiting for the next scheduled pass; autonomous mode does
it on its own. Show the dry run first if the user has not seen one.
