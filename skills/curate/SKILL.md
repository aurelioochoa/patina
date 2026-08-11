---
name: curate
description: Runs the patina curator now to consolidate overlapping skills and retire stale ones.
disable-model-invocation: true
allowed-tools: Bash(patina curator --curate-only), Bash(patina pending list)
---

Run `patina curator --curate-only`, then `patina pending list`.

`--curate-only` rather than `--run` on purpose: `--run` sweeps first, reviewing
every session whose SessionEnd hook never fired, up to the sweep limit. That is
correct on a schedule and surprising when a person asked for curation — it forks
a batch of real reviews before it reaches the curator.

This is a real model call and takes a minute or two. Say so before starting.

Nothing it decides is applied. Consolidation and archival are queued like
everything else, so report what landed in the queue and leave the decision with
the user. An archive entry is worth naming explicitly: approving one moves a
skill out of the library, which is recoverable but not silent.
