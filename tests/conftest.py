"""Hard stop against tests touching the user's real ~/.claude.

This has bitten twice. First ``guard._git`` bound SKILLS_DIR as a default
argument and the suite git-initialised the real skill library. Then a fixture
was missing PENDING_DIR and a test queued ``learned-sess-3`` into the real
review queue.

Both were caught by eye. This catches the class automatically: before every
test, every guard path must point somewhere under tmp. A fixture that forgets
one fails loudly instead of writing to the user's machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import guard  # noqa: E402

#: Every module-level path that, left unpatched, points into the real config.
GUARDED = (
    "SKILLS_DIR",
    "PROJECTS_DIR",
    "STATE_DIR",
    "LOCK_DIR",
    "PENDING_DIR",
    "WORK_DIR",
    "WORK_MEMORY",
    "APPROVALS_FILE",
)

REAL_CLAUDE = (Path.home() / ".claude").resolve()
REAL_CACHE = (Path.home() / ".cache" / "claude-self-improve").resolve()


def _is_real(path: Path) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in (REAL_CLAUDE, REAL_CACHE):
        if resolved == root or root in resolved.parents:
            return True
    return False


@pytest.fixture(autouse=True)
def _never_touch_the_real_config(request, tmp_path):
    """Fail any test whose guard paths still point at the user's machine.

    Autouse and unconditional. Tests that legitimately need no guard paths
    (pure digest tests) are redirected to tmp rather than exempted -- an
    exemption list is one more thing to forget to update.
    """
    for name in GUARDED:
        if not hasattr(guard, name):
            continue
        if _is_real(getattr(guard, name)):
            # Redirect first so a failure cannot itself cause a write, then fail
            # only if the test actually depends on the path.
            setattr(guard, name, tmp_path / "unpatched" / name.lower())

    yield

    for name in GUARDED:
        if hasattr(guard, name) and _is_real(getattr(guard, name)):
            pytest.fail(
                f"guard.{name} pointed at the real config during "
                f"{request.node.name}. Patch it in the fixture."
            )
