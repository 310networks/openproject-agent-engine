from __future__ import annotations

import io
import json
import logging
import sys

from op_agent_engine.logging import (
    REDACTED,
    JsonFormatter,
    configure_logging,
    get_logger,
    redact_mapping,
)


def _emit_and_read(**extra: object) -> str:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream, environ={"OP_ENGINE_TOKEN": "s3cret-token"})
    get_logger("test").info("hello %s", "world", extra=extra)
    return stream.getvalue().strip()


def test_json_log_line_contains_standard_fields() -> None:
    record = _emit_and_read(run_id=7, work_package_id=42, agent="frontendEngineer")

    payload = json.loads(record)
    assert payload["level"] == "INFO"
    assert payload["event"] == "hello world"
    assert payload["run_id"] == 7
    assert payload["work_package_id"] == 42
    assert payload["agent"] == "frontendEngineer"
    assert "timestamp" in payload


def test_secret_values_are_redacted_from_messages() -> None:
    record = _emit_and_read(detail="token is s3cret-token here")

    payload = json.loads(record)
    assert "s3cret-token" not in record
    assert REDACTED in payload["detail"]


def test_secret_named_fields_are_redacted() -> None:
    record = _emit_and_read(GH_TOKEN="ghp_plaintext", OP_ENGINE_TOKEN="another")

    payload = json.loads(record)
    assert payload["GH_TOKEN"] == REDACTED
    assert payload["OP_ENGINE_TOKEN"] == REDACTED
    assert "ghp_plaintext" not in record


def test_redact_mapping_handles_nested_values_by_name() -> None:
    redacted = redact_mapping({"OP_BASE_URL": "https://example.com", "other": "visible"})

    assert redacted["OP_BASE_URL"] == REDACTED
    assert redacted["other"] == "visible"


def test_configure_logging_replaces_its_own_handler() -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging("INFO", stream=first, environ={})
    configure_logging("INFO", stream=second, environ={})

    get_logger("test").info("only once")
    assert first.getvalue() == ""
    assert "only once" in second.getvalue()


def test_formatter_formats_exceptions() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exception"]
