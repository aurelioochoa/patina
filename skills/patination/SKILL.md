---
name: patination
description: Reviews the session you are in right now, mid-flight, and queues whatever skill it earns instead of waiting for the session to end.
disable-model-invocation: true
allowed-tools: Bash(ls *), Bash(patina review --transcript *), Bash(patina pending list), Bash(patina pending show *)
---

The loop normally waits for a session to close. This runs it against the session
in progress — useful when the thing worth keeping happened in the last twenty
minutes and you would rather not trust yourself to remember it at midnight.

Resolve the live transcript first:

```
ls ~/.claude/projects/*/"$CLAUDE_CODE_SESSION_ID".jsonl
```

Searching every project directory rather than deriving the slug from the working
directory covers a session that started somewhere else and moved. If
`$CLAUDE_CODE_SESSION_ID` is empty, fall back to the newest `.jsonl` under the
current project's directory and say which file you picked — a wrong guess here
reviews someone else's session.

Then hand it to the loop:

```
patina review --transcript <path> --session-id "$CLAUDE_CODE_SESSION_ID" --cwd "$PWD"
```

This is a real fork: a minute or two, and a few cents. Say so before starting.

**Running this does not consume the session.** The watermark patina records is
the transcript's modification time, and the transcript keeps growing the moment
this returns. Anything said afterwards pushes the mtime past the watermark, so
the SessionEnd hook still reviews the whole session when it closes. Running this
mid-flight is wasteful to repeat, never destructive.

**The compaction is the review's own.** patina digests the transcript before it
forks — that is the compaction step, and it is built for this shape of input. Do
not run `/compact` first to make the session smaller. Compaction rewrites the
conversation into a summary, and a summary is exactly where the specific
correction goes soft: "the user prefers concise output" is what survives, when
the lesson worth keeping was the sentence they actually typed. Reflection needs
the transcript, not a précis of it.

**Do not write the skill yourself.** The temptation is real here, because unlike
the background loop you were present for the session and already know what it
taught. Resist it. The split is the whole safety story — pass one reads the
transcript with no tools and no writable directory, pass two writes without ever
seeing the transcript. Drafting the skill directly collapses both halves into
you: you read the session and you write the file, with nothing in between. Run
the fork and let the queue do its job.

Report what came back:

- Skipped as a thin session — one line, and stop. Mid-session that usually means
  the interesting part has not happened yet.
- Reviewed but nothing found — say so plainly. Common and unremarkable; half a
  session often has not earned anything.
- Something queued — name each proposal and its strongest claim, then point at
  `/patina:approve`. Read it back to the user, since they were there for the
  session and are the one person who can tell a real lesson from a plausible
  one.

Nothing goes live from this command. Approving is `/patina:approve`, deliberately
a separate decision, even when the user's phrasing sounds like assent.
