"""Split and dividend adjustment with (dt, perspective_dt) semantics.

Per ADR 0003 architecture sketch and the discussion in
docs/research/sources/methodology-point-in-time.md (Axis 1): adjusted prices
are computed from a (dt, perspective_dt) pair so that ratio signals see
unadjusted prices and return computation sees adjusted prices.

The total-return reconstruction (M1) lives here as a pure function. See
docs/methodology/total_return_reconstruction.md for the math and tolerance.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import polars as pl


def adjusted_close(
    prices: pl.DataFrame,
    dt: datetime,
    perspective_dt: datetime | None = None,
) -> Decimal:
    """Return the close price at dt, adjusted from the vantage of perspective_dt.

    perspective_dt=None means perspective_dt = dt (returns the unadjusted
    close as observed on dt itself). perspective_dt = some future date
    returns the close as back-adjusted from that future date.
    """
    raise NotImplementedError("M3 deliverable")


def unadjusted_close(prices: pl.DataFrame, dt: datetime) -> Decimal:
    """Return the raw close price at dt with no adjustments applied."""
    raise NotImplementedError("M1 deliverable (used by total-return reconstruction)")


def reconstruct_total_return(
    prices: pl.DataFrame,
    dividends: pl.DataFrame,
    start_dt: datetime,
    end_dt: datetime,
    expense_ratio_annual: Decimal,
) -> pl.DataFrame:
    """Reconstruct a daily total-return series from prices + dividends.

    Reinvestment convention: same-day-at-close on ex-date (algebraically
    equivalent to the standard TR(t) = TR(t-1) * (P(t) + D(t)) / P(t-1)
    update; see docs/methodology/total_return_reconstruction.md).

    Output columns: dt, tr (normalized so tr[start_dt] = 1.0), daily_return.

    Tolerance commitment: matches SPDR-published SPY NAV TR to within 5
    bps annualized over 2005-2024 with the expense-ratio drag explicitly
    subtracted (M1 kill-early gate).
    """
    raise NotImplementedError("M1 deliverable")
