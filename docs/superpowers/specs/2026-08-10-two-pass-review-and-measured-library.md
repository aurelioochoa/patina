# Two-pass review, a curator that can act, and a measured library

2026-08-10. Amends the [2026-08-07 design](2026-08-07-claude-self-improvement-loop-design.md);
that document still describes the hooks, the quarantine queue, and the guard.

## Why

A comparison against the current field — the closest peer implementation
([UniM0cha/self-improving-skills](https://github.com/UniM0cha/self-improving-skills)), the
research this design implicitly implements ([ACE](https://arxiv.org/abs/2510.04618),
[ReasoningBank](https://arxiv.org/abs/2509.25140), the
[memory-poisoning literature](https://arxiv.org/html/2606.04329v1)), and Anthropic's
[skill-authoring guidance](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
— found one bug that made a documented feature a no-op and five gaps.

## Decisions

### The memory half is gone

`guard.memory_dir()` resolved to `~/.claude/projects/<slug>/memory/`, which is where Claude
Code's own auto-memory now writes, MEMORY.md index included. Two writers on one directory with
no merge protocol is worse than one writer, and the one that ships with the harness is in the
session while it happens rather than reading a digest afterwards. patina is a skill-library
loop now.

Removed with it: `WORK_MEMORY`, `snapshot_dir`/`diff_snapshot` (their only caller was the
memory sync), and the `cwd` parameter threaded through `writable_roots` /
`violates_allowlist` / `verify_writes`, whose sole purpose was widening the allowlist to the
active project's memory directory.

### Review is two forks, not one

**Reflect** — no tools, no `--add-dir`, `--json-schema`. Reads the digest, returns
`{lessons: [{kind, claim, evidence, confidence, suggested_target}], note}`.

**Place** — the previous review prompt minus the digest. Receives only the lessons, has the
scratch tree, writes.

Three consequences:

1. **The pass that can write never reads attacker-controlled text.** Transcripts carry web
   pages, file contents and command output; an approved skill's description enters every
   later session's system prompt. This is the structural version of a defence that fencing
   only asks for. The reflect prompt still carries an untrusted-evidence boundary, because
   laundering an instruction into a *lesson* remains possible.
2. **Zero lessons means no second fork.** The common case gets cheaper, which is what pays
   for running two passes on the sessions that do teach something.
3. **Proposals are reviewable as claims.** `meta.json` carries the lessons that motivated the
   entry; `pending show` prints claim and evidence above the diff.

### The curator's actions actually land

`pending.capture()` walked only the work tree, so a deletion was invisible and an archive move
(`archive/<name>/…`) filed an entry for a skill named `archive` — which on approval replaced
the entire archive directory and left the original skill live. Consolidation and archival,
the curator's two headline actions, could not take effect.

Now: `KIND_ARCHIVE` entries that move the *live* directory on approval and record no approval
verdict; per-file deletions inside a surviving skill captured and shown in the diff; a
whole-skill deletion dropped rather than queued, since approving one is the only action in
this system the queue cannot take back.

### The loop is measured

`digest.skills_loaded` was computed and discarded. It now accumulates into `state["usage"]`
for every session, reviewed or not, keyed against re-sweeps so a transcript cannot count
twice. The curator sees the counts and archives only what is both old and never loaded, with
double the age allowance for anything used three times or more. `--status` reports skills
written against skills ever loaded.

### Sessions below a substance threshold are not reviewed

The review prompt pushes hard to find something worth keeping — the fix for an earlier run of
all-no-op reviews. Aimed at a three-message session, that pressure manufactures a lesson.
Gate: `tool_calls < 8 and user_turns < 5`. Either signal alone earns a review, because a long
tool-free conversation is exactly where preferences live.

### Mechanical checks at capture time

`src/lint.py`, pure functions, run when an entry is queued rather than by the weekly curator.
Blocking: frontmatter that would fail to load (name shape and length, reserved words, missing
or oversized description). Warning: body past 500 lines, first-person descriptions, nested
references, missing table of contents, a description whose trigger terms collide with an
existing skill, a patch that removes more than half a file. Blocking findings refuse approval
unless `--force`.

### Fork invocation is one function

`guard.fork_command()` and `guard.parse_fork_result()`. Adds `--output-format json`,
`--no-session-persistence`, `--max-budget-usd` (default `$0.50`) and an optional
`--fallback-model`. Result parsing degrades to plain text if the envelope ever changes.

Deliberately not used: `--bare` (forces API-key auth, breaking OAuth logins) and
`--setting-sources` (risks the user's provider configuration).

## What was considered and left out

- **Learning from failures** (ReasoningBank's dual-signal memory). The existing "do not
  capture unresolved failures" rule stays; the distinction between "this approach class is
  wrong here" and "this tool is broken" is real but needs its own design.
- **Embedding-based retrieval.** The duplicate-trigger warning is the cheap approximation.

## Expected effect

Fewer sessions reviewed, each reviewed session costing two passes with the early exit
absorbing part of that. The win is proposal quality, a curator whose actions land, and the
first real signal about whether any of it is working.
