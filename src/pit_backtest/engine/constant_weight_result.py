"""ConstantWeightDemoResult: M1 demo result type.

Pydantic per the boundary contract (user-facing render target). The full
BacktestResult (analytics/scorecard.py) requires PSR/DSR/MinTRL which are
M4 work; for M1 we return a smaller result type that carries only what
the constant-weight demo produces.

M4 will define an adapter that wraps a ConstantWeightDemoResult into a
BacktestResult with the analytics fields computed from the equity_curve.
"""

from __future__ import annotations

from datetime import date

import polars as pl
from pydantic import BaseModel, ConfigDict

from pit_backtest.validation.confidence_tier import ConfidenceTier


class ConstantWeightDemoResult(BaseModel):
    """M1 demo result. Engine-internal until M4 wraps it as BacktestResult."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    final_pnl: float
    final_nav: float
    initial_capital: float
    equity_curve: pl.DataFrame
    n_trading_days: int
    n_rebalances: int
    tickers: tuple[str, ...]
    start_dt: date
    end_dt: date
    confidence_tier: ConfidenceTier
    sharadar_bundle: str

    def render_summary_line(self) -> str:
        """One-line summary for logs and PR descriptions."""
        return (
            f"constant_weight_demo: final_pnl=${self.final_pnl:+,.2f} "
            f"(initial=${self.initial_capital:,.2f}, "
            f"final_nav=${self.final_nav:,.2f}, "
            f"tickers={','.join(self.tickers)}, "
            f"window={self.start_dt}..{self.end_dt}, "
            f"n_trading_days={self.n_trading_days}, "
            f"n_rebalances={self.n_rebalances}, "
            f"snapshot={self.sharadar_bundle})"
        )
