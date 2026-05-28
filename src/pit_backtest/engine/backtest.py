"""Backtest: top-level orchestration with validate() preflight.

Per ADR 0003 decision 10: validate() verifies every asset in
Universe.members_at(start) has prices; no membership gap exceeds tolerance;
all required signal lookback days are available. Failures raise with the
offending assets surfaced.

Per ADR 0003 decision 16 and decision 19: prints data-freshness check
(snapshot SHA256, age in days) and validates signal warm-up at construction.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.engine.bar_loop import BarLoop


class Backtest:
    """Top-level backtest orchestration."""

    def __init__(self, bar_loop: BarLoop, snapshots_root: Path) -> None:
        raise NotImplementedError("M1 deliverable")

    def validate(self) -> None:
        """Preflight: data presence, membership gaps, signal warm-up."""
        raise NotImplementedError("M1 deliverable")

    def run(self, start_dt: datetime, end_dt: datetime) -> BacktestResult:
        raise NotImplementedError("M1 deliverable")


class InsufficientHistoryError(ValueError):
    """Raised when the signal's required_lookback_days exceeds available history."""


class UniverseGapError(ValueError):
    """Raised when an asset in the universe is missing prices for part of
    its membership spell.
    """
