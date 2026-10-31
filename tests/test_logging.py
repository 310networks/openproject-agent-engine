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


def test_provider_api_key_env_names_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging(
        "INFO",
        stream=stream,
        environ={
            "OPENROUTER_API_KEY": "sk-or-plaintext",
            "ANTHROPIC_API_KEY": "sk-ant-plaintext",
        },
    )
    get_logger("test").info("using sk-or-plaintext and sk-ant-plaintext")
    record = stream.getvalue().strip()

    assert "sk-or-plaintext" not in record
    assert "sk-ant-plaintext" not in record
    assert REDACTED in record


def test_extra_field_with_provider_key_name_is_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream, environ={})
    get_logger("test").info("hello", extra={"OPENCODE_API_KEY": "plain", "OP_TOKEN_X": "plain"})
    payload = json.loads(stream.getvalue().strip())

    assert payload["OPENCODE_API_KEY"] == REDACTED
    assert payload["OP_TOKEN_X"] == REDACTED


def test_non_secret_names_stay_visible() -> None:
    from op_agent_engine.logging import is_secret_name

    assert not is_secret_name("MAX_REJECTIONS")
    assert not is_secret_name("WORKSPACE_ROOT")
    assert not is_secret_name("OPENCODE")
    assert is_secret_name("OPENROUTER_API_KEY")
