# claude-self-improve

A port of [Hermes Agent](https://github.com/hermes-agent)'s background self-improvement loop
to Claude Code.

Claude Code sessions are amnesiac. A correction given in one session — "stop formatting like
that", "you always miss this step" — is gone by the next. This adds a background loop that
reviews finished sessions and writes what it learned into your skill library and memory.

## Status

**Code complete, not yet activated.** 95 tests pass. One live end-to-end run was
attempted and blocked by an account session limit, so the loop has never
actually written a skill. Hooks are deliberately unregistered until it has.

See [the spec](docs/superpowers/specs/2026-08-07-claude-self-improvement-loop-design.md).

## Install

```sh
./install.sh                  # copy scripts to ~/.claude/self-improve, init audit repo
```

This does **not** activate anything. Verify by hand first:

```sh
# 1. Dry run -- builds the prompt, forks nothing
python3 ~/.claude/self-improve/review.py \
    --transcript ~/.claude/projects/<slug>/<session>.jsonl --dry-run

# 2. Rehearsal with real auth but redirected writes, so the live
#    skill library cannot be touched
export CLAUDE_SELF_IMPROVE_SKILLS_DIR=/tmp/rehearsal/skills
export CLAUDE_SELF_IMPROVE_STATE_DIR=/tmp/rehearsal/state
python3 ~/.claude/self-improve/review.py --transcript <transcript>
git -C /tmp/rehearsal/skills log -p

# 3. Only once that looks right
./install.sh --register-hooks
```

`./install.sh --uninstall` removes the scripts and hooks and leaves your skills
and their git history alone.

## Operating it

```sh
review.py  --status          # runs, no-op rate, allowlist violations
curator.py --status          # interval, pending sweep, run count
curator.py --run             # run the curator now, ignoring the interval
curator.py --pause           # stop the loop without uninstalling
git -C ~/.claude/skills log  # everything it has ever written
```

If `--status` shows every review as a no-op, the prompt is not reaching the
model — that is the failure this reporting exists to catch.

## Adopting a skill

The loop only writes to skills carrying the marker. To hand one over:

```yaml
---
name: my-skill
description: ...
metadata:
  autoManaged: true
---
```

Remove the marker to take it back.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_SELF_IMPROVE_MODEL` | `sonnet` | model for both review and curator |
| `CLAUDE_SELF_IMPROVE_TIMEOUT` | `600` | per-review timeout, seconds |
| `CLAUDE_SELF_IMPROVE_CURATOR_TIMEOUT` | `900` | curator timeout, seconds |
| `CLAUDE_SELF_IMPROVE_SKILLS_DIR` | `~/.claude/skills` | redirect writes (rehearsal) |
| `CLAUDE_SELF_IMPROVE_STATE_DIR` | `~/.claude/self-improve` | redirect state |
| `CLAUDE_SELF_IMPROVE_PROJECTS_DIR` | `~/.claude/projects` | redirect transcript discovery |

Curator interval lives in `state.json` as `interval_hours` (default 168).

## How it works

- A `SessionEnd` hook forks a detached, headless `claude -p` that reads a bounded digest of
  the session transcript and decides whether anything is worth keeping.
- A `SessionStart` hook checks an interval and, roughly weekly, runs a curator pass that
  consolidates overlapping skills and archives stale ones. No daemon.
- Writes are confined to skills explicitly marked `metadata.autoManaged: true`, enforced both
  in the prompt and by a post-run check that reverts anything outside the allowlist.
- Every write is a git commit in a local audit repo, plus a line in an append-only log.

## Design notes

Two things carried over from Hermes because they are what make the difference between a
useful skill library and a junk drawer:

**The preference order.** Patch a loaded skill → patch an existing umbrella → add a
`references/` file → only then create a new skill. Without it you get a flat list of
one-session-one-skill entries.

**The "Do NOT capture" list.** Environment-dependent failures, negative claims about tools,
transient errors, and unresolved failures are explicitly excluded. This is what stops
"browser tools do not work" from hardening into a permanent self-inflicted constraint the
agent cites against itself months after the problem was fixed.

## Layout

Source lives here. Runtime state lives in `~/.claude/self-improve/` and is never committed —
`install.sh` puts the scripts in place without bringing the tree with them.

## License

MIT
