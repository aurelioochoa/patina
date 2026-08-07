# claude-self-improve

A port of [Hermes Agent](https://github.com/hermes-agent)'s background self-improvement loop
to Claude Code.

Claude Code sessions are amnesiac. A correction given in one session — "stop formatting like
that", "you always miss this step" — is gone by the next. This adds a background loop that
reviews finished sessions and writes what it learned into your skill library and memory.

## Status

**Design approved, not yet implemented.** See
[the spec](docs/superpowers/specs/2026-08-07-claude-self-improvement-loop-design.md).

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
