"""Structured logging tests.

Verifies the configure_logging + get_logger contract: levels respected,
structured key=value fields appended, idempotent re-configure.
"""

from __future__ import annotations

import io
import logging

import pytest

from pit_backtest.utils.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def reset_logging():
    """Reset the root logger between tests so configure_logging is
    deterministic.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _capture_logs(level: str = "INFO") -> tuple[logging.Logger, io.StringIO]:
    """Configure logging at the given level and redirect stderr handler to
    a StringIO buffer. Returns (logger, buffer).
    """
    configure_logging(level)  # type: ignore[arg-type]
    root = logging.getLogger()
    buf = io.StringIO()
    # Replace the streamhandler's stream with our buffer.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            h.stream = buf
    return get_logger("test_module"), buf


def test_info_log_emits_at_info_level() -> None:
    log, buf = _capture_logs("INFO")
    log.info("hello world")
    output = buf.getvalue()
    assert "INFO" in output
    assert "test_module" in output
    assert "hello world" in output


def test_debug_filtered_at_info_level() -> None:
    """DEBUG records are suppressed when level is INFO."""
    log, buf = _capture_logs("INFO")
    log.debug("not visible")
    log.info("visible")
    output = buf.getvalue()
    assert "not visible" not in output
    assert "visible" in output


def test_debug_visible_at_debug_level() -> None:
    log, buf = _capture_logs("DEBUG")
    log.debug("debug detail")
    assert "DEBUG" in buf.getvalue()
    assert "debug detail" in buf.getvalue()


def test_structured_fields_rendered_as_key_value() -> None:
    """extra dict becomes ' key=value ...' after the message."""
    log, buf = _capture_logs("INFO")
    log.info("loaded snapshot", extra={"bundle": "sharadar_2026-05-28", "files": 5})
    output = buf.getvalue()
    assert "bundle=sharadar_2026-05-28" in output
    assert "files=5" in output


def test_values_with_spaces_quoted() -> None:
    """Values containing spaces are repr()'d so grep parses them unambiguously."""
    log, buf = _capture_logs("INFO")
    log.info("event", extra={"note": "value with spaces"})
    output = buf.getvalue()
    assert "note='value with spaces'" in output


def test_configure_logging_idempotent() -> None:
    """Re-calling configure_logging replaces existing handlers; the most
    recent level wins.
    """
    log, _ = _capture_logs("DEBUG")
    configure_logging("WARNING")
    log = get_logger("test_module")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    # Single handler attached (the latest one).
    handler_count = sum(1 for h in root.handlers if isinstance(h, logging.StreamHandler))
    assert handler_count == 1


def test_warning_level_filters_info() -> None:
    log, buf = _capture_logs("WARNING")
    log.info("hidden")
    log.warning("shown")
    output = buf.getvalue()
    assert "hidden" not in output
    assert "shown" in output
