---
name: approve
description: Applies a queued patina proposal to the live skill library.
disable-model-invocation: true
argument-hint: "[id | --all | <id> --refine]"
allowed-tools: Bash(patina pending show *), Bash(patina pending approve *), Bash(patina pending refine *), Bash(patina pending list)
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

## `--refine`: work on the draft before it lands

What the loop files is a draft. The pass that wrote it had no user to ask, no
way to test whether the skill actually fires, and a spend ceiling — so the
wording, the trigger phrase, and whether the thing works at all are exactly the
questions it could not answer. This session can.

When the user asks to refine an entry, or when reading one together turns up
something worth fixing rather than rejecting:

1. Run `patina pending refine <id>`. It stages an editable copy and prints the
   path, the claims behind the draft, and whether skill-creator is installed.
2. If it reports a skill-creator path, invoke that skill and work on the staged
   copy with it. The draft is already written, so the useful part of its loop is
   the second half: test prompts, does it trigger, does the output improve,
   iterate on the description. Skip the interview — the claims printed in step 1
   are what the interview would have been asking about.
3. If it reports skill-creator missing, either edit the staged copy directly or
   pass on the install command it printed. Do not install a plugin without
   asking.
4. Finish with `patina pending approve <id> --from <staged path>`. The refined
   copy is re-checked before it is applied, so a refinement that breaks the
   frontmatter is refused like any other malformed skill.

The queue entry stays untouched until that last command, so an abandoned
refinement costs nothing.
