"""Analytics layer: LdP chapter 14 scorecard.

PSR, DSR, MinTRL, HHI, drawdown, and the Markdown scorecard renderer. Per
ADR 0001 decision 4, raw Sharpe shown alone is a configuration error; the
render path enforces this via the ConfidenceTier check on BacktestResult.
"""

from pit_backtest.analytics.concentration import hhi
from pit_backtest.analytics.distribution import BacktestPathDistribution
from pit_backtest.analytics.drawdown import (
    DrawdownDurationReport,
    calmar_ratio,
    drawdown_duration_report,
    max_drawdown,
)
from pit_backtest.analytics.sharpe import dsr, min_trl, psr

__all__ = [
    "BacktestPathDistribution",
    "DrawdownDurationReport",
    "calmar_ratio",
    "drawdown_duration_report",
    "dsr",
    "hhi",
    "max_drawdown",
    "min_trl",
    "psr",
]
