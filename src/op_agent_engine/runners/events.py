"""Parsing of the agent's JSONL event stream.

The agent CLI can emit one JSON object per line instead of a text transcript
(pi: ``--mode json``). That stream is the only place where intermediate
messages exist: text print mode writes nothing until the process exits, so a
50-minute run and a hung run look identical on disk.

This module consumes those lines and keeps three things:

* the **last assistant message text** -- the dashboard's ``last message`` line
  and the post-mortem of a silent run (WP 356, WP 366);
* the last ``RESULT:`` line found in assistant text, so outcome handling keeps
  working exactly as with text mode (WP 356);
* nothing else. Thinking blocks, tool calls and tool output are ignored: they
  are not what the agent said.

The parser is fed one line at a time from the stdout pump, so it must stay
cheap and must never raise: a malformed line is skipped. The authoritative
text is written to ``runs/<id>/last_message.txt`` atomically (temp file plus
rename) so the dashboard never reads a half-written file.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from op_agent_engine.logging import get_logger

logger = get_logger(__name__)

#: The file the dashboard reads for the "last message" line.
LAST_MESSAGE_FILE = "last_message.txt"

#: Upper bound for the stored message. The dashboard shows the last two
#: sentences (about 300 characters); the file keeps context for post-mortems
#: without letting a chatty agent grow it without limit.
MAX_MESSAGE_BYTES = 4096

RESULT_PREFIX = "RESULT:"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def assistant_text(message: Any) -> str | None:
    """The text content blocks of one assistant message, joined.

    Returns ``None`` when the message is not an assistant message with text
    (a tool-only turn, a user message, thinking-only output).
    """
    payload = _as_dict(message)
    if payload.get("role") != "assistant":
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts) if parts else None


def result_line_from_text(text: str) -> str | None:
    """The last ``RESULT:`` line in *text*, stripped, or ``None``."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(RESULT_PREFIX):
            return stripped
    return None


@dataclass
class AssistantStream:
    """Accumulates the assistant's messages from JSONL events.

    ``feed_line`` is the entry point used by the runner's stdout pump. The
    object is not thread safe: one pump drives it.
    """

    path: Path
    last_message: str | None = None
    result_line: str | None = None
    messages_seen: int = 0
    _deltas: dict[int, str] = field(default_factory=dict)

    def feed_line(self, raw: bytes | str) -> bool:
        """Consume one line. Returns True when the last message changed.

        JSON decode errors, unknown event types and events without assistant
        text are ignored: this parser must never fail a run.
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        line = raw.strip()
        if not line:
            return False
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False

        kind = event.get("type")
        if kind == "message_update":
            self._feed_update(_as_dict(event.get("assistantMessageEvent")))
            return False
        if kind == "message_end":
            text = assistant_text(event.get("message"))
            if text is None:
                return False
            self._deltas.clear()
            return self._store(text)
        return False

    def _feed_update(self, update: dict[str, Any]) -> None:
        """Track deltas so a killed run still shows what it was saying."""
        kind = update.get("type")
        index = update.get("contentIndex")
        if not isinstance(index, int):
            return
        if kind == "text_delta":
            delta = update.get("delta")
            if isinstance(delta, str):
                self._deltas[index] = self._deltas.get(index, "") + delta
        elif kind == "text_end":
            content = update.get("content")
            if isinstance(content, str):
                self._deltas[index] = content
        elif kind in {"thinking_start", "thinking_delta", "thinking_end"}:
            # Thinking is not a message. Drop it explicitly so nobody adds it
            # to the dashboard later by accident.
            self._deltas.pop(index, None)

    def _store(self, text: str) -> bool:
        self.messages_seen += 1
        found = result_line_from_text(text)
        if found is not None:
            self.result_line = found
        if text == self.last_message:
            return False
        self.last_message = text
        self.write_last_message()
        return True

    def write_last_message(self) -> None:
        """Write the stored message atomically, bounded to ``MAX_MESSAGE_BYTES``."""
        text = self.last_message or ""
        encoded = text.encode("utf-8")[-MAX_MESSAGE_BYTES:]
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, self.path)
        except OSError as error:  # pragma: no cover - disk trouble only
            logger.warning(
                "could not write the last agent message to %s: %s", self.path, error
            )

    @property
    def partial_text(self) -> str:
        """Text received so far in the current message (deltas only)."""
        return "".join(self._deltas[key] for key in sorted(self._deltas))


def write_last_message_atomically(path: Path, text: str) -> None:
    """Write *text* to *path* via a temp file, bounded, tolerating IO errors."""
    stream = AssistantStream(path=path, last_message=text)
    stream.write_last_message()


def cleanup_partial(path: Path) -> None:
    """Remove a stray temp file left by an interrupted write."""
    with contextlib.suppress(OSError):
        path.with_name(path.name + ".tmp").unlink()
