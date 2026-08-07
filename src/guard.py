"""Safety kernel for the self-improvement loop.

Every other component imports this. Three jobs:

1. **Recursion prevention.** A headless ``claude -p`` spawned from a hook would
   itself fire that hook. Two independent guards: an env sentinel checked by
   both entry points, and ``--settings '{"hooks":{"disableAllHooks":true}}'`` on
   the child.

2. **Write confinement.** The fork may only touch skills carrying
   ``metadata.autoManaged: true`` and the active project's memory directory.
   Enforced at two layers -- ``--add-dir`` on the child (a harness boundary) and
   :func:`verify_writes` afterwards (the actual security boundary). The prompt
   also lists the allowlist, but that is a courtesy to reduce wasted work, not a
   control. Prompt-level rules get ignored under pressure; the post-hoc check
   does not.

3. **Mutual exclusion.** Two sessions ending at once must not both rewrite the
   same SKILL.md.

Stdlib only. PyYAML is used when importable and a minimal parser covers the
frontmatter shape otherwise -- a hook that dies on import is worse than a hook
that parses slightly less YAML.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

HOME = Path.home()
# install.sh honours the same variable; the two must agree or the installer
# would wire hooks to a tree the scripts never look at.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
# Write targets are independently overridable. This is what makes a real
# end-to-end rehearsal possible: point these at a scratch tree while
# CLAUDE_CONFIG_DIR stays on the real config, so the fork authenticates
# normally but cannot touch the live skill library.
SKILLS_DIR = Path(os.environ.get("CLAUDE_SELF_IMPROVE_SKILLS_DIR") or (CLAUDE_DIR / "skills"))
PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_SELF_IMPROVE_PROJECTS_DIR") or (CLAUDE_DIR / "projects")
)
STATE_DIR = Path(
    os.environ.get("CLAUDE_SELF_IMPROVE_STATE_DIR") or (CLAUDE_DIR / "self-improve")
)
LOCK_DIR = STATE_DIR / ".locks"

#: Staging tree. New skills and proposed patches land here for review; nothing
#: reaches SKILLS_DIR until the user approves it.
PENDING_DIR = STATE_DIR / "pending"

#: Scratch copy of the skill library handed to the fork. The fork is never told
#: where the real library is, so quarantine is a property of what it can reach
#: rather than of a check we run afterwards.
WORK_DIR = STATE_DIR / "work" / "skills"

#: Records which auto-created skills the user has blessed, and which they have
#: refused. See skillgate.py.
APPROVALS_FILE = STATE_DIR / "approvals.json"

#: Env sentinel. Set on every child; both hook entry points exit if they see it.
SENTINEL = "CLAUDE_SELF_IMPROVE_CHILD"

#: Frontmatter key that marks a skill as ours to write. No marker, no write.
AUTO_MANAGED_KEY = "autoManaged"


# ---------------------------------------------------------------------------
# Recursion guards
# ---------------------------------------------------------------------------


def is_child() -> bool:
    """True when running inside a fork we spawned. Entry points must exit."""
    return os.environ.get(SENTINEL) == "1"


def child_env() -> Dict[str, str]:
    """Environment for the forked ``claude -p``."""
    env = dict(os.environ)
    env[SENTINEL] = "1"
    return env


#: Passed to every child. The second half of the recursion guard, and the
#: reason a detached process beats a subagent: this is a real process boundary.
CHILD_SETTINGS = json.dumps({"hooks": {"disableAllHooks": True}})


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def _parse_yaml_minimal(text: str) -> Dict[str, Any]:
    """Fallback parser: top-level ``key: value`` plus one nesting level.

    Covers the frontmatter shape skills actually use. Anything more exotic
    parses as a string, which is fine -- the only field we make decisions on is
    ``metadata.autoManaged``.
    """
    root: Dict[str, Any] = {}
    # (indent_of_key_that_opened_this_mapping, mapping)
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("- ") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value:
            parent[key] = _coerce(value)
        else:
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    return value.strip("\"'")


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Extract a SKILL.md's frontmatter as a dict. ``{}`` when absent."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(block)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return _parse_yaml_minimal(block)


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in ("true", "yes"))


def is_auto_managed(frontmatter: Dict[str, Any]) -> bool:
    """Does this skill carry the ownership marker?

    Accepts ``autoManaged`` at the top level or nested one level under any
    mapping (``metadata.autoManaged``, ``metadata.selfImprove.autoManaged``),
    so adopting a skill by hand is forgiving about placement.
    """
    if _truthy(frontmatter.get(AUTO_MANAGED_KEY)):
        return True
    for value in frontmatter.values():
        if isinstance(value, dict):
            if _truthy(value.get(AUTO_MANAGED_KEY)):
                return True
            for nested in value.values():
                if isinstance(nested, dict) and _truthy(nested.get(AUTO_MANAGED_KEY)):
                    return True
    return False


# ---------------------------------------------------------------------------
# Write surface
# ---------------------------------------------------------------------------


def project_slug(cwd: str | Path) -> str:
    """Claude Code's project directory slug for a working directory.

    Every non-alphanumeric character becomes ``-``; leading separators produce
    the doubled dashes seen in real project names
    (``/tmp/x/-home-aurelio`` -> ``-tmp-x---home-aurelio``).
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def memory_dir(cwd: str | Path) -> Path:
    """Per-project memory directory for a working directory."""
    return PROJECTS_DIR / project_slug(cwd) / "memory"


def writable_skills() -> List[Path]:
    """SKILL.md paths the loop is permitted to modify.

    Single-level glob on purpose: Claude Code personal skills are flat
    (``~/.claude/skills/find-docs/SKILL.md``), unlike Hermes' ``category/name/``
    nesting. Plugin skills live under ``~/.claude/plugins/`` and are never
    globbed at all, so they are outside the write surface by construction.
    """
    if not SKILLS_DIR.is_dir():
        return []
    found = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            if is_auto_managed(parse_frontmatter(path.read_text(encoding="utf-8"))):
                found.append(path)
        except OSError:
            continue
    return found


def writable_roots(cwd: str | Path) -> Set[Path]:
    """Directories the fork may write inside.

    Existing autoManaged skill directories, the skills root itself (so new
    skills can be created), and the active project's memory directory.
    """
    roots = {skill.parent.resolve() for skill in writable_skills()}
    roots.add(SKILLS_DIR.resolve())
    roots.add(memory_dir(cwd).resolve())
    return roots


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def violates_allowlist(path: Path, cwd: str | Path) -> bool:
    """Is this path outside the write surface?

    A new skill directory under ``SKILLS_DIR`` is allowed -- that is how skills
    get created. But a write to an *existing* skill that lacks the marker is a
    violation even though it sits under ``SKILLS_DIR``.
    """
    roots = writable_roots(cwd)
    if not is_within(path, roots):
        return True
    resolved = path.resolve()
    skills_root = SKILLS_DIR.resolve()
    if skills_root in resolved.parents:
        relative = resolved.relative_to(skills_root)
        if len(relative.parts) > 1:
            skill_dir = skills_root / relative.parts[0]
            existing = skill_dir / "SKILL.md"
            if existing.exists():
                try:
                    marked = is_auto_managed(
                        parse_frontmatter(existing.read_text(encoding="utf-8"))
                    )
                except OSError:
                    return True
                return not marked
    return False


# ---------------------------------------------------------------------------
# Git audit repo (~/.claude/skills) -- local only, never pushed
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    # Resolved at call time, never as a default argument: a default would bind
    # SKILLS_DIR at import and quietly run git against the real skills tree even
    # when a caller (or a test) has pointed the module elsewhere.
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd if cwd is not None else SKILLS_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        # No git, or the skills tree vanished. Degrade to "nothing changed"
        # rather than raising: losing the audit trail is bad, but taking the
        # user's session down with it is worse.
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def ensure_skills_repo() -> None:
    """Initialise the local audit repo if absent. Idempotent."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if (SKILLS_DIR / ".git").is_dir():
        return
    _git("init", "-q")
    _git("config", "user.name", "claude-self-improve")
    _git("config", "user.email", "self-improve@localhost")
    if _git("rev-parse", "HEAD").returncode != 0:
        _git("add", "-A")
        _git("commit", "-q", "--allow-empty", "-m", "Baseline before autonomous writes")


def head_sha() -> Optional[str]:
    result = _git("rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def changed_paths() -> List[tuple[str, Path]]:
    """``[(porcelain_status, absolute_path)]`` for the audit repo."""
    # -uall is required, not cosmetic: without it git collapses an untracked
    # directory to "sneaky/" and the per-file allowlist check never sees the
    # SKILL.md inside, so a brand-new unmarked skill would slip through.
    result = _git("status", "--porcelain=v1", "-z", "-uall")
    if result.returncode != 0:
        return []
    entries: List[tuple[str, Path]] = []
    for chunk in result.stdout.split("\0"):
        if len(chunk) < 4:
            continue
        status, name = chunk[:2], chunk[3:]
        entries.append((status.strip() or "??", SKILLS_DIR / name))
    return entries


def verify_writes(cwd: str | Path) -> List[str]:
    """Revert anything the fork wrote outside the allowlist.

    This is the security boundary. Returns the reverted paths so callers can log
    the violation -- a fork that trips this is a signal worth surfacing, not a
    routine occurrence.
    """
    violations: List[str] = []
    for status, path in changed_paths():
        if not violates_allowlist(path, cwd):
            continue
        violations.append(str(path))
        relative = str(path.relative_to(SKILLS_DIR))
        if status == "??":
            try:
                path.unlink()
            except OSError:
                pass
        else:
            _git("checkout", "--", relative)
    return violations


def commit(message: str) -> Optional[str]:
    """Commit pending changes in the audit repo. ``None`` when nothing changed."""
    if not changed_paths():
        return None
    _git("add", "-A")
    result = _git("commit", "-q", "-m", message)
    if result.returncode != 0:
        return None
    return head_sha()


# ---------------------------------------------------------------------------
# Memory snapshots
#
# The memory directory lives outside the audit repo, so git cannot police it.
# Hash the tree before and after instead, and restore anything unexpected.
# ---------------------------------------------------------------------------


def snapshot_dir(directory: Path) -> Dict[str, str]:
    if not directory.is_dir():
        return {}
    snapshot = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            try:
                snapshot[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
    return snapshot


def diff_snapshot(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return {"added": added, "removed": removed, "modified": modified}


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


@contextmanager
def lock(name: str, timeout: float = 5.0) -> Iterator[bool]:
    """Non-blocking flock with brief retry.

    Yields False when the lock is held elsewhere. Callers should defer to the
    sweep rather than wait -- this runs at session end and must not linger.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    handle = open(LOCK_DIR / f"{safe}.lock", "w")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.25)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
