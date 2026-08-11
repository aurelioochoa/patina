---
name: reject
description: Discards a queued patina proposal so the loop stops offering it.
disable-model-invocation: true
argument-hint: "[id | --all]"
allowed-tools: Bash(patina pending show *), Bash(patina pending reject *), Bash(patina pending list)
---

Reject the pending entry named in `$ARGUMENTS`.

With no arguments, run `patina pending list` and ask which one.

Then run `patina pending reject <id>`.

Two things worth telling the user once, when it applies:

- Rejecting a **new skill** records a permanent refusal, so the loop will not
  propose that name again. That is the point, but it is not easily undone.
- Rejecting a **patch** discards only this proposal. The same lesson can come
  back from a later session.

If the user is rejecting because the proposal is close but wrong, say so plainly:
the loop cannot read the rejection, so nothing improves from it. Editing the
skill by hand after approving, or adopting the skill and correcting it, teaches
more than a refusal does.
