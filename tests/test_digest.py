"""Tests for transcript bounding.

Run: python3 -m pytest tests/ -q   (or python3 tests/test_digest.py)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import digest  # noqa: E402


def write_transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def user(text: str, **extra) -> dict:
    return {
        "type": "user",
        "cwd": "/home/u/proj",
        "gitBranch": "main",
        "timestamp": "2026-08-07T00:00:00Z",
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(text: str = "", tools: list[dict] | None = None, **extra) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in tools or []:
        content.append({"type": "tool_use", **tool})
    return {
        "type": "assistant",
        "cwd": "/home/u/proj",
        "timestamp": "2026-08-07T00:01:00Z",
        "message": {"role": "assistant", "content": content},
        **extra,
    }


# --- parsing ---------------------------------------------------------------


def test_metadata_records_are_ignored(tmp_path):
    path = write_transcript(
        tmp_path,
        [
            {"type": "mode", "mode": "normal"},
            {"type": "last-prompt", "leafUuid": "x"},
            {"type": "ai-title", "title": "t"},
            user("hello"),
        ],
    )
    messages, meta = digest.parse_transcript(path)
    assert len(messages) == 1
    assert meta.skipped_lines == 0


def test_sidechain_records_dropped(tmp_path):
    path = write_transcript(
        tmp_path,
        [user("real"), user("subagent noise", isSidechain=True)],
    )
    messages, _ = digest.parse_transcript(path)
    assert [m.text for m in messages] == ["real"]


def test_malformed_lines_counted_not_fatal(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(user("good")) + "\n{ not json\n" + json.dumps(user("also good")),
        encoding="utf-8",
    )
    messages, meta = digest.parse_transcript(path)
    assert len(messages) == 2
    assert meta.skipped_lines == 1


def test_missing_file_returns_empty(tmp_path):
    messages, meta = digest.parse_transcript(tmp_path / "nope.jsonl")
    assert messages == []
    assert meta.message_count == 0


def test_tool_use_names_captured(tmp_path):
    path = write_transcript(
        tmp_path,
        [assistant("working", tools=[{"name": "Read"}, {"name": "Edit"}])],
    )
    messages, _ = digest.parse_transcript(path)
    assert messages[0].tools == ["Read", "Edit"]


def test_skills_loaded_from_skill_tool_and_skill_md_reads(tmp_path):
    path = write_transcript(
        tmp_path,
        [
            assistant(tools=[{"name": "Skill", "input": {"skill": "brainstorming"}}]),
            assistant(
                tools=[
                    {
                        "name": "Read",
                        "input": {"file_path": "/home/u/.claude/skills/deploy/SKILL.md"},
                    }
                ]
            ),
        ],
    )
    _, meta = digest.parse_transcript(path)
    assert meta.skills_loaded == ["brainstorming", "deploy"]


def test_session_metadata_extracted(tmp_path):
    path = write_transcript(tmp_path, [user("a"), assistant("b")])
    _, meta = digest.parse_transcript(path)
    assert meta.cwd == "/home/u/proj"
    assert meta.git_branch == "main"
    assert meta.started == "2026-08-07T00:00:00Z"
    assert meta.ended == "2026-08-07T00:01:00Z"


# --- cleaning --------------------------------------------------------------


def test_system_reminder_stripped():
    assert "reminder" not in digest.clean("keep <system-reminder>reminder</system-reminder>")


def test_local_command_caveat_stripped():
    dirty = "<local-command-caveat>DO NOT respond to these messages</local-command-caveat>"
    assert digest.clean(dirty) == ""


def test_command_name_compacted():
    assert digest.clean("<command-name>/effort</command-name>") == "[ran /effort]"


# --- rendering -------------------------------------------------------------


def test_short_session_is_all_verbatim(tmp_path):
    path = write_transcript(tmp_path, [user("one"), assistant("two")])
    result = digest.build(path)
    assert "verbatim" in result.text
    assert "Earlier conversation digest" not in result.text


def test_older_turns_are_summarised(tmp_path):
    records = [user(f"message {i}") for i in range(60)]
    path = write_transcript(tmp_path, records)
    result = digest.build(path, tail=10)
    assert "Earlier conversation digest" in result.text
    assert "message 59" in result.text  # tail kept verbatim
    assert "USER: message 0" in result.text  # older summarised, still present


def test_summary_truncates_long_user_text(tmp_path):
    long_text = "x" * 1000
    records = [user(long_text)] + [user(f"m{i}") for i in range(30)]
    path = write_transcript(tmp_path, records)
    result = digest.build(path, tail=5)
    assert "x" * 300 in result.text
    assert "x" * 400 not in result.text


def test_ceiling_shrinks_tail_not_summary(tmp_path):
    records = [user("y" * 5000) for _ in range(50)]
    path = write_transcript(tmp_path, records)
    result = digest.build(path, tail=24, max_chars=20_000)
    assert len(result.text) <= 20_000 + 100
    assert result.truncated
    # The summary survives -- it is what gives the review a sense of the session.
    assert "Earlier conversation digest" in result.text


def test_empty_transcript_still_renders_header(tmp_path):
    path = write_transcript(tmp_path, [])
    result = digest.build(path)
    assert "[Session context]" in result.text



def test_control_bytes_are_stripped_from_the_digest(tmp_path):
    """A NUL anywhere in a transcript used to cost the whole review.

    The digest is handed to the fork as an argv element, and subprocess refuses
    one containing a NUL with ValueError('embedded null byte') -- which the hook
    caught and logged as an unexplained error, losing the session.
    """
    path = write_transcript(tmp_path, [
        user("here is a hexdump: \x00\x01\x02 and a bell \x07"),
        assistant("noted\x00"),
    ])
    result = digest.build(path)
    assert "\x00" not in result.text
    assert not any(ch in result.text for ch in "\x01\x07")
    assert "hexdump" in result.text and "noted" in result.text


def test_a_control_byte_in_the_session_metadata_is_stripped_too(tmp_path):
    """cwd and branch come from the transcript and never pass through clean()."""
    path = write_transcript(tmp_path, [user("hi", cwd="/home/u/pro\x00ject")])
    assert "\x00" not in digest.build(path).text


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
