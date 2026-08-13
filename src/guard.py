"""Safety kernel for patina, the background self-improvement loop.

Every other component imports this. Three jobs:

1. **Recursion prevention.** A headless ``claude -p`` spawned from a hook would
   itself fire that hook. Two independent guards: an env sentinel checked by
   both entry points, and ``--settings '{"hooks":{"disableAllHooks":true}}'`` on
   the child.

2. **Write confinement.** The fork may only touch skills carrying
   ``metadata.autoManaged: true``. Enforced at two layers -- ``--add-dir`` on the child (a harness boundary) and
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
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

HOME = Path.home()


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read ``PATINA_<name>``, falling back to the pre-rename spelling.

    The project was called claude-self-improve until it was renamed. The old
    variables keep working: they may still be exported in a shell profile, and
    a config surface that silently stops being read is worse than two spellings.
    """
    return (
        os.environ.get(f"PATINA_{name}")
        or os.environ.get(f"CLAUDE_SELF_IMPROVE_{name}")
        or default
    )


# install.sh honours the same variable; the two must agree or the installer
# would wire hooks to a tree the scripts never look at.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
# Write targets are independently overridable. This is what makes a real
# end-to-end rehearsal possible: point these at a scratch tree while
# CLAUDE_CONFIG_DIR stays on the real config, so the fork authenticates
# normally but cannot touch the live skill library.
SKILLS_DIR = Path(env("SKILLS_DIR") or (CLAUDE_DIR / "skills"))
PROJECTS_DIR = Path(env("PROJECTS_DIR") or (CLAUDE_DIR / "projects"))
STATE_DIR = Path(env("STATE_DIR") or (CLAUDE_DIR / "patina"))
LOCK_DIR = STATE_DIR / ".locks"

#: Staging tree. New skills and proposed patches land here for review; nothing
#: reaches SKILLS_DIR until the user approves it.
PENDING_DIR = STATE_DIR / "pending"

#: Scratch trees handed to the fork. Deliberately OUTSIDE ~/.claude: Claude Code
#: treats its own config directory as sensitive and refuses Write there no
#: matter what --add-dir and --permission-mode say --
#:
#:     Write requires permission approval for sensitive files in ~/.claude/
#:
#: A fork staged inside ~/.claude therefore saves nothing, silently, while
#: reporting success. Staging out here means the child never needs elevated
#: permission at all; our own Python moves approved content in afterwards.
WORK_ROOT = Path(env("WORK_DIR") or (HOME / ".cache" / "patina"))
WORK_DIR = WORK_ROOT / "skills"

#: Records which auto-created skills the user has blessed, and which they have
#: refused. See skillgate.py.
APPROVALS_FILE = STATE_DIR / "approvals.json"

#: Env sentinel. Set on every child; both hook entry points exit if they see it.
SENTINEL = "PATINA_CHILD"

#: Pre-rename spelling, still honoured: a fork spawned by the old scripts may
#: outlive the upgrade, and it must not be treated as a user session.
LEGACY_SENTINEL = "CLAUDE_SELF_IMPROVE_CHILD"

#: First line of every prompt handed to a fork. The sentinel above stops a fork
#: reviewing *itself*; this stops the sweep reviewing a fork *later*. The fork's
#: own transcript is written to ``~/.claude/projects`` like any other session,
#: and a sweep that picked it up would feed the review prompt back into the
#: review prompt.
FORK_MARKER = "[patina fork — not a user session, never review]"

#: Forks that ran before the marker existed. Matched against the first record
#: of a transcript only, which for a fork is the prompt itself -- a user session
#: that merely discusses these strings does not open with them.
_LEGACY_FORK_OPENINGS = (
    "[claude-self-improve fork",
    "You are reviewing a finished Claude Code session",
    "You are the curator for a Claude Code skill library",
)

#: Frontmatter key that marks a skill as ours to write. No marker, no write.
AUTO_MANAGED_KEY = "autoManaged"


# ---------------------------------------------------------------------------
# Recursion guards
# ---------------------------------------------------------------------------


def is_child() -> bool:
    """True when running inside a fork we spawned. Entry points must exit."""
    return "1" in (os.environ.get(SENTINEL), os.environ.get(LEGACY_SENTINEL))


def child_env() -> Dict[str, str]:
    """Environment for the forked ``claude -p``."""
    child = dict(os.environ)
    child[SENTINEL] = "1"
    return child


#: Passed to every child. The second half of the recursion guard, and the
#: reason a detached process beats a subagent: this is a real process boundary.
CHILD_SETTINGS = json.dumps({"hooks": {"disableAllHooks": True}})


# ---------------------------------------------------------------------------
# The fork
# ---------------------------------------------------------------------------

#: What a fork may reach. Write access is scoped by ``--add-dir`` on top of
#: this; the denials are what stop it reaching the network or shelling out.
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
DENIED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]

#: Hard ceiling per fork, in dollars. Turns and a timeout bound how *long* a
#: fork runs, not what it costs: a single turn can be arbitrarily expensive, and
#: a daily sweep multiplies whatever one review costs by its batch size.
MAX_BUDGET_USD = env("MAX_USD", "0.50")

#: Optional. Claude Code falls back when the primary model is overloaded or
#: unavailable. Unset by default: a quieter model silently producing weaker
#: skills is worse than a review that fails and gets retried.
FALLBACK_MODEL = env("FALLBACK_MODEL", "")


def fork_command(
    prompt: str,
    *,
    model: str,
    max_turns: int,
    add_dirs: Iterable[Path] = (),
    tools: Optional[List[str]] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Argv for a headless child.

    One place, because the two entry points had drifted into near-identical
    twenty-line lists and a flag added to one was a flag missing from the other.

    ``tools=[]`` means a pass that reads and writes nothing -- it only thinks.
    ``schema`` forces structured output, which is what lets callers branch on
    what the fork decided instead of grepping its prose for a phrase.
    """
    allowed = ALLOWED_TOOLS if tools is None else tools
    command = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--settings",
        CHILD_SETTINGS,
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
        # The fork's own transcript is of no use to anyone and the sweep has to
        # be taught to ignore it. Not writing one is simpler than excluding it.
        "--no-session-persistence",
    ]
    if MAX_BUDGET_USD:
        command += ["--max-budget-usd", str(MAX_BUDGET_USD)]
    if FALLBACK_MODEL:
        command += ["--fallback-model", FALLBACK_MODEL]
    if schema is not None:
        command += ["--json-schema", json.dumps(schema)]
    for directory in add_dirs:
        command += ["--add-dir", str(directory)]
    if allowed:
        command += ["--allowedTools", *allowed]
    command += ["--disallowedTools", *DENIED_TOOLS]
    return command


class ForkResult:
    """What a fork returned, whatever shape it came back in.

    ``--output-format json`` is requested, but a CLI that changes its envelope
    must degrade to "the reply is whatever it printed" rather than take the loop
    offline. Everything here is best-effort except ``text``.
    """

    def __init__(self, text: str = "", structured: Any = None,
                 cost_usd: Optional[float] = None, subtype: str = "") -> None:
        self.text = text
        self.structured = structured
        self.cost_usd = cost_usd
        self.subtype = subtype

    @property
    def hit_budget(self) -> bool:
        """Did this fork stop because it ran out of money rather than ideas?

        Worth distinguishing: a ceiling set too low fails every attempt, and
        counting those as ordinary crashes spends the retry budget on a
        configuration problem that retrying cannot fix.
        """
        haystack = f"{self.subtype} {self.text}".lower()
        return "budget" in haystack and "exceed" in haystack or "max_budget" in haystack

    @property
    def hit_rate_limit(self) -> bool:
        """Did this fork fail because the account is out of usage right now?

        A different thing from every other failure: it says nothing about the
        transcript. Counting it as an ordinary error spends the retry budget on
        a condition that clears by itself, and the session it was reviewing --
        which may be the one with the lesson in it -- is dropped for a reason
        that had nothing to do with it.
        """
        haystack = f"{self.subtype} {self.text}".lower()
        return any(
            phrase in haystack
            for phrase in (
                "session limit",
                "usage limit",
                "rate limit",
                "rate_limit",
                "429",
                "try again later",
                "resets at",
                "upgrade to increase your usage limit",
            )
        )


def parse_fork_result(stdout: str) -> ForkResult:
    raw = (stdout or "").strip()
    if not raw:
        return ForkResult()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ForkResult(text=raw)
    if not isinstance(payload, dict):
        return ForkResult(text=raw)
    result = payload.get("result")
    cost = payload.get("total_cost_usd")
    if isinstance(result, str):
        text = result.strip()
    elif payload.get("is_error") or payload.get("subtype"):
        # A failed run returns the envelope with no ``result`` at all. Falling
        # back to the raw JSON here would log a screenful of usage counters as
        # the fork's reply.
        text = str(payload.get("errors") or "")
    else:
        text = raw
    return ForkResult(
        text=text,
        structured=payload.get("structured_output"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        subtype=str(payload.get("subtype") or ""),
    )


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


def own_project_slugs() -> Set[str]:
    """Project directory names Claude Code gives the loop's own trees.

    A fork runs with ``cwd`` set to the work tree, so its transcript lands in
    the project directory named after it. Deriving the slugs rather than
    hard-coding them keeps the rehearsal overrides working.
    """
    return {project_slug(root) for root in (WORK_ROOT, WORK_DIR, SKILLS_DIR, STATE_DIR)}


def is_own_transcript(path: Path) -> bool:
    """True when this transcript is a fork we spawned, not a user session.

    Two checks because neither alone is enough: the slug covers every fork this
    installation will spawn from now on, and the prompt marker covers forks
    from an earlier configuration whose work tree no longer exists.
    """
    if Path(path).parent.name in own_project_slugs():
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.readline(32768)
    except OSError:
        return False
    return FORK_MARKER in head or any(sign in head for sign in _LEGACY_FORK_OPENINGS)


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


def writable_roots() -> Set[Path]:
    """Directories the fork may write inside.

    Existing autoManaged skill directories and the skills root itself, so new
    skills can be created.
    """
    roots = {skill.parent.resolve() for skill in writable_skills()}
    roots.add(SKILLS_DIR.resolve())
    return roots


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def violates_allowlist(path: Path) -> bool:
    """Is this path outside the write surface?

    A new skill directory under ``SKILLS_DIR`` is allowed -- that is how skills
    get created. But a write to an *existing* skill that lacks the marker is a
    violation even though it sits under ``SKILLS_DIR``.
    """
    roots = writable_roots()
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
    _git("config", "user.name", "patina")
    _git("config", "user.email", "patina@localhost")
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


def verify_writes() -> List[str]:
    """Revert anything the fork wrote outside the allowlist.

    This is the security boundary. Returns the reverted paths so callers can log
    the violation -- a fork that trips this is a signal worth surfacing, not a
    routine occurrence.
    """
    violations: List[str] = []
    for status, path in changed_paths():
        if not violates_allowlist(path):
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
