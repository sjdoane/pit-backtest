"""Drawdown statistics: max drawdown, average drawdown, duration, Calmar."""

from __future__ import annotations

import polars as pl


def max_drawdown(equity_curve: pl.DataFrame) -> float:
    """Maximum peak-to-trough drawdown on the equity curve."""
    raise NotImplementedError("M4 deliverable")


def drawdown_duration_days(equity_curve: pl.DataFrame) -> int:
    """Longest drawdown duration in trading days."""
    raise NotImplementedError("M4 deliverable")


def calmar_ratio(equity_curve: pl.DataFrame, periods_per_year: int = 252) -> float:
    """Annualized return divided by absolute value of max drawdown."""
    raise NotImplementedError("M4 deliverable")
