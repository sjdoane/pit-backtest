"""Structured logging configuration.

Per ADR 0002 decision 5 (M1 scope): stdlib logging configured at boot
with --log-level surfaced on the CLI. Records emit key=value structured
fields on every line so logs are grep-friendly.

Usage:
    from pit_backtest.utils.logging import configure_logging, get_logger
    configure_logging("INFO")
    log = get_logger(__name__)
    log.info("loaded snapshot", extra={"bundle": "sharadar_2026-05-28", "files": 5})

Output (one line):
    2026-05-28T10:42:17 INFO pit_backtest.cli loaded snapshot bundle=sharadar_2026-05-28 files=5
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Format: ISO-8601 timestamp, level, logger name, message, then key=value
# pairs from the record's extra dict. Comma-less for one-pass grep.
_BASE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Standard LogRecord attribute names that should NOT be rendered as
# key=value fields (they are part of the formatter prefix or stdlib bookkeeping).
_RESERVED_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class _StructuredFormatter(logging.Formatter):
    """Renders the standard prefix plus any non-reserved record attributes
    as space-separated key=value pairs appended to the message.

    Values are repr()'d if they contain whitespace or `=`, so the parse is
    unambiguous on the consumer side.
    """

    def format(self, record: logging.LogRecord) -> str:
        prefix = super().format(record)
        extras = []
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS:
                continue
            extras.append(f"{key}={_format_value(value)}")
        if not extras:
            return prefix
        return f"{prefix} {' '.join(extras)}"


def _format_value(value: object) -> str:
    s = str(value)
    if any(c in s for c in (" ", "=", '"', "'")):
        return repr(s)
    return s


def configure_logging(level: LogLevel) -> None:
    """Configure stdlib logging for the engine.

    Idempotent: re-calling replaces existing handlers on the root logger
    so the most recent level wins (useful when CLI parsing and library
    code both attempt to configure).
    """
    root = logging.getLogger()
    # Clear existing handlers to make this idempotent across re-calls.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = _StructuredFormatter(fmt=_BASE_FORMAT, datefmt=_DATEFMT)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(level)

    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for engine module use."""
    return logging.getLogger(name)
