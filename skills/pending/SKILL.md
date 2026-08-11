---
name: pending
description: Shows what the patina loop wants to change in the skill library, waiting for approval.
disable-model-invocation: true
allowed-tools: Bash(patina pending list), Bash(patina pending show *)
---

Run `patina pending list`.

If the queue is empty, say so in one line and stop.

Otherwise report what is waiting, grouped so the user can decide where to spend
attention:

- Lead with anything marked `MALFORMED` — those cannot be approved as they
  stand.
- Then entries whose strongest claim is `high` confidence: the loop is most sure
  about these, and they are usually explicit corrections the user made.
- Then the rest, one line each.

Do not run `show` on everything. Offer it: name the two or three worth opening
first and why. If the user asks for one, run `patina pending show <id>` and
summarize the claims and the diff rather than pasting the whole thing.

Never approve or reject anything from this skill, even if the user's phrasing
sounds like assent — that is `/patina:approve` and `/patina:reject`, which exist
separately so the decision is always explicit.
