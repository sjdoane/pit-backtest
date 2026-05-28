"""Structured logging configuration.

Per ADR 0002 decision 5 (M1 scope): stdlib logging configured at boot with
--log-level surfaced on the CLI. Adds key=value structured fields on every
record so logs are grep-friendly.
"""

from __future__ import annotations

import logging
from typing import Literal


def configure_logging(level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]) -> None:
    """Configure stdlib logging for the engine."""
    raise NotImplementedError("M1 deliverable")


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for engine module use."""
    return logging.getLogger(name)
