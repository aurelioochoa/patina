# Claude Code Self-Improvement Loop — Design

**Date:** 2026-08-07
**Status:** Approved, pending implementation plan

## Problem

Claude Code sessions are amnesiac. A correction given in one session — "stop formatting like
that", "you always miss this step" — is gone by the next. The knowledge exists only in
transcripts nobody reads.

Hermes Agent solves this with a background self-improvement loop. This document specifies a
port of that loop to Claude Code.

### What Hermes actually does

The capability is harness plumbing, not model capability. Three parts:

- `agent/background_review.py` — after each turn, forks the agent in a daemon thread with a
  tool whitelist limited to memory and `skill_manage`, replays the conversation, and runs a
  review prompt that asks "should any skill or memory be saved?". The fork inherits the
  parent's runtime, so it hits the same warm prompt cache. Main conversation untouched.
- `agent/curator.py` — inactivity-triggered maintenance, ~weekly. Archives stale skills,
  consolidates overlaps, auto-transitions lifecycle states. Never deletes.
- `agent/learning_graph.py` — visualization over the result.

Two Hermes properties are load-bearing and must survive the port:

1. **The preference order.** Patch a loaded skill → patch an existing umbrella → add a
   `references/` file → only then create a new skill. Without it the library degenerates into
   a flat list of one-session-one-skill entries.
2. **The "Do NOT capture" list.** Environment-dependent failures, negative claims about
   tools, transient errors, one-off task narratives, and unresolved failures are explicitly
   excluded. This is the part that is easy to omit and expensive to omit — it is what stops
   "browser tools do not work" from hardening into a permanent self-inflicted constraint the
   agent cites against itself months after the problem was fixed.

### What does not port

Hermes replays the full conversation into the fork because the fork shares the parent's
prompt cache, making replay nearly free. Claude Code cannot do this: a headless `claude -p`
is a separate process with a cold cache. Transcripts on this machine range 0.1–4.7 MB of
JSONL. Full replay is therefore off the table and a digest step is mandatory, not an
optimization.

## Constraints

- No daemon. Nothing to install, keep alive, or notice has died.
- The user's foreground session must never be blocked or disrupted by the loop.
- The loop must not be able to corrupt skills it did not author.
- Every autonomous write must be visible and revertible after the fact.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Trigger cadence | `SessionEnd` + idle sweep | Per-turn `Stop` means a cold-start fork per turn; Hermes only affords that via warm cache. `SessionEnd` alone misses hard kills, so a sweep backstops it. |
| Review model | Sonnet | The review is a bounded judgment task. Haiku's judgment is too weak for "durable lesson or one-off?", and a library filled with noise is worse than no library. Opus is unjustifiable for an unwatched background task. |
| Write scope | `autoManaged` frontmatter marker | Structural, not conventional. No marker, no write. Plugin skills live outside the globbed tree entirely. |
| Curator trigger | `SessionStart` interval check | Ports Hermes' no-daemon design. Runs only when Claude Code is used, which is when it matters. |
| Audit | Git repo + JSONL log | `git diff`/`git revert` for free; the log additionally records no-op runs, which git cannot. |
| Packaging | Plain stdlib scripts + hooks | Every component hand-runnable. Critical for something that otherwise only runs invisibly. |
| Repo layout | Source in `~/Repos/claude-self-improve`, installed to `~/.claude/self-improve/` | Runtime state is structurally outside the working tree and cannot be accidentally committed. |

## Architecture

```
SessionStart ──> curator.py --check        (fast, synchronous, ~50ms)
                     │
                     ├─ interval elapsed? ──no──> exit 0, session proceeds
                     └─ yes ──> fork detached ──> curator run + idle sweep
                                                       │
SessionEnd ────> review.py  (async hook, detached)     │
                     │                                  │
                     ├─ digest.py: transcript.jsonl ──> bounded digest
                     ├─ guard.py: build allow/deny sets
                     └─ claude -p <review prompt + digest>
                            --model sonnet
                            --settings '{"hooks":{"disableAllHooks":true}}'
                            --strict-mcp-config
                            --allowedTools Read Write Edit Glob Grep
                            --disallowedTools Bash WebFetch WebSearch Task
                            --max-turns 30
                                   │
                                   └─ writes ──> ~/.claude/skills/<name>/SKILL.md
                                              └> ~/.claude/projects/<slug>/memory/
                                                       │
                                              git commit + audit.jsonl append
```

### Two git repositories, deliberately separate

This is easy to conflate and important to keep straight:

- **`~/Repos/claude-self-improve`** — the source repo. Scripts, prompts, tests, this spec.
  Pushed to GitHub (private). Contains no runtime state and no conversation-derived content.
- **`~/.claude/skills/`** — a *separate* git repository, local only, never pushed. It exists
  purely as the audit and undo mechanism for autonomous writes. `git init` on it is part of
  `install.sh`. Every autonomous write is one commit tagged with session id and reason.

`verify_writes` and archival operate on the second repo. Runtime state (`state.json`,
`audit.jsonl`) lives in `~/.claude/self-improve/`, inside neither repo's working tree.

### Why a detached process, not a subagent

A subagent shares the session's permission and hook context. A detached `claude -p` gets its
own, which makes `disableAllHooks` a real process boundary rather than a convention. It also
means the review survives the user closing the terminal — necessary, since it fires at
session end.

### Verified mechanics

These were confirmed against Claude Code 2.1.224 before the design was settled:

- Hook config supports `"async": true` for detached, non-blocking execution.
- `SessionEnd` and `SessionStart` both deliver `transcript_path`, `session_id`, and `cwd` on
  stdin as JSON.
- `claude -p ... --settings '{"hooks":{"disableAllHooks":true}}' --strict-mcp-config` runs
  clean and exits 0. This is the recursion guard, and it works.
- Transcript JSONL record types present in practice: `user` (`message.content` is a string),
  `assistant` (`message.content` is a block list), `attachment`, plus metadata rows
  (`last-prompt`, `mode`, `permission-mode`, `bridge-session`, `ai-title`,
  `file-history-snapshot`) which carry no conversational content.
- Skills resolve from `~/.claude/skills/<name>/SKILL.md`. Plugin skills resolve from
  `~/.claude/plugins/` and are therefore outside the write surface by construction.
- Memory is per-project at `~/.claude/projects/<cwd-slug>/memory/`, indexed by `MEMORY.md`.
  Directories are created on demand per working directory.

## Components

### `guard.py` — safety kernel

Imported by every other component.

- `writable_skills()` — globs `~/.claude/skills/*/SKILL.md`, parses frontmatter, returns only
  entries with `metadata.autoManaged: true`. The glob is deliberately single-level: personal
  skills in Claude Code are flat (`~/.claude/skills/find-docs/SKILL.md`), unlike Hermes'
  `category/name/` nesting. Nested paths are not writable and not created.
- `verify_writes(before_sha)` — diffs the working tree against the pre-run commit. Any path
  outside the writable set, or outside the active project's memory directory, is reverted via
  `git checkout --` and recorded as a violation.
- `child_env()` — sets `CLAUDE_SELF_IMPROVE_CHILD=1`. Both hook entry points exit immediately
  if it is already set.
- `lock(path)` — `flock`-based context manager, non-blocking with short retry.

The allowlist is enforced twice: injected into the prompt as an explicit list, and verified
after the fork returns. Prompt-level rules are ignored under pressure; the post-hoc check is
not. **The post-hoc check is the actual security boundary. The prompt injection is a
courtesy that reduces wasted work.**

### `digest.py` — transcript bounding

Pure function, `transcript_path → str`, no side effects.

Port of Hermes `_digest_history`: last 24 messages verbatim, older turns collapsed to one
line each (`USER: <300 chars>`, `ASSISTANT[tools: Read, Edit]`, `ASSISTANT: <200 chars>`).

- Records with `isSidechain: true` are dropped — subagent chatter is not the user's lesson.
- Unparseable lines are skipped with a counter, never fatal.
- Hard ceiling on total digest size. On overflow the verbatim tail shrinks before the
  summary does, because recent turns carry the corrections.

Also extracts a header the review depends on: `cwd`, `gitBranch`, session duration, and which
skills were loaded during the session. The preference order needs the last of these — a skill
that was in play is the right one to extend.

### `review.py` — SessionEnd entry point

Reads hook JSON from stdin. Checks the child sentinel and exits if set. Digests the
transcript, builds the prompt from `prompts/review.md` plus the writable allowlist, forks
`claude -p`, verifies writes, commits, appends to `audit.jsonl`.

Flags: `--dry-run` (print the prompt, fork nothing), `--transcript PATH` (run against any
past session), `--status` (report last run, runs in last 30 days, skills touched).

### `curator.py` — SessionStart entry point

Runs on Sonnet, same as the review — one model for the whole loop, no second cost surface to
reason about. Interval defaults to **7 days**, matching Hermes' `DEFAULT_INTERVAL_HOURS`, and
is configurable in `state.json`.

One interval check gating two jobs:

1. **Sweep** — finds transcripts modified since their recorded watermark and runs `review.py`
   on each. This is the hard-kill backstop that makes `SessionEnd` sufficient.
2. **Curate** — maintenance over the whole `autoManaged` library: consolidate overlaps,
   archive stale skills, never delete. Archival is a git commit moving the skill under
   `archive/`, so `git revert` restores it.

State in `~/.claude/self-improve/state.json`: `last_curator_run`, `paused`,
`watermarks{session_id: mtime}`.

### `prompts/review.md`

Hermes' `_COMBINED_REVIEW_PROMPT`, adapted for Claude Code paths, the `autoManaged` ownership
rule, and the user's CLAUDE.md conventions. The preference order and the "Do NOT capture"
list carry over in full. Stored as plain text so it can be edited without touching code.

## Failure modes

The real risk is not crashing. It is failing silently — a loop that has quietly stopped
learning is indistinguishable from one that had nothing to learn.

| Failure | Handling |
|---|---|
| Recursive fork | `disableAllHooks` plus `CLAUDE_SELF_IMPROVE_CHILD` sentinel. Both checked; either alone suffices. |
| Fork writes outside allowlist | `verify_writes` reverts and logs a violation. |
| Fork hangs | `timeout` wrapper on the subprocess. Killed runs are logged and the watermark is not advanced, so the sweep retries. |
| Transcript malformed | `digest.py` skips unparseable lines and logs a count. |
| Two sessions end simultaneously | Per-target `flock`. Loser defers to the sweep. |
| `claude` missing or unauthenticated | Non-zero exit logged; the hook still exits 0 so the user's session is never disrupted. |
| Loop silently stops firing | `audit.jsonl` records every run including no-ops. `--status` surfaces it. |

## Testing

- `digest.py` and `guard.py` are pure enough for unit tests against fixture transcripts,
  including one built from a real 4.7 MB session — the size ceiling worth proving against.
- Integration test with a stub `claude` on `PATH` that writes a known out-of-allowlist file,
  asserting `verify_writes` reverts it.
- End-to-end: `review.py --transcript <real past session> --dry-run`, then one live run
  against a finished session.

## Rollout order

Hooks are registered in `settings.json` **last**, after the scripts are proven by hand. A
broken `SessionStart` hook fires on every session, and it would have to be debugged inside
the tool it is breaking.

1. `guard.py`, `digest.py` + unit tests
2. `review.py` with `--dry-run` against real past transcripts
3. One live `review.py` run, manually inspected
4. `curator.py`, sweep verified against stale transcripts
5. `install.sh` — copy or symlink into `~/.claude/self-improve/`
6. Register hooks in `~/.claude/settings.json`

## Implementation notes (added during build)

Three things the design did not anticipate, discovered while building:

- **Redirectable write targets are necessary, not a convenience.**
  `CLAUDE_SELF_IMPROVE_SKILLS_DIR` / `_STATE_DIR` / `_PROJECTS_DIR` exist because
  sandboxing a rehearsal via `CLAUDE_CONFIG_DIR` alone breaks authentication --
  credentials live in the real config directory, so the fork fails with
  "Not logged in". Redirecting only the write targets lets a rehearsal use real
  auth while being unable to touch the live library.

- **`git status` must be run with `-uall`.** Without it git collapses an
  untracked directory to `sneaky/`, the per-file allowlist check never sees the
  `SKILL.md` inside, and a brand-new unmarked skill slips through.

- **Slash-command plumbing has to be stripped from the digest.** Transcripts
  contain `<local-command-caveat>` blocks whose text instructs the reader to
  ignore the surrounding message. Feeding that to a review fork is asking for
  confusion. `digest.clean` removes them and compacts `<command-name>` to
  `[ran /foo]`.

## Accepted risks

This loop writes to the skill library autonomously, and Sonnet's judgment about what is worth
keeping will not always match the user's. The `autoManaged` marker, git history, and the "Do
NOT capture" list make that recoverable rather than corrosive, but the first month should be
expected to require pruning, and the first several generated skills should be expected to be
mediocre.

Note also that the maintenance half of the Hermes system has never executed on this machine —
`.curator_state` reports `run_count: 0`. What is being ported is a design, not a
battle-tested deployment.

## Out of scope

- A `/learn` skill for on-demand mid-session review. Deferred until the automatic path is
  proven; likely worth adding after.
- Porting the Hermes skill library itself (95+ skills, same `SKILL.md` format). Separate
  effort.
- Any equivalent of `learning_graph.py` visualization.

---

# Addendum: approval gating (2026-08-07)

Added after the loop was working, in response to: "for every skill it creates,
needs permission from the author whether it should or should not use it."

## Why quarantine, not only a use-time prompt

The literal request was a per-use permission prompt. That was implemented, but
as the *backstop*, not the primary control, for two reasons.

**Consent fatigue.** A prompt arrives while the user is mid-task, showing only a
skill name and one-line description. That is the worst moment to make a quality
judgement, and the predictable outcome is "never ask again" clicked to get on
with the work.

**Invocation is not the only way a skill acts.** Every skill's name and
description is injected into the system prompt every session whether or not it
is ever invoked. A bad skill in the library costs context and biases behaviour
without the `Skill` tool firing once. Gating the tool cannot fix that; keeping
the skill out of the library can.

So: quarantine is primary, the gate is the backstop for paths quarantine misses.

## Quarantine

`pending.prepare_work_tree()` copies the library to `guard.WORK_DIR` before each
fork. The fork is given `--add-dir` for the copy and never told where the real
library is, so confinement is a property of what it can *reach* rather than a
check run afterwards. This is strictly stronger than the original
revert-after-the-fact design, which is retained as a cheap assertion.

`pending.capture()` then diffs the copy against the live tree:

- New skill → queued as `new`.
- Change to an `autoManaged` skill → queued as `patch`, with a unified diff.
- Change to a skill without the marker → **dropped, not queued.** Offering to
  approve an edit to a hand-written skill invites exactly the mistake the marker
  exists to prevent. Logged as `dropped-protected-edit`.

Patches are queued because patching is the loop's most common action; a patch
that skipped review could smuggle bad content into an already-trusted skill.

The curator goes through the same queue. A consolidation that merges two skills
is the most consequential action in the system and gets no exemption.

## The four states

`skillgate.py`, a `PreToolUse` hook matching `Skill`. Verified against Claude
Code 2.1.224 — the hook fires for the `Skill` tool, `tool_input.skill` carries
the name, `session_id` is present, and `deny` genuinely blocks:

    {"tool_name": "Skill",
     "tool_input": {"skill": "find-docs", "args": "React useEffect"},
     "session_id": "6564a3bc-..."}

`always` / `never` live in `approvals.json`; session-scoped verdicts are keyed on
`session_id`. Unmarked and plugin skills are never gated. Any internal error
allows rather than denies — a bug in the gate must not lock the user out of
their own skills.

## Marker enforcement

`normalize_new_skills()` forces `autoManaged: true` onto any skill the loop
created without it. Without this, an unmarked creation would, once approved,
look hand-written forever and become permanently unpatchable by the loop. It
also rewrites `createdFrom` to the real session id, since the model reliably
invents a plausible label instead.

Patches are exempt from both rewrites: a patch must not have its history
rewritten.
