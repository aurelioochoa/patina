---
name: status
description: Reports whether the patina loop is running, what it costs, and whether anything it wrote is ever used.
disable-model-invocation: true
allowed-tools: Bash(patina status)
---

Run `patina status` and interpret it. The numbers are not self-explanatory, and
three of them are the ones that matter:

- **Every review a no-op.** The prompt is not reaching the model. This is the
  failure the reporting exists to catch, not a quiet library.
- **Runs that returned no structured output.** If that count is climbing,
  `--json-schema` is not being honoured and every review will read as "nothing
  to save" whether or not it found something.
- **Skills written but never loaded.** The loop is working and the library is
  not. An unused skill is almost always a description that never matched
  anything, rather than a body that was wrong — the fix is rewriting the
  trigger, not deleting the skill.

Also worth calling out when present: spend that is climbing faster than the
number of reviews, a sweep backlog that is not shrinking, or runs stopped at the
spend ceiling (retrying those cannot help — the ceiling is the problem).

Report what you see in a few lines. If everything is healthy, say that in one
line rather than restating the whole table.
