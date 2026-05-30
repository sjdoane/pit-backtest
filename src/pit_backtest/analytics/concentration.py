"""HHI concentration on bar-level PnL.

Per ADR 0001 decision 4 the LdP chapter 14 scorecard requires HHI as the
concentration metric. Per the M4 PR 2 Plan-reviewer Medium 4 the
all-zero PnL case raises rather than silently returns 0.0, consistent
with the loud-failure discipline locked in ADR 0013 decision 7: a
scorecard rendering HHI=0.0 cannot be distinguished by the reader from
"uniform contributions across 1000 bars" vs "strategy never traded".
"""

from __future__ import annotations

import polars as pl


def hhi(pnl_series: pl.Series) -> float:
    """Herfindahl-Hirschman Index on absolute-value PnL contributions.

    `HHI = sum(w_i^2)` where `w_i = |pnl_i| / sum(|pnl_j|)`.
    Range `[1/N, 1]` when the absolute-value sum is non-zero. Near
    `1/N` = uniform contributions across bars; near 1 = concentrated in
    a few bars.

    Absolute-value normalization (per Plan-reviewer Choice D ratification)
    handles mixed-sign PnL correctly: a strategy with offsetting gains
    and losses is concentrated in its trading bars, not de-concentrated
    by net-zero cancellation. A series of `[+10, -10, +5, -5]` has
    concentration = (100 + 100 + 25 + 25) / 900 = 0.2777.

    Raises:
      ValueError: when pnl_series is empty;
        when the absolute-value sum is zero (mathematically undefined;
        per the codebase loud-failure discipline the operator fixes the
        backtest setup, not the HHI interpretation).
    """
    if pnl_series.is_empty():
        raise ValueError("hhi requires a non-empty pnl_series; got length 0")

    abs_pnl = pnl_series.abs()
    total = abs_pnl.sum()
    if total is None:
        raise ValueError(
            "hhi pnl_series contained only nulls; sum is undefined"
        )
    total_float = float(total)
    if total_float == 0.0:
        raise ValueError(
            "hhi undefined when sum(|pnl|) == 0; the strategy produced no "
            "PnL contributions. Fix the backtest setup rather than "
            "interpreting a degenerate concentration value."
        )

    weights = abs_pnl / total_float
    weights_squared_sum = (weights * weights).sum()
    if weights_squared_sum is None:
        raise ValueError(
            "hhi weights-squared-sum is undefined; inspect the pnl_series"
        )
    return float(weights_squared_sum)
