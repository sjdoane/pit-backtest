"""HHI concentration on bar-level PnL.

Per ADR 0001 decision 4: the LdP chapter 14 scorecard requires HHI as the
concentration metric.
"""

from __future__ import annotations

import polars as pl


def hhi(pnl_series: pl.Series) -> float:
    """Herfindahl-Hirschman Index on absolute-value PnL contributions.

    Range [1/N, 1]; near 1/N = uniform contributions across bars, near 1 =
    concentrated in a few bars.
    """
    raise NotImplementedError("M4 deliverable")
