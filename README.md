# patina

A port of [Hermes Agent](https://github.com/hermes-agent)'s background self-improvement loop
to Claude Code.

Claude Code sessions are amnesiac. A correction given in one session — "stop formatting like
that", "you always miss this step" — is gone by the next. This adds a background loop that
reviews finished sessions and writes what it learned into your skill library and memory.

Named for what builds on a surface through use, and is worth keeping rather than cleaning
off.

See [the spec](docs/superpowers/specs/2026-08-07-claude-self-improvement-loop-design.md).

## Install

```sh
./install.sh                  # copy scripts to ~/.claude/patina, init audit repo
```

This does **not** activate anything. Verify by hand first:

```sh
# 1. Dry run -- builds the prompt, forks nothing
python3 ~/.claude/patina/review.py \
    --transcript ~/.claude/projects/<slug>/<session>.jsonl --dry-run

# 2. Rehearsal with real auth but redirected writes, so the live
#    skill library cannot be touched
export PATINA_SKILLS_DIR=/tmp/rehearsal/skills
export PATINA_STATE_DIR=/tmp/rehearsal/state
python3 ~/.claude/patina/review.py --transcript <transcript>
git -C /tmp/rehearsal/skills log -p

# 3. Only once that looks right
./install.sh --register-hooks
```

`./install.sh --uninstall` removes the scripts and hooks and leaves your skills
and their git history alone.

## Nothing goes live unreviewed

The review fork writes into a **scratch copy** of your skill library, never the
real one. It is not told where the real library is. Afterwards, every difference
is filed as a pending entry for you to approve or reject.

```sh
pending.py list                 # what the loop wants to change
pending.py show <id>            # full text for new skills, a diff for patches
pending.py approve <id>         # apply it and trust the skill from now on
pending.py reject <id>          # discard it; the loop won't re-propose it
pending.py approve --all
```

This covers patches too, not just new skills — patching is the loop's most
common action, and a patch that skipped review could smuggle bad content into a
skill you already trust.

Why quarantine rather than only prompting at use time: **a skill's name and
description are injected into the system prompt every session, whether or not it
is ever invoked.** A bad skill sitting in the library costs context and biases
behaviour without the `Skill` tool ever firing. Gating invocation cannot fix
that; keeping it out of the library can.

### The use-time backstop

`skillgate.py` runs as a `PreToolUse` hook on the `Skill` tool and refuses
auto-created skills you have not blessed. Quarantine means it normally has
nothing to do; it exists for the paths quarantine misses — a skill approved once
and later edited by hand, an entry restored from git.

| State | Behaviour |
|---|---|
| `always` | allow, never ask again |
| `never` | deny, never ask again |
| session | allow for this session only |
| unset | ask |

Hand-written skills (no `autoManaged` marker) and plugin skills are never gated.
If the gate itself errors, it allows — a bug in it must not lock you out of your
own skills.

## Operating it

```sh
review.py  --status          # runs, no-op rate, allowlist violations
curator.py --status          # intervals, pending sweep, run count
curator.py --run             # run the curator now, ignoring the interval
curator.py --sweep-only      # catch up on missed sessions, skip consolidation
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
| `PATINA_MODEL` | `sonnet` | model for both review and curator |
| `PATINA_TIMEOUT` | `600` | per-review timeout, seconds |
| `PATINA_CURATOR_TIMEOUT` | `900` | curator timeout, seconds |
| `PATINA_SKILLS_DIR` | `~/.claude/skills` | redirect writes (rehearsal) |
| `PATINA_STATE_DIR` | `~/.claude/patina` | redirect state |
| `PATINA_PROJECTS_DIR` | `~/.claude/projects` | redirect transcript discovery |
| `PATINA_WORK_DIR` | `~/.cache/patina` | scratch tree handed to the fork |

The pre-rename `CLAUDE_SELF_IMPROVE_*` spellings are still read as a fallback, so an old
export in a shell profile keeps working.

Intervals live in `state.json`:

| Key | Default | Purpose |
|---|---|---|
| `interval_hours` | `168` | how often the curator consolidates the library |
| `sweep_interval_hours` | `24` | how often the sweep picks up missed sessions |
| `sweep_limit` | `10` | forks per sweep — a spend ceiling as much as a batch size |

## How it works

- A `SessionEnd` hook forks a detached, headless `claude -p` that reads a bounded digest of
  the session transcript and decides whether anything is worth keeping.
- A `SessionStart` hook checks two intervals and forks whatever is due. No daemon. Daily, a
  sweep reviews sessions whose `SessionEnd` hook never fired — a hard kill, a closed
  terminal, a crash. Weekly, a curator pass consolidates overlapping skills and archives
  stale ones.
- A review that fails is left unwatermarked so the sweep retries it, up to three attempts.
  The first failure in the wild was an account limit, which is transient by definition;
  treating that as reviewed loses the session permanently.
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

Source lives here. Runtime state lives in `~/.claude/patina/` and is never committed —
`install.sh` puts the scripts in place without bringing the tree with them.

## License

MIT
