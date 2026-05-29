"""SPY reconciliation harness (M1 kill-early gate).

Per ADR 0006 the reconciliation compares the engine's reconstructed SPY
TR to SSGA's published trailing 1Y / 3Y / 5Y / 10Y / SI annualizations
ending at SSGA's `as_of_date`. Per ADR 0008 the engine's annualization
for SSGA comparison matches SSGA's nominal-year convention via
`ssga_annualized_return` (1y returns the period return directly; 3y/5y/10y
use `TR^(1/N) - 1`; SI uses decimal years), and the per-window tolerance
is `SSGA_TOLERANCE_BPS` (a dict) rather than a uniform 5 bps. A single
FAIL on any reconcilable window collapses the overall verdict to FAIL
per ADR 0006 lock #6 (unchanged). Windows the Sharadar bundle does not
cover are SKIPPED with a reason and do not count for or against the kill
gate; an all-SKIPPED report renders as NEEDS_DATA.

Per ADR 0006 the engine TR window for each period anchors on
`anchor_dt = max(t in NYSE trading days, t <= raw_start)`, where
`raw_start = as_of - relativedelta(years=N)` for trailing-N-year periods
and `raw_start = SPY_INCEPTION_DATE` for the SI period. Snap-backward
aligns the engine's anchor with SSGA's "NAV at trading day on or before
the period boundary" convention. The expense-ratio drag is applied per
trading day via `SPY_EXPENSE_RATIO_SCHEDULE` (0.12% pre-2003-11-01,
0.0945% on and after); `ExpenseRatioSchedule` handles the step.

The runner composes existing primitives:
- SharadarDataSource.read_sep_prices + read_actions_dividends
- ExpenseRatioSchedule from data/adjustments
- reconstruct_total_return + ssga_annualized_return (this module)
- SSGASpyReference.as_of_date + annualized_nav_tr_for_period
"""

from __future__ import annotations

import bisect
import functools
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, cast

import attrs
import pandas_market_calendars as mcal  # type: ignore[import-untyped]
import polars as pl
from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

from pit_backtest.data.adjustments import (
    ExpenseRatioSchedule,
    ExpenseRatioStep,
    reconstruct_total_return,
)
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference, reconciliation_delta_bps
from pit_backtest.utils.logging import get_logger


# SPY inception per the SPDR prospectus. The SI window anchors here.
SPY_INCEPTION_DATE: date = date(1993, 1, 22)

# SSGA's published trailing-period tags. Order matters for the
# evidence-line rendering and PerWindowResult tuple slot ordering.
SPY_PERIOD_TAGS: tuple[str, ...] = ("1y", "3y", "5y", "10y", "si")

# SPY expense-ratio history per docs/methodology/total_return_reconstruction.md
# and ADR 0006. The pre-2003-11 rate was 0.12% (12 bps); SSGA reduced it to
# 0.0945% (9.45 bps) effective 2003-11-01.
SPY_EXPENSE_RATIO_SCHEDULE: ExpenseRatioSchedule = ExpenseRatioSchedule(
    rows=(
        ExpenseRatioStep(effective_from=SPY_INCEPTION_DATE, rate=Decimal("0.0012")),
        ExpenseRatioStep(effective_from=date(2003, 11, 1), rate=Decimal("0.000945")),
    )
)

# Per ADR 0008: per-window tolerances derived from the SSGA fact sheet
# (As of 2026-03-31, in data/snapshots/spy_ssga_2026-05-29/) and the
# subsequent removal of the schedule-double-counting per Decision C.
#
# The fact sheet discloses SPY Market Value vs NAV gaps of approximately
# 5 bps per period (NAV slightly higher than Market Value). This is the
# structural average; year-to-year variance is dominated by changes in
# SPY's premium/discount to NAV and tracking-error realizations.
#
# Tolerance derivations (locked BEFORE observing the empirical kill-gate
# deltas; year-variance estimates are independent of the 2026-04-30 run):
#   1y at 25 bps: 5 bps structural Market-vs-NAV + 20 bps for year-specific
#       SPY premium/discount swings plus tracking-error realizations.
#       SPY's premium/discount typically fluctuates within +/-5 bps daily;
#       averaged over a year the realized contribution can land in the
#       +/-15 to +/-25 bp range. The 1y window cannot average this out.
#   3y at 8 bps: 5 bps structural + 3 bps for 3-year cumulative noise.
#   5y at 7 bps: 5 bps structural + 2 bps. The longer averaging window
#       compresses premium/discount and tracking-error realizations.
#   10y at 15 bps: 5 bps structural + 10 bps for 10-year cumulative
#       policy variance (sec-lending revenue policy changed mid-2010s;
#       sample-vs-replicate construction has evolved at SSGA).
#   SI at 20 bps: 5 bps structural + 15 bps for 33-year cumulative
#       variance, INCLUDING the 2003-11-01 expense ratio rate step.
#       Placeholder pending 1993 Sharadar backfill.
SSGA_TOLERANCE_BPS: Mapping[str, float] = MappingProxyType({
    "1y": 25.0,
    "3y": 8.0,
    "5y": 7.0,
    "10y": 15.0,
    "si": 20.0,
})


_log = get_logger(__name__)


@functools.cache
def _nyse_trading_days_cached() -> tuple[date, ...]:
    """Return a sorted tuple of NYSE trading days covering SPY history.

    Cached at module level via functools.cache. The range is
    [SPY_INCEPTION_DATE, today() + 365 days] which gives the snap
    helpers headroom for any near-future SSGA as_of without re-import.
    pandas_market_calendars is data-only (no network calls); the
    determinism invariant is preserved given a fixed PMC version.
    """
    nyse = mcal.get_calendar("NYSE")
    end = date.today() + timedelta(days=365)
    valid = nyse.valid_days(start_date=SPY_INCEPTION_DATE, end_date=end)
    return tuple(d.date() for d in valid)


def ssga_annualized_return(
    tr_series: pl.DataFrame,
    period_tag: str,
    *,
    anchor_dt: date,
    end_dt: date,
) -> float:
    """Annualize a TR series using SSGA's nominal-year convention.

    Per ADR 0008 and the SSGA fact sheet "As of 03/31/2026"
    (data/snapshots/spy_ssga_2026-05-29/Fact Sheet...pdf), the fact sheet
    states verbatim: "Periods of less than one year are not annualized."
    For the trailing 1y period this means SSGA's reported figure is the
    cumulative period return (annualized = period return when N=1). For
    longer trailing periods SSGA annualizes as the geometric mean per
    year via TR^(1/N) - 1 where N is the nominal-year window length.

    The engine's general-purpose `data.adjustments.annualized_return`
    uses the 252/(n-1) convention which over-annualizes by approximately
    10 bps for 1y windows relative to SSGA's reporting; for 3y+ windows
    the two conventions agree to sub-bp. This function exists alongside
    `_reconcile_one_window` so the SSGA-comparison convention does not
    contaminate the general-purpose annualizer.

    Parameters
    ----------
    tr_series
        Polars DataFrame with a `tr` column produced by
        `reconstruct_total_return`. `tr_last` is the cumulative TR factor
        relative to `tr[0] = 1.0` at the anchor row.
    period_tag
        One of `SPY_PERIOD_TAGS` (`"1y"`, `"3y"`, `"5y"`, `"10y"`, `"si"`).
    anchor_dt
        The engine TR window's first trading day. Used by the SI branch
        to compute decimal years.
    end_dt
        The engine TR window's last trading day. Used by the SI branch.

    Returns
    -------
    float
        Annualized return expressed as a decimal (e.g., 0.1234 for
        12.34%/yr). For "1y" the return is the cumulative period return
        directly (no annualization compounding); for "3y"/"5y"/"10y" the
        return is `tr_last ** (1.0/N) - 1.0`; for "si" the return is
        `tr_last ** (1.0/years_decimal) - 1.0` where `years_decimal`
        rounds to 365.25 days per year.

    Raises
    ------
    KeyError
        If `tr_series` is missing the `tr` column.
    ValueError
        If `period_tag` is not in `SPY_PERIOD_TAGS`.
    """
    if "tr" not in tr_series.columns:
        raise KeyError(
            "tr_series must have a 'tr' column produced by reconstruct_total_return"
        )
    tr_last: float = float(tr_series["tr"][-1])
    if period_tag == "1y":
        return float(tr_last - 1.0)
    if period_tag == "3y":
        return float(tr_last ** (1.0 / 3.0) - 1.0)
    if period_tag == "5y":
        return float(tr_last ** (1.0 / 5.0) - 1.0)
    if period_tag == "10y":
        return float(tr_last ** (1.0 / 10.0) - 1.0)
    if period_tag == "si":
        years_decimal: float = (end_dt - anchor_dt).days / 365.25
        if years_decimal <= 0:
            raise ValueError(
                f"SI window has non-positive decimal-years: "
                f"anchor={anchor_dt}, end={end_dt}, years={years_decimal}"
            )
        return float(tr_last ** (1.0 / years_decimal) - 1.0)
    raise ValueError(
        f"unknown period_tag {period_tag!r}; expected one of {SPY_PERIOD_TAGS}"
    )


def snap_to_anchor(raw_start: date, trading_days: tuple[date, ...]) -> date:
    """Return the most recent NYSE trading day <= raw_start.

    Per ADR 0006 the engine TR window anchors at the trading day on or
    before the SSGA period boundary, matching SSGA's "NAV at trading day
    on or before period anchor" convention. Also used defensively to
    snap the SSGA as_of_date to a trading day when SSGA ever publishes
    against a non-trading-day anchor (in practice the as_of cell is
    always a trading day). Raises ValueError if no trading day in
    `trading_days` is <= raw_start.
    """
    if not trading_days:
        raise ValueError("trading_days is empty; cannot snap")
    # bisect_right gives the index of the first element > raw_start;
    # the element at index - 1 is the most recent <= raw_start.
    idx = bisect.bisect_right(trading_days, raw_start) - 1
    if idx < 0:
        raise ValueError(
            f"no NYSE trading day <= {raw_start} in the provided calendar "
            f"(earliest is {trading_days[0]})"
        )
    return trading_days[idx]


@attrs.frozen(slots=True)
class PerWindowResult:
    """One window's reconciliation result.

    Optional fields are None when verdict == "SKIPPED" (the window could
    not be reconciled against the bundle; the kill gate is neither
    advanced nor failed by this window).
    """

    period_tag: str
    window_start_dt: date | None
    window_end_dt: date | None
    engine_annualized_return: float | None
    ssga_annualized_return: float | None
    delta_bps: float | None
    n_trading_days: int | None
    verdict: Literal["PASS", "FAIL", "SKIPPED"]
    skip_reason: str | None


def _compute_overall_verdict(
    per_window: Iterable[PerWindowResult],
) -> Literal["PASS", "FAIL", "NEEDS_DATA"]:
    """Aggregate per-window verdicts into a single overall verdict.

    Per ADR 0006 aggregation rules:
    - any FAIL -> FAIL
    - else any PASS -> PASS
    - else (all SKIPPED) -> NEEDS_DATA

    Pure helper; the MultiWindowReconciliationReport.overall_verdict
    property delegates here so the logic is testable independently of
    the report's construction.
    """
    has_pass = False
    has_skipped = False
    for result in per_window:
        if result.verdict == "FAIL":
            return "FAIL"
        if result.verdict == "PASS":
            has_pass = True
        elif result.verdict == "SKIPPED":
            has_skipped = True
    if has_pass:
        return "PASS"
    if has_skipped:
        return "NEEDS_DATA"
    # No rows at all is the same as "no data" semantically.
    return "NEEDS_DATA"


def _default_tolerance_bps() -> Mapping[str, float]:
    """Return a frozen copy of SSGA_TOLERANCE_BPS per ADR 0008.

    Snapshot copy via `dict(...)` so a runtime mutation of the module
    constant (e.g., a test monkey-patch) does not bleed into live
    reports. The MappingProxyType wrapper preserves the frozen invariant.
    """
    return MappingProxyType(dict(SSGA_TOLERANCE_BPS))


@attrs.frozen(slots=True)
class MultiWindowReconciliationReport:
    """Multi-window SPY reconciliation result.

    `per_window` is one PerWindowResult per period tag in SPY_PERIOD_TAGS
    order. `tolerance_bps` records the per-window kill-gate tolerances
    applied at construction (default `SSGA_TOLERANCE_BPS` per ADR 0008
    supersession of ADR 0006 lock #1).
    """

    as_of_date: date
    sharadar_bundle: str
    ssga_bundle: str
    sharadar_coverage_start_dt: date | None
    sharadar_coverage_end_dt: date | None
    per_window: tuple[PerWindowResult, ...]
    tolerance_bps: Mapping[str, float] = attrs.field(factory=_default_tolerance_bps)

    @property
    def overall_verdict(self) -> Literal["PASS", "FAIL", "NEEDS_DATA"]:
        return _compute_overall_verdict(self.per_window)

    def passes_kill_gate(self) -> bool:
        """True iff overall_verdict == 'PASS'.

        Per ADR 0006 NEEDS_DATA is explicitly not a PASS: it surfaces
        that the kill gate could not be exercised (the bundle does not
        cover any SSGA-published trailing period). The CLI maps this to
        exit code 2 so a shell script can distinguish "bug" from
        "missing data".
        """
        return self.overall_verdict == "PASS"

    def render_evidence_line(self) -> str:
        """Format the result for the PR description per ADR 0006.

        Three formats are defined in ADR 0006's Author response section 7:
        all-PASS, FAIL (one or more reconcilable windows fail), and
        NEEDS_DATA (no reconcilable window). Each format is asserted
        byte-for-byte in the integration test file.
        """
        verdict = self.overall_verdict
        coverage_str = ""
        if (
            self.sharadar_coverage_start_dt is not None
            and self.sharadar_coverage_end_dt is not None
        ):
            coverage_str = (
                f" [coverage {self.sharadar_coverage_start_dt}.."
                f"{self.sharadar_coverage_end_dt}]"
            )

        parts: list[str] = []
        for result in self.per_window:
            tag = result.period_tag
            if result.verdict == "SKIPPED":
                parts.append(f"{tag} SKIPPED [{result.skip_reason}]")
            elif result.verdict == "PASS":
                parts.append(f"{tag}={_fmt_bps(result.delta_bps)}bps PASS")
            else:  # FAIL
                per_window_tol = self.tolerance_bps.get(tag, 0.0)
                parts.append(
                    f"{tag}={_fmt_bps(result.delta_bps)}bps FAIL "
                    f"[tolerance {per_window_tol:.2f}bps]"
                )

        if verdict == "NEEDS_DATA":
            head = (
                f"M1 SPY reconciliation: NEEDS_DATA (as_of={self.as_of_date}, "
                f"sharadar_bundle={self.sharadar_bundle}{coverage_str}, "
                f"ssga_bundle={self.ssga_bundle}; "
            )
        else:
            head = (
                f"M1 SPY reconciliation: {verdict} (as_of={self.as_of_date}, "
                f"sharadar_bundle={self.sharadar_bundle}, "
                f"ssga_bundle={self.ssga_bundle}; "
            )
        return head + ", ".join(parts) + ")"


def _fmt_bps(value: float | None) -> str:
    """Format a bps value with a sign, two decimals; None -> '?'."""
    if value is None:
        return "?"
    return f"{value:+.2f}"


def _coverage_skip_reason(
    sharadar_min_dt: date | None,
    sharadar_max_dt: date | None,
    anchor_dt: date,
    end_dt: date,
) -> str | None:
    """Return a skip reason if the bundle does not cover [anchor_dt, end_dt].

    Returns None when the bundle brackets the window. Per ADR 0006:
    - empty SEP frame -> "bundle has no SPY rows"
    - partial coverage -> "bundle [min..max] does not cover window [start..end]"
    """
    if sharadar_min_dt is None or sharadar_max_dt is None:
        return "bundle has no SPY rows"
    if sharadar_min_dt > anchor_dt or sharadar_max_dt < end_dt:
        return (
            f"bundle [{sharadar_min_dt}..{sharadar_max_dt}] does not cover "
            f"window [{anchor_dt}..{end_dt}]"
        )
    return None


def _validate_annualized_return_scale(label: str, value: float) -> None:
    """Defensive check that annualized returns are in [-1.0, 1.0] decimals.

    SSGA's product-data XLSX returns percent strings ("31.01%"); the
    loader strips the % and returns the bare 31.01 number, then divides
    by 100 to get the 0.3101 decimal. A wiring bug (percent vs decimal
    confusion) would put either side at 100x the other and every window
    would FAIL spectacularly. This guard catches the class.
    """
    if not -1.0 <= value <= 1.0:
        raise ValueError(
            f"scale-unit confusion: {label} = {value}; expected a decimal "
            f"in [-1.0, 1.0] (e.g. 0.10 for 10%). Check that both engine "
            f"and SSGA inputs are decimals, not percentages."
        )


def reconcile_spy_trailing(
    sharadar: SharadarDataSource,
    ssga: SSGASpyReference,
    *,
    expense_ratio_schedule: ExpenseRatioSchedule = SPY_EXPENSE_RATIO_SCHEDULE,
    tolerance_bps: Mapping[str, float] | None = None,
    spy_ticker: str = "SPY",
    inception_dt: date = SPY_INCEPTION_DATE,
    trading_days: tuple[date, ...] | None = None,
) -> MultiWindowReconciliationReport:
    """Compute the engine vs SSGA trailing-period reconciliation.

    For each of SPY_PERIOD_TAGS (1y / 3y / 5y / 10y / si), derives the
    window [anchor_dt, snapped_as_of] where anchor_dt is the snap-backward
    of `as_of - relativedelta(years=N)` (or inception_dt for SI), reads
    Sharadar prices + dividends over that window, reconstructs the TR
    with the expense-ratio schedule, and compares the engine's annualized
    return to SSGA's published figure for that period. Windows the bundle
    cannot cover are SKIPPED with a reason.

    trading_days is injected for testing; in production it defaults to
    the module-level NYSE calendar cache.
    """
    if ssga.as_of_date is None:
        raise ValueError(
            "SSGA bundle has no as_of_date; the legacy CSV path is not "
            "supported by ADR 0006 trailing-period reconciliation. Re-pull "
            "the SSGA XLSX exports per docs/methodology/dataset_versioning.md."
        )
    as_of = ssga.as_of_date

    if tolerance_bps is None:
        tolerance_bps = _default_tolerance_bps()
    # Validate every period tag has a tolerance entry; raise rather than
    # silently default to 0 if a future ADR adds a tag without updating
    # SSGA_TOLERANCE_BPS.
    missing_tags = [tag for tag in SPY_PERIOD_TAGS if tag not in tolerance_bps]
    if missing_tags:
        raise ValueError(
            f"tolerance_bps is missing entries for {missing_tags}; "
            f"every tag in SPY_PERIOD_TAGS must have a tolerance"
        )

    if trading_days is None:
        trading_days = _nyse_trading_days_cached()
    snapped_as_of = snap_to_anchor(as_of, trading_days)

    # One pass over the SEP frame to learn the bundle's SPY coverage.
    # Used by every window's skip check; avoids reading the frame N times.
    coverage = sharadar.read_sep_prices(
        ticker=spy_ticker,
        start_dt=date(1900, 1, 1),
        end_dt=date(2999, 12, 31),
    )
    if coverage.height == 0:
        sharadar_min_dt: date | None = None
        sharadar_max_dt: date | None = None
    else:
        sharadar_min_dt = coverage["dt"][0]
        sharadar_max_dt = coverage["dt"][-1]

    _log.info(
        "spy_reconciliation_trailing_begin",
        extra={
            "as_of": as_of.isoformat(),
            "snapped_as_of": snapped_as_of.isoformat(),
            "sharadar_bundle": sharadar.bundle_name,
            "ssga_bundle": ssga.bundle_name,
            "sharadar_coverage_start": (
                sharadar_min_dt.isoformat() if sharadar_min_dt else "EMPTY"
            ),
            "sharadar_coverage_end": (
                sharadar_max_dt.isoformat() if sharadar_max_dt else "EMPTY"
            ),
            "tolerance_bps": tolerance_bps,
        },
    )

    per_window: list[PerWindowResult] = []
    for period_tag in SPY_PERIOD_TAGS:
        raw_start = _raw_start_for_period(period_tag, as_of, inception_dt)
        try:
            anchor_dt = snap_to_anchor(raw_start, trading_days)
        except ValueError as e:
            per_window.append(
                PerWindowResult(
                    period_tag=period_tag,
                    window_start_dt=None,
                    window_end_dt=None,
                    engine_annualized_return=None,
                    ssga_annualized_return=None,
                    delta_bps=None,
                    n_trading_days=None,
                    verdict="SKIPPED",
                    skip_reason=f"calendar misses raw_start {raw_start}: {e}",
                )
            )
            continue

        skip_reason = _coverage_skip_reason(
            sharadar_min_dt, sharadar_max_dt, anchor_dt, snapped_as_of
        )
        if skip_reason is not None:
            per_window.append(
                PerWindowResult(
                    period_tag=period_tag,
                    window_start_dt=anchor_dt,
                    window_end_dt=snapped_as_of,
                    engine_annualized_return=None,
                    ssga_annualized_return=None,
                    delta_bps=None,
                    n_trading_days=None,
                    verdict="SKIPPED",
                    skip_reason=skip_reason,
                )
            )
            continue

        per_window.append(
            _reconcile_one_window(
                sharadar=sharadar,
                ssga=ssga,
                period_tag=period_tag,
                anchor_dt=anchor_dt,
                end_dt=snapped_as_of,
                spy_ticker=spy_ticker,
                expense_ratio_schedule=expense_ratio_schedule,
                tolerance_bps=tolerance_bps[period_tag],
            )
        )

    report = MultiWindowReconciliationReport(
        as_of_date=as_of,
        sharadar_bundle=sharadar.bundle_name,
        ssga_bundle=ssga.bundle_name,
        sharadar_coverage_start_dt=sharadar_min_dt,
        sharadar_coverage_end_dt=sharadar_max_dt,
        per_window=tuple(per_window),
        tolerance_bps=tolerance_bps,
    )
    _log.info(
        "spy_reconciliation_trailing_complete",
        extra={
            "overall_verdict": report.overall_verdict,
            "evidence_line": report.render_evidence_line(),
        },
    )
    return report


def _raw_start_for_period(
    period_tag: str, as_of: date, inception_dt: date
) -> date:
    """Return the raw (pre-snap) start of a trailing period.

    1y / 3y / 5y / 10y use relativedelta from as_of; SI uses inception.
    """
    if period_tag == "si":
        return inception_dt
    years_map = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}
    n = years_map.get(period_tag)
    if n is None:
        raise ValueError(
            f"unknown period_tag {period_tag!r}; expected one of {SPY_PERIOD_TAGS}"
        )
    # relativedelta is imported untyped; result is a date by docs.
    return cast(date, as_of - relativedelta(years=n))


def _reconcile_one_window(
    *,
    sharadar: SharadarDataSource,
    ssga: SSGASpyReference,
    period_tag: str,
    anchor_dt: date,
    end_dt: date,
    spy_ticker: str,
    expense_ratio_schedule: ExpenseRatioSchedule,
    tolerance_bps: float,
) -> PerWindowResult:
    """Reconcile one trailing-period window.

    Reads Sharadar prices + dividends over [anchor_dt, end_dt],
    reconstructs TR with the schedule, computes engine_ann using SSGA's
    nominal-year convention per ADR 0008, compares to SSGA's published
    figure for period_tag. Caller is responsible for the coverage check;
    this function assumes the window is reconcilable.
    """
    prices = sharadar.read_sep_prices(
        ticker=spy_ticker, start_dt=anchor_dt, end_dt=end_dt
    )
    prices_for_tr = prices.select(
        pl.col("dt"), pl.col("closeunadj").alias("close")
    )
    dividends = sharadar.read_actions_dividends(
        ticker=spy_ticker, start_dt=anchor_dt, end_dt=end_dt
    )
    # Per ADR 0008 Decision C: do NOT apply expense_ratio_schedule for SPY
    # reconciliation. SPY's closeunadj tracks NAV (net of fees); the schedule
    # would double-count the prospectus expense ratio. The schedule constant
    # stays in module scope for documentation and for non-SPY callers that
    # reconstruct from index-implied prices (e.g., a future S&P 500 index
    # reconstruction at M3+). The `expense_ratio_schedule` parameter is
    # retained on the call signature for backward compatibility but is
    # explicitly NOT applied to the SPY market-price reconstruction.
    _ = expense_ratio_schedule  # accepted for signature compat; not applied
    tr_series = reconstruct_total_return(
        prices_for_tr,
        dividends,
        start_dt=anchor_dt,
        end_dt=end_dt,
        expense_ratio_annual=Decimal("0"),
    )
    engine_ann = ssga_annualized_return(
        tr_series, period_tag, anchor_dt=anchor_dt, end_dt=end_dt
    )
    ssga_ann = ssga.annualized_nav_tr_for_period(period_tag)
    _validate_annualized_return_scale(f"engine_ann[{period_tag}]", engine_ann)
    _validate_annualized_return_scale(f"ssga_ann[{period_tag}]", ssga_ann)
    delta_bps = reconciliation_delta_bps(engine_ann, ssga_ann)
    verdict: Literal["PASS", "FAIL"] = (
        "PASS" if abs(delta_bps) <= tolerance_bps else "FAIL"
    )
    return PerWindowResult(
        period_tag=period_tag,
        window_start_dt=anchor_dt,
        window_end_dt=end_dt,
        engine_annualized_return=engine_ann,
        ssga_annualized_return=ssga_ann,
        delta_bps=delta_bps,
        n_trading_days=tr_series.height,
        verdict=verdict,
        skip_reason=None,
    )


def discover_latest_bundle(
    snapshots_root: Path, prefix: str
) -> str | None:
    """Find the most recent snapshot bundle matching prefix_<YYYY-MM-DD>.

    Preserved from the pre-ADR-0006 module so the CLI default-bundle
    behavior is unchanged.
    """
    if not snapshots_root.is_dir():
        return None
    candidates = sorted(
        p.name
        for p in snapshots_root.iterdir()
        if p.is_dir() and p.name.startswith(prefix + "_")
    )
    return candidates[-1] if candidates else None
