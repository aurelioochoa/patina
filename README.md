# patina

A port of [Hermes Agent](https://github.com/hermes-agent)'s background self-improvement loop
to Claude Code.

Claude Code sessions are amnesiac. A correction given in one session — "stop formatting like
that", "you always miss this step" — is gone by the next. This adds a background loop that
reviews finished sessions and writes what it learned into your skill library.

It does not touch memory. Claude Code's own auto-memory writes to
`~/.claude/projects/<slug>/memory/`, which is where this loop used to write too — two writers
on one directory with no merge protocol between them. That ground is better held by the
feature that ships with the harness.

Named for what builds on a surface through use, and is worth keeping rather than cleaning
off.

See [the original design](docs/superpowers/specs/2026-08-07-claude-self-improvement-loop-design.md)
and [the 2026-08-10 amendment](docs/superpowers/specs/2026-08-10-two-pass-review-and-measured-library.md),
which added the two-pass review and the usage signal.

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
export PATINA_PROJECTS_DIR=/tmp/rehearsal/projects   # see below
python3 ~/.claude/patina/review.py --transcript <transcript>
python3 ~/.claude/patina/pending.py list
git -C /tmp/rehearsal/skills log -p

# 3. Only once that looks right
./install.sh --register-hooks
```

Redirect `PATINA_PROJECTS_DIR` too, or leave it pointed somewhere empty. It is
where the sweep looks for unreviewed sessions, and it defaults to the real
`~/.claude/projects` even when everything else is redirected — so
`curator.py --run` during a rehearsal will find your genuine backlog and fork a
batch of real reviews before it gets to the curator. Use `curator.py --curate-only`
when the curator is what you meant to exercise.

`./install.sh --uninstall` removes the scripts and hooks and leaves your skills
and their git history alone.

## Two passes

A review is two forks, and the split is the point.

**Reflect** reads the session and returns a structured list of lessons — a
claim, the evidence for it, and a confidence. It has no tools, no directory, and
no way to write anything.

**Place** takes that list and decides where it belongs in the library. It never
sees the transcript.

Two things fall out of this. The cheap one: most sessions teach nothing, and
finding that out now costs one tool-less pass instead of a full write-capable
fork. The important one: **the pass that can write files never reads
attacker-controlled text.** A transcript contains web pages, file contents and
command output, and an approved skill's description enters the system prompt of
every session afterwards. Fencing the prompt asks a model not to be fooled;
this arranges for it never to be asked.

## Nothing goes live unreviewed

Neither fork can reach the real library. Both write into a **scratch copy** and
are not told where the real one is. Afterwards, every difference is filed as a
pending entry for you to approve or reject.

```sh
pending.py list                 # what the loop wants to change
pending.py show <id>            # the claims, the checks, then the diff
pending.py approve <id>         # apply it and trust the skill from now on
pending.py reject <id>          # discard it; the loop won't re-propose it
pending.py approve --all
```

`show` leads with what the change claims to have learned and the evidence for
it, because that is the question a diff cannot answer. Then the mechanical
checks — name and description validity, size, first-person descriptions,
triggers that collide with a skill you already have. Findings that mean the
skill would not load at all block approval; `--force` overrides.

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
review.py  --status          # runs, spend, no-op rate, how much is ever used
curator.py --status          # intervals, pending sweep, run count
curator.py --run             # run the curator now, ignoring the interval
curator.py --curate-only     # curate without sweeping first
curator.py --sweep-only      # catch up on missed sessions, skip consolidation
curator.py --pause           # stop the loop without uninstalling
git -C ~/.claude/skills log  # everything it has ever written
```

Two numbers matter. If every review is a no-op, the prompt is not reaching the
model. If skills are being written but never loaded, the loop is working and the
library still is not: an unused skill is almost always a description that never
matched anything, not a body that was wrong.

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
| `PATINA_MAX_USD` | `0.50` | hard spend ceiling per fork |
| `PATINA_FALLBACK_MODEL` | unset | model to fall back to when the primary is unavailable |

The pre-rename `CLAUDE_SELF_IMPROVE_*` spellings are still read as a fallback, so an old
export in a shell profile keeps working.

Intervals live in `state.json`:

| Key | Default | Purpose |
|---|---|---|
| `interval_hours` | `168` | how often the curator consolidates the library |
| `sweep_interval_hours` | `24` | how often the sweep picks up missed sessions |
| `sweep_limit` | `10` | forks per sweep — a spend ceiling as much as a batch size |
| `min_tool_calls` | `8` | below this *and* `min_user_turns`, a session is not reviewed |
| `min_user_turns` | `5` | either signal alone is enough to earn a review |

## How it works

- A `SessionEnd` hook forks a detached, headless `claude -p` that reads a bounded digest of
  the session transcript and decides whether anything is worth keeping. Sessions too small to
  have taught anything are not forked at all — the prompt pushes hard to find a lesson, and
  aimed at a three-message session that pressure manufactures one.
- Every session, reviewed or not, records which skills it loaded. That count is the only
  feedback the loop has: it decides what the curator treats as stale, and it is what
  `--status` reports as the difference between skills written and skills used.
- A `SessionStart` hook checks two intervals and forks whatever is due. No daemon. Daily, a
  sweep reviews sessions whose `SessionEnd` hook never fired — a hard kill, a closed
  terminal, a crash. Weekly, a curator pass consolidates overlapping skills and archives
  stale ones.
- A review that fails is left unwatermarked so the sweep retries it, up to three attempts.
  The first failure in the wild was an account limit, which is transient by definition;
  treating that as reviewed loses the session permanently.
- Writes are confined to skills explicitly marked `metadata.autoManaged: true`, enforced both
  in the prompt and by a post-run check that reverts anything outside the allowlist.
- Every fork carries a hard dollar ceiling, and a run that hits it is logged as that rather
  than as a crash — retrying cannot fix a limit that is simply too low.
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

And three added since, each answering a failure the original design left open:

**Reflect and place are separate forks.** Splitting evaluation from curation is
[ACE](https://arxiv.org/abs/2510.04618)'s measured result; here it also buys the injection
boundary described above, and an early exit on the common case.

**The curator can act.** Archiving and consolidation both need to move or remove files, which
a capture pass that only walked the proposed tree could not see. Both of the curator's
headline actions used to be silent no-ops.

**Usage is measured.** Age alone cannot distinguish a skill nobody needs from one that
quietly does its job every month. The curator now sees load counts, and a skill that has been
used recently is never stale.

## Layout

Source lives here. Runtime state lives in `~/.claude/patina/` and is never committed —
`install.sh` puts the scripts in place without bringing the tree with them.

## License

MIT
