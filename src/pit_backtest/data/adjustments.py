"""Split and dividend adjustment with (dt, perspective_dt) semantics.

Per ADR 0003 architecture sketch and the discussion in
docs/research/sources/methodology-point-in-time.md (Axis 1): adjusted prices
are computed from a (dt, perspective_dt) pair so that ratio signals see
unadjusted prices and return computation sees adjusted prices.

The total-return reconstruction (M1) lives here as a pure function. See
docs/methodology/total_return_reconstruction.md for the math and tolerance.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import polars as pl


# Trading days per year used to annualize the SPY expense-ratio drag. Per
# docs/methodology/total_return_reconstruction.md: SSGA's published 1-year,
# 3-year, 5-year, 10-year SPY NAV TR uses a 252-trading-day convention.
TRADING_DAYS_PER_YEAR = 252


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
    raise NotImplementedError("M1 day 3 deliverable (used by SEP adapter point lookup)")


def reconstruct_total_return(
    prices: pl.DataFrame,
    dividends: pl.DataFrame,
    start_dt: date | datetime,
    end_dt: date | datetime,
    expense_ratio_annual: Decimal,
) -> pl.DataFrame:
    """Reconstruct a daily total-return series from prices + dividends.

    Reinvestment convention: same-day-at-close on ex-date (algebraically
    equivalent to the standard TR(t) = TR(t-1) * (P(t) + D(t)) / P(t-1)
    update; see docs/methodology/total_return_reconstruction.md).

    Expense-ratio drag applied per trading day as a multiplicative factor
    (1 - expense_ratio_annual / 252) on every multiplier except the first
    row (the first row is the reference, TR[0] = 1.0, no time elapsed).

    Parameters
    ----------
    prices
        Polars frame with columns `dt` (pl.Date) and `close` (pl.Float64).
        Must be dense across NYSE trading days in [start_dt, end_dt].
    dividends
        Polars frame with columns `ex_date` (pl.Date) and `amount_per_share`
        (pl.Float64). May be empty if no dividends fall in the window.
    start_dt
        Reconciliation window start (inclusive). Becomes the row where
        TR = 1.0 by definition.
    end_dt
        Reconciliation window end (inclusive).
    expense_ratio_annual
        SPY expense ratio for the window. Constant across the window; the
        step-function case for windows crossing the 2003-11 SPY ratio
        change is a follow-up (out of scope for the M1 2005-2024 window
        which is entirely after the change).

    Returns
    -------
    pl.DataFrame
        Columns: `dt` (pl.Date), `tr` (pl.Float64, normalized so
        tr[start_dt] = 1.0), `daily_return` (pl.Float64, 0 on the
        reference row).

    Raises
    ------
    KeyError
        If `prices` is missing `dt` or `close`, or `dividends` is missing
        `ex_date` or `amount_per_share`.
    ValueError
        If no rows in `prices` fall in [start_dt, end_dt].

    Tolerance commitment
    --------------------
    Reconstruction matches SPDR-published SPY NAV TR to within 5 bps
    annualized over 2005-2024 with the expense-ratio drag explicitly
    subtracted (M1 kill-early gate). The toy three-day fixture in
    `tests/data/test_tr_reconstruction.py` exercises the algebra; the
    SPY reconciliation harness gates on the full window.
    """
    _validate_price_columns(prices)
    _validate_dividend_columns(dividends)

    start = start_dt.date() if isinstance(start_dt, datetime) else start_dt
    end = end_dt.date() if isinstance(end_dt, datetime) else end_dt

    prices_window = prices.filter(
        (pl.col("dt") >= start) & (pl.col("dt") <= end)
    ).sort("dt")

    if prices_window.is_empty():
        raise ValueError(
            f"No price rows in window [{start}, {end}]; "
            f"check the input frame and the window bounds."
        )

    daily_drag = float(expense_ratio_annual) / TRADING_DAYS_PER_YEAR

    dividends_aligned = dividends.rename({"ex_date": "dt"}).select(
        ["dt", pl.col("amount_per_share").cast(pl.Float64).alias("div")]
    )

    combined = (
        prices_window.join(dividends_aligned, on="dt", how="left")
        .with_columns(pl.col("div").fill_null(0.0))
        .sort("dt")
    )

    # First row's multiplier is 1.0 by definition (the reference); subsequent
    # rows use (close + div) / prior_close * (1 - daily_drag). The when/then
    # picks up the first row via the null on shift(1).
    combined = combined.with_columns(
        pl.when(pl.col("close").shift(1).is_null())
        .then(pl.lit(1.0))
        .otherwise(
            (pl.col("close") + pl.col("div"))
            / pl.col("close").shift(1)
            * (1.0 - daily_drag)
        )
        .alias("multiplier")
    )

    combined = combined.with_columns(
        pl.col("multiplier").cum_prod().alias("tr"),
        (pl.col("multiplier") - 1.0).alias("daily_return"),
    )

    return combined.select(["dt", "tr", "daily_return"]).sort("dt")


def annualized_return(tr_series: pl.DataFrame) -> float:
    """Annualize the cumulative total return over the trading days observed.

    Convention: ((TR_final / TR_start) ** (252 / (T - 1))) - 1, where T is
    the number of trading days in the series. Matches the convention SSGA
    uses for SPY's 1-year/3-year/5-year/10-year published NAV TR.
    """
    if "tr" not in tr_series.columns:
        raise KeyError("tr_series must have a 'tr' column produced by reconstruct_total_return")
    n = tr_series.height
    if n < 2:
        raise ValueError(
            f"need at least 2 rows to annualize; got {n}. The first row is the "
            f"reference (TR = 1.0) and contributes no time."
        )
    tr_first = float(tr_series["tr"][0])
    tr_last = float(tr_series["tr"][-1])
    annualized = (tr_last / tr_first) ** (TRADING_DAYS_PER_YEAR / (n - 1)) - 1.0
    return float(annualized)


def _validate_price_columns(prices: pl.DataFrame) -> None:
    missing = {"dt", "close"} - set(prices.columns)
    if missing:
        raise KeyError(f"prices frame missing columns: {sorted(missing)}")


def _validate_dividend_columns(dividends: pl.DataFrame) -> None:
    missing = {"ex_date", "amount_per_share"} - set(dividends.columns)
    if missing:
        raise KeyError(f"dividends frame missing columns: {sorted(missing)}")
