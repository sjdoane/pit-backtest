"""Drawdown statistics: max drawdown, longest-duration report, Calmar ratio.

Per ADR 0014 the `drawdown_duration_report` function returns a
`DrawdownDurationReport` attrs.frozen record carrying the four
LdP-honest fields (`days`, `is_censored_at_end`, `peak_dt`, `trough_dt`)
rather than a bare int. LdP 2018 chapter 13 treats longest-drawdown
duration as a censored survival-analysis quantity; a `-> int` return
would lose the censoring flag when the equity curve ends underwater.

Function bodies are M4 PR 2 deliverables; this module ships the
`DrawdownDurationReport` record + the function signatures only as part
of the ADR 0014 prep PR.
"""

from __future__ import annotations

from datetime import date

import attrs
import polars as pl


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


def max_drawdown(equity_curve: pl.DataFrame) -> float:
    """Maximum peak-to-trough drawdown on the equity curve.

    Returns the positive magnitude (LdP convention; `0.45` means a 45%
    drawdown). M4 PR 2 deliverable.
    """
    raise NotImplementedError("M4 deliverable")


def drawdown_duration_report(
    equity_curve: pl.DataFrame,
) -> DrawdownDurationReport:
    """Longest in-window drawdown reported via DrawdownDurationReport.

    Per ADR 0014 (renamed from `drawdown_duration_days` and widened from
    `-> int` to honor LdP 2018 chapter 13 censored-survival semantics).
    M4 PR 2 deliverable.
    """
    raise NotImplementedError("M4 deliverable")


def calmar_ratio(
    equity_curve: pl.DataFrame, periods_per_year: int = 252
) -> float:
    """CAGR divided by absolute max drawdown magnitude.

    M4 PR 2 deliverable.
    """
    raise NotImplementedError("M4 deliverable")
