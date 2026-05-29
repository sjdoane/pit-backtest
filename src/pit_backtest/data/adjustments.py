"""Split and dividend adjustment with (dt, perspective_dt) semantics.

Per ADR 0003 architecture sketch and the discussion in
docs/research/sources/methodology-point-in-time.md (Axis 1): adjusted prices
are computed from a (dt, perspective_dt) pair so that ratio signals see
unadjusted prices and return computation sees adjusted prices.

The total-return reconstruction (M1) lives here as a pure function. See
docs/methodology/total_return_reconstruction.md for the math and tolerance.

Per ADR 0006, reconstruct_total_return supports a step-function expense
ratio via ExpenseRatioSchedule. The schedule is needed for the SI window
(SPY inception 1993-01-22) which straddles the 2003-11-01 expense-ratio
reduction from 0.12% to 0.0945%.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime
from decimal import Decimal
from typing import Union

import attrs
import polars as pl


# Trading days per year used to annualize the SPY expense-ratio drag. Per
# docs/methodology/total_return_reconstruction.md: SSGA's published 1-year,
# 3-year, 5-year, 10-year SPY NAV TR uses a 252-trading-day convention.
TRADING_DAYS_PER_YEAR = 252


@attrs.frozen(slots=True)
class ExpenseRatioStep:
    """One step of a stepwise time-varying expense-ratio schedule.

    The rate is annualized; the per-trading-day drag is rate / 252.
    """

    effective_from: date
    rate: Decimal


@attrs.frozen(slots=True)
class ExpenseRatioSchedule:
    """A stepwise expense-ratio schedule sorted ascending by effective_from.

    The schedule supports scalar lookups via rate_for(d) and frame-wide
    joins inside reconstruct_total_return via to_drag_frame(). The two
    paths agree at every boundary day per ADR 0006: a query date equal
    to a step's effective_from picks up the new rate.

    For SPY's history, ADR 0006 locks::

        ExpenseRatioSchedule(rows=(
            ExpenseRatioStep(date(1993, 1, 22), Decimal("0.0012")),
            ExpenseRatioStep(date(2003, 11, 1), Decimal("0.000945")),
        ))
    """

    rows: tuple[ExpenseRatioStep, ...]

    def __attrs_post_init__(self) -> None:
        if not self.rows:
            raise ValueError("ExpenseRatioSchedule requires at least one step")
        effective_dates = [step.effective_from for step in self.rows]
        if effective_dates != sorted(effective_dates):
            raise ValueError(
                "ExpenseRatioSchedule rows must be sorted ascending by effective_from; "
                f"got {effective_dates}"
            )

    def rate_for(self, d: date) -> Decimal:
        """Return the rate effective on date d (inclusive of effective_from).

        Uses bisect_right; the step at index (bisect_right - 1) is the
        most recent step satisfying effective_from <= d. Raises
        ValueError if d precedes the schedule's first effective_from.
        """
        effective_dates = [step.effective_from for step in self.rows]
        idx = bisect.bisect_right(effective_dates, d) - 1
        if idx < 0:
            raise ValueError(
                f"date {d} precedes the schedule's first effective_from "
                f"({self.rows[0].effective_from}); no rate is defined"
            )
        return self.rows[idx].rate

    def to_drag_frame(self) -> pl.DataFrame:
        """Return a Polars frame keyed by effective_from with the per-day drag.

        Columns: effective_from (pl.Date), daily_drag (pl.Float64).
        Used by reconstruct_total_return's schedule branch as the right
        side of a join_asof(strategy='backward') against the prices frame.
        The Decimal-to-float conversion happens here once.
        """
        return pl.DataFrame(
            {
                "effective_from": [step.effective_from for step in self.rows],
                "daily_drag": [
                    float(step.rate) / TRADING_DAYS_PER_YEAR for step in self.rows
                ],
            },
            schema={"effective_from": pl.Date, "daily_drag": pl.Float64},
        )


ExpenseRatioParam = Union[Decimal, ExpenseRatioSchedule]


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
    expense_ratio_annual: ExpenseRatioParam,
) -> pl.DataFrame:
    """Reconstruct a daily total-return series from prices + dividends.

    Reinvestment convention: same-day-at-close on ex-date (algebraically
    equivalent to the standard TR(t) = TR(t-1) * (P(t) + D(t)) / P(t-1)
    update; see docs/methodology/total_return_reconstruction.md).

    Expense-ratio drag applied per trading day as a multiplicative factor
    (1 - daily_drag) on every multiplier except the first row (the first
    row is the reference, TR[0] = 1.0, no time elapsed). The drag rate
    is constant when expense_ratio_annual is a Decimal; it is a step
    function when expense_ratio_annual is an ExpenseRatioSchedule (per
    ADR 0006, the SI window for SPY straddles the 2003-11-01 reduction).

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
        Either a `Decimal` for a constant rate across the window, or an
        `ExpenseRatioSchedule` for a stepwise time-varying rate (e.g.
        SPY's pre-2003 0.12% / post-2003 0.0945% split). The schedule
        path uses `join_asof(strategy='backward')` so the boundary day
        `dt == effective_from` picks up the new rate. The scalar path
        is byte-for-byte equivalent to the pre-ADR-0006 implementation.

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
        If no rows in `prices` fall in [start_dt, end_dt], or if a schedule
        is supplied but does not cover the window's first trading day.

    Tolerance commitment
    --------------------
    Reconstruction matches SSGA-published SPY NAV TR to within 5 bps
    annualized per trailing period (1y / 3y / 5y / 10y / SI ending at
    SSGA's as_of_date) with the expense-ratio drag explicitly subtracted
    (M1 kill-early gate per ADR 0006). The toy three-day fixture in
    `tests/data/test_tr_reconstruction.py` exercises the algebra; the
    SPY reconciliation harness gates on each trailing period.
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

    dividends_aligned = dividends.rename({"ex_date": "dt"}).select(
        ["dt", pl.col("amount_per_share").cast(pl.Float64).alias("div")]
    )

    combined = (
        prices_window.join(dividends_aligned, on="dt", how="left")
        .with_columns(pl.col("div").fill_null(0.0))
        .sort("dt")
    )

    # Resolve daily_drag as either a scalar literal (Decimal path) or a
    # per-row column (ExpenseRatioSchedule path). The two paths agree at
    # every boundary day per ADR 0006; the scalar path is byte-for-byte
    # equivalent to the pre-ADR-0006 implementation.
    if isinstance(expense_ratio_annual, ExpenseRatioSchedule):
        # Validate coverage: the window's first trading day must be at or
        # after the schedule's first effective_from. Otherwise the join
        # would silently produce null daily_drag and the multiplier would
        # become NaN.
        first_dt = combined["dt"][0]
        if first_dt < expense_ratio_annual.rows[0].effective_from:
            raise ValueError(
                f"ExpenseRatioSchedule's first effective_from "
                f"({expense_ratio_annual.rows[0].effective_from}) is after "
                f"the window's first trading day ({first_dt}); add an earlier "
                f"step or narrow the window."
            )
        drag_frame = expense_ratio_annual.to_drag_frame()
        combined = combined.sort("dt").join_asof(
            drag_frame.sort("effective_from"),
            left_on="dt",
            right_on="effective_from",
            strategy="backward",
        )
        daily_drag_expr: pl.Expr = pl.col("daily_drag")
    else:
        scalar_drag = float(expense_ratio_annual) / TRADING_DAYS_PER_YEAR
        daily_drag_expr = pl.lit(scalar_drag)

    # First row's multiplier is 1.0 by definition (the reference); subsequent
    # rows use (close + div) / prior_close * (1 - daily_drag). The when/then
    # picks up the first row via the null on shift(1).
    combined = combined.with_columns(
        pl.when(pl.col("close").shift(1).is_null())
        .then(pl.lit(1.0))
        .otherwise(
            (pl.col("close") + pl.col("div"))
            / pl.col("close").shift(1)
            * (1.0 - daily_drag_expr)
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
