"""Structured JSON logging with secret redaction.

Logs go to stdout as one JSON object per line. Journald captures stdout, so no
file handling is required in the service.

Secret handling: values of environment variables whose names start with ``OP_``
or ``GH_TOKEN``, or that end with a credential suffix such as ``_API_KEY``,
``_TOKEN``, ``_PASSWORD`` or ``_SECRET``, are registered as secrets when logging
is configured, and their literal values are replaced with ``***`` wherever they
appear in a log event or extra field. Extra fields whose *name* looks secret are
redacted too.

The suffix rule covers the provider keys pi reads (for example
``OPENROUTER_API_KEY`` or ``ANTHROPIC_API_KEY``), which do not share the
engine's ``OP_`` prefix.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

REDACTED = "***"

_SECRET_PREFIXES = ("OP_", "GH_TOKEN")
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")

_STANDARD_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_secret_values: set[str] = set()


def is_secret_name(name: str) -> bool:
    """Return True when an environment/extra field name denotes a secret."""
    upper = name.upper()
    if any(upper.startswith(prefix) for prefix in _SECRET_PREFIXES):
        return True
    return any(upper.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def register_secrets(environ: Mapping[str, str] | None = None) -> None:
    """Remember secret values so they can be scrubbed from log output."""
    source = os.environ if environ is None else environ
    for name, value in source.items():
        if is_secret_name(name) and value:
            _secret_values.add(value)


def redact_text(value: str) -> str:
    for secret in sorted(_secret_values, key=len, reverse=True):
        if secret in value:
            value = value.replace(secret, REDACTED)
    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in values.items():
        if is_secret_name(str(key)):
            redacted[key] = REDACTED
        elif isinstance(value, str):
            redacted[key] = redact_text(value)
        else:
            redacted[key] = value
    return redacted


class JsonFormatter(logging.Formatter):
    """Render log records as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = REDACTED if is_secret_name(key) else value

        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))

        return redact_text(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install the JSON handler on the root logger.

    Safe to call more than once (for example in tests); the previous handler
    installed by this module is replaced.
    """
    register_secrets(environ)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_op_agent_engine", False):
            root.removeHandler(existing)
            existing.close()
    handler._op_agent_engine = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
