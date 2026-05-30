"""Drawdown statistics: max drawdown, longest-duration report, Calmar ratio.

Per ADR 0014 the `drawdown_duration_report` function returns a
`DrawdownDurationReport` attrs.frozen record carrying the four
LdP-honest fields (`days`, `is_censored_at_end`, `peak_dt`, `trough_dt`)
rather than a bare int. LdP 2018 chapter 13 treats longest-drawdown
duration as a censored survival-analysis quantity; a `-> int` return
would lose the censoring flag when the equity curve ends underwater.

Conventions:
- `max_drawdown` returns the LdP positive magnitude (0.45 means a 45%
  drawdown), NOT the signed negative form. Pinned by the test fixture
  to match the scorecard convention at `analytics/scorecard.py:37`.
- `calmar_ratio` uses the CAGR-style geometric annualization
  `(nav_last / nav_first) ** (periods_per_year / n_periods) - 1` matching
  the LdP 2018 chapter 14 + Stoeckl 1991 Calmar definition. A
  minimum-periods floor of 21 trading days (one trading month per
  LdP 2018 chapter 14; bumped from the original Plan-reviewer High 3
  one-week floor per M4 PR 2 post-impl reviewer Medium 4) guards
  against the small-n_periods blowup where a 2-bar curve produces a
  252-power annualization with no statistical content.
- Domain violations raise `ValueError` per the codebase loud-failure
  discipline locked in ADR 0013 decision 7.

Input contract: `equity_curve` is a Polars frame with at minimum the
columns `dt: pl.Date` and `nav: pl.Float64`. Additional columns are
ignored. Rows are sorted by `dt` ascending defensively (the BarLoop
sorts at construction; the determinism invariant in
`docs/methodology/determinism.md` mandates sorted output at every step).
"""

from __future__ import annotations

from datetime import date
from typing import Final

import attrs
import polars as pl


# Minimum number of bars for a Calmar computation to produce a
# statistically meaningful annualized return. With n_periods < 21 the
# `(nav_last / nav_first) ** (periods_per_year / n_periods)` exponent
# is large enough that small bar-level price moves get annualized into
# physically meaningless numbers (a 2-bar 10% return becomes
# 1.10 ** 252 = 2.7e10). One trading month is the LdP 2018 chapter 14
# convention for the shortest CAGR-defensible interval. The original
# Plan-reviewer High 3 set this to 5 (one week); the M4 PR 2 post-impl
# reviewer Medium 4 bumped it to 21 (one month) because the 50.4x
# exponent at 5 bars still produces numbers a downstream consumer
# would not trust.
_CALMAR_MIN_PERIODS: Final[int] = 21


@attrs.frozen(slots=True)
class DrawdownDurationReport:
    """Longest-drawdown summary with LdP 2018 chapter 13 honesty fields.

    Per ADR 0014. Fields:
      days: integer count of bars in the longest underwater run (an
        underwater bar has `nav < running_peak`). For a flat curve with
        no drawdown, `days == 0`.
      is_censored_at_end: True when the longest underwater run's last
        bar is the last bar of the equity curve (recovery is censored
        by the backtest window cutoff). False otherwise.
      peak_dt: the date of the last bar BEFORE the longest underwater
        run (the high-water mark from which the drawdown started). For
        a flat curve, this equals the first bar's date.
      trough_dt: the date at which `nav` reached its minimum within the
        longest underwater run. None when the curve never went below
        its first-bar peak (flat curve case).
    """

    days: int
    is_censored_at_end: bool
    peak_dt: date
    trough_dt: date | None


def _validate_equity_curve(
    equity_curve: pl.DataFrame, min_height: int, fn_name: str
) -> None:
    """Common equity_curve validation: required columns + minimum height.

    Single source of truth so the three module-public functions raise
    with consistent messages. Per ADR 0013 decision 7 the codebase
    discipline is loud-fail-with-the-offending-value.
    """
    missing = {"dt", "nav"} - set(equity_curve.columns)
    if missing:
        raise ValueError(
            f"{fn_name} requires equity_curve columns dt + nav; missing: "
            f"{sorted(missing)}"
        )
    if equity_curve.height < min_height:
        raise ValueError(
            f"{fn_name} requires equity_curve.height >= {min_height}; "
            f"got {equity_curve.height}"
        )


def max_drawdown(equity_curve: pl.DataFrame) -> float:
    """Maximum peak-to-trough drawdown on the equity curve.

    Returns the positive magnitude (LdP convention; 0.45 means a 45%
    drawdown). The internal `drawdown` Polars column is signed; the
    return is negated so the public API is unsigned-magnitude consistent
    with the scorecard `RunsAndDrawdowns.max_drawdown: float` field.

    Raises:
      ValueError: when equity_curve is missing dt or nav columns;
        when height < 2 (no excursion possible);
        when the first-bar nav is non-positive (the CAGR / drawdown
        arithmetic divides by the running peak, which would be zero or
        negative).
    """
    _validate_equity_curve(equity_curve, min_height=2, fn_name="max_drawdown")

    sorted_ec = equity_curve.sort("dt")

    nav_first = float(sorted_ec["nav"][0])
    if nav_first <= 0.0:
        raise ValueError(
            f"max_drawdown requires positive starting equity; "
            f"got nav_first={nav_first}"
        )

    with_dd = sorted_ec.with_columns(
        pl.col("nav").cum_max().alias("running_peak"),
    ).with_columns(
        (
            (pl.col("nav") - pl.col("running_peak")) / pl.col("running_peak")
        ).alias("drawdown")
    )
    min_dd = with_dd["drawdown"].min()
    if min_dd is None:
        raise ValueError(
            "max_drawdown received an equity curve whose drawdown column "
            "evaluated to all null; check the input nav column"
        )
    if not isinstance(min_dd, (int, float)):
        raise ValueError(
            f"max_drawdown internal: drawdown column min() returned "
            f"non-numeric type {type(min_dd).__name__}"
        )
    return -float(min_dd)


def drawdown_duration_report(
    equity_curve: pl.DataFrame,
) -> DrawdownDurationReport:
    """Longest in-window drawdown reported via DrawdownDurationReport.

    Per ADR 0014. The four LdP-honest fields are computed in one pass:
    `(~underwater).cum_sum()` produces a run identifier that increments
    each time the curve crosses back to the running peak; consecutive
    underwater bars share one run_id. The longest run by bar count
    (tie-broken by earliest start date for determinism) is the report's
    subject. Within the chosen longest run, `trough_dt` is the date of
    the minimum-`nav` bar (ties broken by earliest `dt` for
    determinism per `docs/methodology/determinism.md`).

    Raises:
      ValueError: when equity_curve is missing dt or nav columns or
        when height < 1.
    """
    _validate_equity_curve(
        equity_curve, min_height=1, fn_name="drawdown_duration_report"
    )

    sorted_ec = equity_curve.sort("dt")
    first_bar_dt = sorted_ec["dt"][0]

    with_dd = sorted_ec.with_columns(
        pl.col("nav").cum_max().alias("running_peak"),
    ).with_columns(
        (pl.col("nav") < pl.col("running_peak")).alias("underwater"),
    ).with_columns(
        (~pl.col("underwater")).cum_sum().alias("run_id"),
    )

    underwater_rows = with_dd.filter(pl.col("underwater"))

    if underwater_rows.height == 0:
        # Flat or always-rising curve: no underwater bars.
        return DrawdownDurationReport(
            days=0,
            is_censored_at_end=False,
            peak_dt=first_bar_dt,
            trough_dt=None,
        )

    runs = underwater_rows.group_by("run_id").agg(
        pl.col("dt").min().alias("start_dt"),
        pl.col("dt").max().alias("end_dt"),
        pl.len().alias("bar_count"),
    ).sort(
        ["bar_count", "start_dt"], descending=[True, False]
    )
    longest = runs.row(0, named=True)
    longest_run_id = longest["run_id"]
    longest_bars = underwater_rows.filter(
        pl.col("run_id") == longest_run_id
    ).sort(["nav", "dt"])
    trough_dt = longest_bars["dt"][0]

    last_bar_dt = sorted_ec["dt"][-1]
    is_censored = longest["end_dt"] == last_bar_dt

    start_dt = longest["start_dt"]
    sorted_with_idx = sorted_ec.with_row_index()
    start_idx = sorted_with_idx.filter(
        pl.col("dt") == start_dt
    )["index"][0]
    # Per cum_max invariant: the first bar's running_peak equals its own
    # nav, so the first bar can never satisfy nav < running_peak. An
    # underwater run therefore cannot start at index 0. Per M4 PR 2
    # post-impl reviewer High 1 we raise loudly if the invariant is ever
    # violated rather than silently returning a wrong peak_dt.
    if start_idx == 0:
        raise RuntimeError(
            "drawdown_duration_report internal invariant violated: "
            "longest underwater run started at bar 0 but cum_max guarantees "
            "the first bar's running_peak equals its own nav. Inspect the "
            "upstream sort or the equity_curve construction"
        )
    peak_dt = sorted_ec["dt"][int(start_idx) - 1]

    return DrawdownDurationReport(
        days=int(longest["bar_count"]),
        is_censored_at_end=bool(is_censored),
        peak_dt=peak_dt,
        trough_dt=trough_dt,
    )


def calmar_ratio(
    equity_curve: pl.DataFrame, periods_per_year: int = 252
) -> float:
    """CAGR divided by absolute max drawdown magnitude.

    `CAGR = (nav_last / nav_first) ** (periods_per_year / n_periods) - 1`
    where `n_periods = equity_curve.height - 1` (the number of return
    periods, not the number of bars). Returned as a signed float: a
    negative-CAGR + positive max_drawdown combination produces a
    negative Calmar.

    Per the M4 PR 2 post-impl reviewer Medium 4 we raise on
    `equity_curve.height < _CALMAR_MIN_PERIODS` (21 bars; one trading
    month). The geometric annualization explodes at small n_periods
    (a 2-bar 10% return annualizes to 2.7e10 with no statistical
    content); the 21-bar monthly floor matches LdP 2018 chapter 14's
    convention for the shortest CAGR-defensible interval and is the
    loud-fail boundary per ADR 0013 decision 7.

    Raises:
      ValueError: when equity_curve is missing dt or nav columns;
        when height < _CALMAR_MIN_PERIODS;
        when periods_per_year <= 0;
        when nav_first <= 0;
        when max_drawdown returns 0.0 (flat curve has no risk denominator).
    """
    _validate_equity_curve(
        equity_curve, min_height=_CALMAR_MIN_PERIODS, fn_name="calmar_ratio"
    )
    if periods_per_year <= 0:
        raise ValueError(
            f"calmar_ratio requires periods_per_year > 0; "
            f"got periods_per_year={periods_per_year}"
        )

    sorted_ec = equity_curve.sort("dt")
    nav_first = float(sorted_ec["nav"][0])
    nav_last = float(sorted_ec["nav"][-1])
    if nav_first <= 0.0:
        raise ValueError(
            f"calmar_ratio requires positive starting equity; "
            f"got nav_first={nav_first}"
        )

    n_periods = sorted_ec.height - 1
    cagr = (nav_last / nav_first) ** (periods_per_year / n_periods) - 1.0
    max_dd = max_drawdown(sorted_ec)
    if max_dd == 0.0:
        raise ValueError(
            "calmar_ratio undefined when max_drawdown == 0; flat equity "
            "curve has no risk denominator"
        )
    return float(cagr / max_dd)
