"""Transcript -> bounded digest.

Hermes replays the whole conversation into its review fork because the fork
shares the parent's prompt cache, making replay nearly free. A headless
``claude -p`` is a separate process with a cold cache, and transcripts here run
to several megabytes of JSONL. So bounding is mandatory, not an optimisation.

Port of Hermes' ``_digest_history``: keep the recent tail verbatim, collapse
everything older to one line apiece. On overflow the tail shrinks before the
summary does -- recent turns carry the corrections, which are the whole point.

Pure functions, no side effects, no I/O beyond reading the path handed in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Messages kept verbatim at the tail.
DEFAULT_TAIL = 24

#: Ceiling on the rendered digest. Roughly 30k tokens -- generous for a review
#: fork, small enough that a 4.7 MB transcript cannot blow up the run.
DEFAULT_MAX_CHARS = 120_000

#: Truncation widths for collapsed older turns, matching Hermes.
_USER_SUMMARY_CHARS = 300
_ASSISTANT_SUMMARY_CHARS = 200

#: Record types that carry no conversational content.
_METADATA_TYPES = {
    "last-prompt",
    "mode",
    "permission-mode",
    "bridge-session",
    "ai-title",
    "file-history-snapshot",
    "summary",
}


#: Tools whose use means the session changed something, as opposed to reading
#: around. Kept separate from the raw tool count because a long investigation
#: that edits nothing can still carry a lesson worth keeping.
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass
class Message:
    role: str
    text: str = ""
    tools: List[str] = field(default_factory=list)
    timestamp: Optional[str] = None
    #: A user record carrying tool output rather than something the user typed.
    #: Both arrive with ``role: user``; only one of them is a turn.
    is_tool_result: bool = False


@dataclass
class Digest:
    text: str
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    started: Optional[str] = None
    ended: Optional[str] = None
    skills_loaded: List[str] = field(default_factory=list)
    message_count: int = 0
    skipped_lines: int = 0
    truncated: bool = False
    #: What the session actually did. The review gate reads these: forking a
    #: model over a three-message session costs the same as forking over a real
    #: one, and asks it to find a lesson that is not there.
    tool_calls: int = 0
    edit_calls: int = 0
    user_turns: int = 0


#: Slash-command and harness plumbing that carries no lesson. Stripped rather
#: than skipped wholesale, since ``<command-name>`` is worth keeping in compact
#: form -- which skills and commands ran is signal.
_STRIP_BLOCKS = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|"
    r"command-message|command-args)>.*?</\1>",
    re.DOTALL,
)
_COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_EMPTY_TAGS = re.compile(r"</?(local-command-caveat|local-command-stdout)>")

#: Control characters that survive JSON decoding and whitespace splitting.
#: A NUL among them is fatal rather than ugly: the digest is handed to the fork
#: as an argv element, and ``subprocess`` refuses an argument containing one
#: with ``ValueError: embedded null byte``. A single such byte anywhere in a
#: transcript -- a hexdump, a binary file read, a terminal capture -- therefore
#: cost the whole review. Stripped at the one place every message passes
#: through.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Remove harness plumbing, keeping invoked command names."""
    text = _STRIP_BLOCKS.sub("", text)
    text = _COMMAND_NAME.sub(lambda m: f"[ran {m.group(1)}]", text)
    text = _EMPTY_TAGS.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    return " ".join(text.split())


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        return str(block.get("text") or "")
    if block.get("type") == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(_block_text(b) for b in content)
    return ""


def _extract(record: Dict[str, Any]) -> Optional[Message]:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role") or record.get("type")
    content = message.get("content")

    text_parts: List[str] = []
    tools: List[str] = []
    spoken = False
    tool_output = False

    if isinstance(content, str):
        text_parts.append(content)
        spoken = True
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.append(str(block.get("name") or "?"))
                continue
            chunk = _block_text(block)
            if not chunk:
                continue
            text_parts.append(chunk)
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_output = True
            else:
                spoken = True

    text = clean(" ".join(p.strip() for p in text_parts if p and p.strip()))
    if not text and not tools:
        return None
    return Message(
        role=str(role),
        text=text,
        tools=tools,
        timestamp=record.get("timestamp"),
        is_tool_result=tool_output and not spoken,
    )


def _skill_names(record: Dict[str, Any]) -> List[str]:
    """Skills invoked this session.

    The review's preference order leans on this -- a skill that was in play is
    the right one to extend, so the fork needs to know which ones were loaded.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        payload = block.get("input") or {}
        if block.get("name") == "Skill" and isinstance(payload, dict):
            skill = payload.get("skill")
            if skill:
                names.append(str(skill))
        elif block.get("name") == "Read" and isinstance(payload, dict):
            path = str(payload.get("file_path") or "")
            if path.endswith("SKILL.md"):
                names.append(Path(path).parent.name)
    return names


def parse_transcript(path: str | Path) -> tuple[List[Message], Digest]:
    """Read a transcript into messages plus session metadata.

    Unparseable lines are counted, never fatal -- a malformed tail must not cost
    the whole review.
    """
    meta = Digest(text="")
    messages: List[Message] = []
    seen_skills: List[str] = []

    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return [], meta

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                meta.skipped_lines += 1
                continue
            if not isinstance(record, dict):
                meta.skipped_lines += 1
                continue

            kind = record.get("type")
            if kind in _METADATA_TYPES:
                continue
            # Subagent chatter is not the user's lesson.
            if record.get("isSidechain"):
                continue
            if kind not in ("user", "assistant"):
                continue

            meta.cwd = record.get("cwd") or meta.cwd
            meta.git_branch = record.get("gitBranch") or meta.git_branch
            stamp = record.get("timestamp")
            if stamp:
                meta.started = meta.started or stamp
                meta.ended = stamp

            for name in _skill_names(record):
                if name not in seen_skills:
                    seen_skills.append(name)

            message = _extract(record)
            if message:
                messages.append(message)

    meta.skills_loaded = seen_skills
    meta.message_count = len(messages)
    meta.tool_calls = sum(len(m.tools) for m in messages)
    meta.edit_calls = sum(
        1 for m in messages for name in m.tools if name in _EDIT_TOOLS
    )
    meta.user_turns = sum(
        1 for m in messages if m.role == "user" and not m.is_tool_result
    )
    return messages, meta


def _summarise(message: Message) -> Optional[str]:
    flat = " ".join(message.text.split())
    if message.role == "user":
        return f"USER: {flat[:_USER_SUMMARY_CHARS]}" if flat else None
    lines = []
    if message.tools:
        lines.append(f"ASSISTANT[tools: {', '.join(message.tools)}]")
    if flat:
        lines.append(f"ASSISTANT: {flat[:_ASSISTANT_SUMMARY_CHARS]}")
    return "\n".join(lines) if lines else None


def _render(message: Message) -> str:
    parts = [f"{message.role.upper()}:"]
    if message.text:
        parts.append(message.text)
    if message.tools:
        parts.append(f"[tools: {', '.join(message.tools)}]")
    return " ".join(parts)


def render(
    messages: List[Message],
    meta: Digest,
    tail: int = DEFAULT_TAIL,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Digest:
    """Build the digest text, shrinking the verbatim tail until it fits."""
    header = [
        "[Session context]",
        f"cwd: {meta.cwd or 'unknown'}",
        f"git branch: {meta.git_branch or 'n/a'}",
        f"messages: {meta.message_count}",
        f"user turns: {meta.user_turns}  tool calls: {meta.tool_calls}"
        f"  file edits: {meta.edit_calls}",
    ]
    if meta.started:
        header.append(f"started: {meta.started}")
    if meta.ended:
        header.append(f"ended: {meta.ended}")
    if meta.skills_loaded:
        header.append(f"skills loaded this session: {', '.join(meta.skills_loaded)}")
    if meta.skipped_lines:
        header.append(f"unparseable transcript lines skipped: {meta.skipped_lines}")
    header_text = "\n".join(header)

    current_tail = max(0, min(tail, len(messages)))
    truncated = False

    while True:
        older = messages[: len(messages) - current_tail] if current_tail else messages
        recent = messages[len(messages) - current_tail :] if current_tail else []

        sections = [header_text]
        if older:
            summary = [s for s in (_summarise(m) for m in older) if s]
            sections.append(
                "[Earlier conversation digest -- older turns summarised to bound "
                "the review's cold-start cost. Recent turns follow verbatim.]\n"
                + "\n".join(summary)
            )
        if recent:
            sections.append(
                "[Recent turns, verbatim]\n"
                + "\n\n".join(_render(m) for m in recent)
            )

        # Header fields (cwd, branch, skill names) come from the transcript too
        # and never pass through clean(). One sweep here covers them.
        text = _CONTROL_CHARS.sub("", "\n\n".join(sections))
        if len(text) <= max_chars or current_tail == 0:
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[digest truncated at ceiling]"
                truncated = True
            meta.text = text
            meta.truncated = truncated
            return meta

        # Over ceiling: shrink the verbatim tail, never the summary. Recent
        # turns are the expensive part and the summary is what gives the review
        # its sense of the whole session.
        current_tail = current_tail // 2
        truncated = True


def build(
    path: str | Path,
    tail: int = DEFAULT_TAIL,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Digest:
    """Transcript path -> rendered digest."""
    messages, meta = parse_transcript(path)
    return render(messages, meta, tail=tail, max_chars=max_chars)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: digest.py <transcript.jsonl>")
    result = build(sys.argv[1])
    print(result.text)
    print(
        f"\n--- {result.message_count} messages, {len(result.text)} chars, "
        f"truncated={result.truncated}, skipped={result.skipped_lines}",
        file=sys.stderr,
    )
