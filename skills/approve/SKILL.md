---
name: approve
description: Applies a queued patina proposal to the live skill library.
disable-model-invocation: true
argument-hint: "[id | --all]"
allowed-tools: Bash(patina pending show *), Bash(patina pending approve *), Bash(patina pending list)
---

Approve the pending entry named in `$ARGUMENTS`.

**With no arguments**, do not guess. Run `patina pending list` and ask which one.

**Before approving a single entry**, run `patina pending show <id>` and put the
case in front of the user: what it claims to have learned, the evidence, and any
check warnings. An approved skill's description enters the system prompt of every
later session, so this is the moment where that gets decided.

Then run `patina pending approve <id>`.

**`--all` deserves a pause.** Say how many entries it covers and confirm before
running it. Approving a queue unread is the failure this queue exists to prevent.

If approval is refused because the skill is malformed, report the blocking
findings and stop. Do not reach for `--force` on your own — the user has to ask
for it, and the honest options are usually to reject the entry and let the loop
propose something better, or to fix the file by hand after approving with
`--force`.
