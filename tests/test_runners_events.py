"""Tests for the JSONL event parser (WP 356).

The parser turns the agent's ``--mode json`` stream into the last assistant
message and the ``RESULT:`` line, ignoring thinking and tool calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from op_agent_engine.runners.events import (
    MAX_MESSAGE_BYTES,
    AssistantStream,
    assistant_text,
    result_line_from_text,
)


def event(**payload: Any) -> str:
    return json.dumps(payload)


def assistant(stream: AssistantStream, text: str) -> bool:
    """Feed one complete assistant message, as pi ends one."""
    return stream.feed_line(
        event(
            type="message_end",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        )
    )


def stream(tmp_path: Path) -> AssistantStream:
    return AssistantStream(path=tmp_path / "last_message.txt")


# -- assistant_text ----------------------------------------------------------


def test_assistant_text_joins_text_blocks_only() -> None:
    message = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "first "},
            {"type": "toolCall", "name": "bash"},
            {"type": "text", "text": "second"},
        ],
    }
    assert assistant_text(message) == "first second"


def test_assistant_text_ignores_user_and_tool_only_messages() -> None:
    assert assistant_text({"role": "user", "content": "hi"}) is None
    assert (
        assistant_text({"role": "assistant", "content": [{"type": "toolCall"}]})
        is None
    )
    assert assistant_text({"role": "assistant", "content": []}) is None
    assert assistant_text(None) is None


# -- result_line_from_text ---------------------------------------------------


def test_result_line_from_text_takes_the_last_one() -> None:
    text = "did things\nRESULT: ok\nmore\nRESULT: BLOCKED need input"
    assert result_line_from_text(text) == "RESULT: BLOCKED need input"


def test_result_line_from_text_tolerates_absent_and_spaced() -> None:
    assert result_line_from_text("no result here") is None
    assert result_line_from_text("   RESULT:   spaced  ") == "RESULT:   spaced"


# -- streaming ---------------------------------------------------------------


def test_message_end_stores_the_message_and_writes_the_file(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assert assistant(parser, "Hello from the agent.") is True
    assert parser.last_message == "Hello from the agent."
    assert (tmp_path / "last_message.txt").read_text() == "Hello from the agent."


def test_an_unchanged_message_is_not_rewritten(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "same")
    assert assistant(parser, "same") is False
    assert parser.messages_seen == 2


def test_thinking_and_tool_events_never_reach_the_message(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "thinking_delta", "contentIndex": 0, "delta": "secret"},
        )
    )
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "text_delta", "contentIndex": 1, "delta": "visible "},
        )
    )
    # A complete message replaces the deltas; thinking is dropped.
    assistant(parser, "visible and final")
    assert parser.last_message == "visible and final"
    assert "secret" not in parser.last_message


def test_deltas_are_available_while_a_message_streams(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "partial "},
        )
    )
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "text"},
        )
    )
    assert parser.partial_text == "partial text"


def test_text_end_replaces_the_deltas(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "dropped"},
        )
    )
    parser.feed_line(
        event(
            type="message_update",
            assistantMessageEvent={"type": "text_end", "contentIndex": 0, "content": "final"},
        )
    )
    assert parser.partial_text == "final"


def test_result_line_is_picked_up_from_json_mode(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "work done\nRESULT: PR opened")
    assert parser.result_line == "RESULT: PR opened"


def test_the_last_result_line_wins_across_messages(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "RESULT: first")
    assistant(parser, "RESULT: second")
    assert parser.result_line == "RESULT: second"


def test_a_message_without_result_keeps_the_earlier_one(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "RESULT: PR opened")
    assistant(parser, "just chatting")
    assert parser.result_line == "RESULT: PR opened"
    assert parser.last_message == "just chatting"


def test_malformed_lines_are_ignored(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assert parser.feed_line(b"not json") is False
    assert parser.feed_line(b"") is False
    assert parser.feed_line(b'{"type": "agent_start"}') is False
    assert parser.feed_line(b"[1, 2, 3]") is False
    assert parser.last_message is None
    assert not (tmp_path / "last_message.txt").exists()


def test_the_stored_message_is_bounded(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "x" * (MAX_MESSAGE_BYTES * 2))
    stored = (tmp_path / "last_message.txt").read_bytes()
    assert len(stored) == MAX_MESSAGE_BYTES
    assert stored.endswith(b"x")


def test_the_file_is_replaced_atomically(tmp_path: Path) -> None:
    parser = stream(tmp_path)
    assistant(parser, "first message")
    assistant(parser, "second message")
    assert (tmp_path / "last_message.txt").read_text() == "second message"
    assert not (tmp_path / "last_message.txt.tmp").exists()
