#!/usr/bin/env bash
# Install patina, the background self-improvement loop, into ~/.claude.
#
# Copies scripts to ~/.claude/patina/ and initialises the local audit
# repo. Does NOT register hooks -- that is a separate, deliberate step, because
# a broken SessionStart hook fires on every session and you would be debugging
# it inside the tool it is breaking. Run --register-hooks only once the manual
# checks below pass.
#
#   ./install.sh                  # install scripts, init audit repo
#   ./install.sh --register-hooks # wire into settings.json (do this last)
#   ./install.sh --uninstall      # remove scripts and hooks, keep skills
#
# If you installed the plugin instead, it carries the same three hooks and
# --register-hooks will refuse rather than register them twice.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
TARGET="$CLAUDE_DIR/patina"
# Pre-rename layout. Carried over rather than abandoned: it holds the review
# watermarks, the pending queue, the approvals and the audit log.
LEGACY_TARGET="$CLAUDE_DIR/self-improve"
LEGACY_WORK="$HOME/.cache/claude-self-improve"
WORK="$HOME/.cache/patina"
SETTINGS="$CLAUDE_DIR/settings.json"
SKILLS="$CLAUDE_DIR/skills"

info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# Move a pre-rename installation into place rather than leaving two half-live
# trees. The state is the valuable part: watermarks, pending queue, approvals,
# audit log. Losing it makes the loop re-review every recent session.
migrate() {
  if [ -d "$LEGACY_TARGET" ] && [ ! -d "$TARGET" ]; then
    mv "$LEGACY_TARGET" "$TARGET"
    ok "moved state from $LEGACY_TARGET"
  elif [ -d "$LEGACY_TARGET" ]; then
    warn "both $LEGACY_TARGET and $TARGET exist — merge them by hand"
  fi
  if [ -d "$LEGACY_WORK" ] && [ ! -d "$WORK" ]; then
    mv "$LEGACY_WORK" "$WORK"
    ok "moved work tree from $LEGACY_WORK"
  fi
}

install_scripts() {
  echo "Installing to $TARGET"
  command -v claude >/dev/null || die "claude not found on PATH"
  command -v git    >/dev/null || die "git not found on PATH"
  command -v python3 >/dev/null || die "python3 not found on PATH"

  migrate

  mkdir -p "$TARGET/prompts"
  cp "$SOURCE_DIR"/src/*.py "$TARGET/"
  cp "$SOURCE_DIR"/src/prompts/*.md "$TARGET/prompts/"
  # The single review prompt became reflect.md + place.md. Copying does not
  # remove it, and a leftover prompt nothing reads is a thing to trip over.
  rm -f "$TARGET/prompts/review.md"
  chmod +x "$TARGET/review.py" "$TARGET/curator.py"
  ok "scripts installed"

  mkdir -p "$SKILLS"
  if [ -d "$SKILLS/.git" ]; then
    ok "audit repo already initialised"
  else
    git -C "$SKILLS" init -q
    git -C "$SKILLS" config user.name  "patina"
    git -C "$SKILLS" config user.email "patina@localhost"
    git -C "$SKILLS" add -A
    git -C "$SKILLS" commit -q --allow-empty -m "Baseline before autonomous writes"
    ok "audit repo initialised at $SKILLS"
  fi

  # Runtime state lives here, deliberately outside the source tree so it can
  # never be committed to the public repo.
  mkdir -p "$TARGET/.locks"
  ok "state directory ready"

  cat <<EOF

Installed, but NOT yet active. Verify by hand before registering hooks:

  1. Dry run against a past session (forks nothing):
       python3 $TARGET/review.py --transcript <a-transcript.jsonl> --dry-run

  2. One real run, then inspect what it did:
       python3 $TARGET/review.py --transcript <a-transcript.jsonl>
       git -C $SKILLS log -p
       python3 $TARGET/review.py --status

  3. Only then:
       ./install.sh --register-hooks

Find transcripts under: $CLAUDE_DIR/projects/<project-slug>/*.jsonl
EOF
}

# The same three hooks ship inside the plugin, so registering them here as well
# fires each one twice per session. The loop survives that -- the review lock
# defers the second, and the watermark makes it a no-op -- but it doubles the
# forks the sweep can start, and a doubled spend ceiling is not a ceiling.
#
# A plugin loaded with --plugin-dir for development leaves no trace on disk, so
# this catches the installed case only. Hence a warning with an override rather
# than a hard refusal.
plugin_is_installed() {
  python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, sys

root = pathlib.Path(sys.argv[1]) / "plugins"
for manifest in root.rglob(".claude-plugin/plugin.json"):
    try:
        if json.loads(manifest.read_text()).get("name") == "patina":
            print(manifest.parent.parent)
            raise SystemExit(0)
    except (OSError, json.JSONDecodeError):
        continue
raise SystemExit(1)
PY
}

register_hooks() {
  [ -f "$SETTINGS" ] || die "no settings.json at $SETTINGS"
  [ -x "$TARGET/review.py" ] || die "run ./install.sh first"

  if found=$(plugin_is_installed); then
    if [ "${FORCE:-0}" != "1" ]; then
      warn "the patina plugin is already installed at:"
      info "  $found"
      info "It registers these same hooks, so doing both fires each twice."
      info "Manage hooks with /plugin instead, or re-run with --force."
      die "refusing to double-register"
    fi
    warn "plugin present but --force given; hooks will fire twice"
  fi

  cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d_%H%M%S)"
  ok "backed up settings.json"

  python3 - "$SETTINGS" "$TARGET" <<'PY'
import json, sys
settings_path, target = sys.argv[1], sys.argv[2]
with open(settings_path) as fh:
    settings = json.load(fh)

hooks = settings.setdefault("hooks", {})

def ensure(event, command, extra=None, matcher=None):
    entry = {"type": "command", "command": command, "timeout": 30}
    if extra:
        entry.update(extra)
    matchers = hooks.setdefault(event, [])
    for existing in matchers:
        for hook in existing.get("hooks", []):
            if any(n in str(hook.get("command", "")) for n in ("patina", "self-improve")):
                hook.clear()
                hook.update(entry)
                return f"updated {event}"
    block = {"hooks": [entry]}
    if matcher:
        block["matcher"] = matcher
    matchers.append(block)
    return f"added {event}"

# async so neither session hook ever holds up the user.
print(ensure("SessionEnd", f"python3 {target}/review.py", {"async": True}))
print(ensure("SessionStart", f"python3 {target}/curator.py --check"))
# Backstop: refuse auto-created skills the author has not approved. Must be
# synchronous -- its verdict is the point.
print(ensure("PreToolUse", f"python3 {target}/skillgate.py", matcher="Skill"))

with open(settings_path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
PY

  python3 -c "import json;json.load(open('$SETTINGS'))" || die "settings.json is now invalid — restore the .bak"
  ok "hooks registered and settings.json still parses"
  info "Start a new session and end it, then: python3 $TARGET/review.py --status"
}

uninstall() {
  if [ -f "$SETTINGS" ]; then
    cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d_%H%M%S)"
    python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as fh:
    settings = json.load(fh)
for event, matchers in list(settings.get("hooks", {}).items()):
    if not isinstance(matchers, list):
        continue
    for matcher in matchers:
        matcher["hooks"] = [
            h for h in matcher.get("hooks", [])
            if not any(n in str(h.get("command", "")) for n in ("patina", "self-improve"))
        ]
    settings["hooks"][event] = [m for m in matchers if m.get("hooks")]
    if not settings["hooks"][event]:
        del settings["hooks"][event]
with open(path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
PY
    ok "hooks removed"
  fi
  rm -rf "$TARGET" "$LEGACY_TARGET"
  ok "scripts removed"
  warn "left alone: $SKILLS (your skills and their git history)"
}

FORCE=0
if [ "${2:-}" = "--force" ]; then
  FORCE=1
fi

case "${1:-}" in
  --register-hooks) register_hooks ;;
  --uninstall)      uninstall ;;
  "")               install_scripts ;;
  *)                die "unknown option: $1" ;;
esac
