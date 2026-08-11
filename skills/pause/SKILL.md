---
name: pause
description: Stops the patina loop from running, or starts it again, without uninstalling anything.
disable-model-invocation: true
argument-hint: "[resume]"
allowed-tools: Bash(patina curator --pause), Bash(patina curator --resume), Bash(patina status)
---

If `$ARGUMENTS` contains "resume" or "on", run `patina curator --resume`.
Otherwise run `patina curator --pause`.

Pausing stops both intervals: no curation, and no daily sweep. Sessions still
end normally and the `SessionEnd` review still fires — pausing governs the
scheduled work, not the per-session one. Say so, since "pause the loop" usually
means "stop it spending money in the background" and this only stops part of it.

To stop the per-session reviews as well, the hooks have to come out: `/plugin`
to disable the plugin, or `install.sh --uninstall` for a script install.

Confirm the new state afterwards with `patina status` and report the two
interval lines.
