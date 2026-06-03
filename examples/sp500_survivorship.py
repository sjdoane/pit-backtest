"""S&P 500 survivorship study CLI (M3 PR 5c; ADR 0002 acceptance criterion 1).

Computes the four headline numbers:
  - PIT S&P 500 count at `--pit-date`
  - Current S&P 500 count at `--as-of`
  - Survivor count (members in both sets)
  - Equal-weight CAGR delta (current cohort minus PIT cohort) in bps

The CAGR delta is the empirical survivorship-bias signal: a positive value
means the look-back cohort outperforms the PIT cohort because the
look-back excludes the names that delisted between pit_date and as_of.

Methodology (locked per ADR 0002 acceptance criterion 1 + PR 5c
Plan-reviewer Choice A ratification with caveat disclosure):
  - Buy-and-hold equal-weight (no monthly rebalance).
  - Per-ticker total-return series via `reconstruct_total_return` on SEP
    closeunadj + ACTIONS dividends (`_member_tr_series`), SPLIT-ADJUSTED
    (closeunadj is the as-traded price; a reverse split in a delisted name
    would otherwise read as a spurious gain) via the shared
    `split_adjusted_prices_and_dividends` helper. The dividend amounts are
    AS-TRADED, decoded from Sharadar's back-adjusted ACTIONS storage by
    `read_actions_dividends` (ADR 0018): left raw, a reverse-split name's stored
    value mismatches the closeunadj basis by the product of later split ratios
    and explodes the reinvestment (the prior 79x to 255x bankrupt terminals).
  - Cohort terminal TR = arithmetic mean of per-ticker terminal TRs; the median
    is reported alongside as the outlier-robust read.
  - CAGR = cohort_terminal_TR ** (1 / years) - 1; years = calendar days
    between pit_date and as_of divided by 365.25.
  - PIT cohort: all S&P 500 members at pit_date; a member that delists
    mid-window holds its terminal TR flat to as_of (zero T-bill accrual; v1).
  - Current cohort: members at as_of WHO HAD a SEP price at pit_date. A
    post-pit_date IPO (no pit-date price) is SKIPPED with a logged warning.
  - The two-panel cohort equity curve (`compute_cohort_nav_curve`) is the
    equal-weight buy-and-hold NAV of each cohort over a month-end grid, with the
    invariant that the curve terminal equals the headline mean terminal TR.

Caveats surfaced in the rendered Markdown report:
  1. Buy-and-hold (no rebalance) is NOT the monthly-rebalanced 1.4-1.5 pp
     Hsu-Hutchinson / Brown-Goetzmann figure; the delta here is dominated by
     current-cohort inclusion bias (today's members were selected for growing
     into the index), so it is the inclusion-plus-survivorship gap of this
     construction, read alongside the outlier-robust median.
  2. Zero T-bill accrual on delisting cash adds an estimated ~13 bps annualized
     bias over the window (T-bill ~2.5% nominal).
  3. Skipping current members without pit-date prices SHRINKS the delta
     because TSLA / SNOW / etc. would have boosted current cohort TR.
  4. Delisted PIT members are the survivorship-signal source.
  5. Dividend / split basis is corrected (ADR 0018): no per-name dividend data
     is dropped or capped; the bankrupt names now read near-zero terminals.

The two-panel cohort equity-curve plot contemplated at ADR 0002 line 96 SHIPS
in M5 `scripts/figures/` (`compute_cohort_nav_curve` feeds
`scripts/figures/build_study_artifact.py` -> `survivorship.json` ->
`docs/figures/survivorship_two_panel.png`); the deferral footer at
`docs/decisions/0002-roadmap-review.md` is superseded by ADR 0018.

Exit codes (locked per Plan-reviewer High 3):
  0 = success
  1 = PIT universe empty (pit_count == 0)
  2 = missing bundle (discover_latest_bundle returns None)
  3 = data quality contract failure raised at SharadarDataSource.__init__
  4 = bundle present but missing required M3 tables (TICKERS + SP500)

References:
  - Hsu, J., and Hutchinson, C. (2006). "Why are S&P 500 Stocks Different?"
  - Brown, S., Goetzmann, W., Ibbotson, R., and Ross, S. (1992).
    "Survivorship Bias in Performance Studies", RFS 5(4).
  - docs/decisions/0002-roadmap-review.md M3 acceptance criterion 1.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import attrs
import polars as pl

from pit_backtest.data.adjustments import (
    SPLIT_LIKE_ACTIONS,
    reconstruct_total_return,
    split_adjusted_prices_and_dividends,
)
from pit_backtest.data.contracts import DataQualityError
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.manifest import (
    ManifestParseError,
    SnapshotMismatchError,
)
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import UniverseValidationError
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.utils.logging import configure_logging

_LOG = logging.getLogger(__name__)


_DEFAULT_SNAPSHOTS_ROOT = (
    Path(__file__).resolve().parent.parent / "data" / "snapshots"
)
_DEFAULT_PIT_DATE = date(2010, 1, 4)  # first NYSE trading day of 2010
_REQUIRED_M3_TABLES: frozenset[str] = frozenset({"sp500", "tickers", "sep"})

_EXIT_SUCCESS = 0
_EXIT_UNIVERSE_EMPTY = 1
_EXIT_MISSING_BUNDLE = 2
_EXIT_DATA_QUALITY = 3
_EXIT_MISSING_M3_TABLES = 4


@attrs.frozen(slots=True)
class SurvivorshipReport:
    """The four headline numbers + the audit lists.

    `pit_cohort_cagr` and `current_cohort_cagr` are decimal fractions
    (0.05 = 5%); `cagr_delta_bps` is in basis points. The audit lists
    (`skipped_current_without_2010_price`, `delisted_pit_members`) let
    the operator reconcile the cohort sizes between the two universes.
    """

    pit_date: date
    as_of_date: date
    bundle_name: str
    years: float
    pit_count: int
    current_count: int
    survivor_count: int
    pit_cohort_priced_at_pit_date: int
    current_cohort_priced_at_pit_date: int
    skipped_current_without_2010_price: tuple[AssetId, ...]
    delisted_pit_members: tuple[AssetId, ...]
    pit_cohort_mean_terminal_tr: float
    current_cohort_mean_terminal_tr: float
    pit_cohort_median_terminal_tr: float
    current_cohort_median_terminal_tr: float
    pit_cohort_cagr: float
    current_cohort_cagr: float
    cagr_delta_bps: float


@dataclass(frozen=True)
class _CohortBuildResult:
    terminal_trs: list[float]
    skipped_asset_ids: list[AssetId]


def _to_nyse_close(d: date) -> datetime:
    """Promote a calendar date to America/New_York 16:00 close convention.

    Matches the `_row_date_to_datetime` convention used across M3 PR 3 and
    PR 4 for ACTIONS row promotion. Locally defined so the CLI does not
    depend on a private module symbol.
    """
    return datetime.combine(d, time(16, 0))


def _resolve_ticker_at(
    source: SharadarDataSource, asset_id: AssetId, dt: date
) -> str | None:
    """Resolve asset_id to its ticker at a given date via the public reader.

    Per Plan-reviewer Critical 1 the CLI does NOT reach into the private
    `_resolver` attribute; it consumes the public `read_tickers` reader
    with the `active_at` interval filter which already handles the
    [firstpricedate, lastpricedate] coverage check. Returns None when
    the asset is not in the resolver's index at `dt` (no 2010 price for
    a post-2010 IPO).
    """
    df = source.read_tickers(permaticker=int(asset_id), active_at=dt)
    if df.height == 0:
        return None
    tickers = df["ticker"].to_list()
    return str(tickers[-1])


def _member_tr_series(
    source: SharadarDataSource,
    ticker: str,
    pit_date: date,
    as_of_date: date,
) -> pl.DataFrame | None:
    """Buy-and-hold daily total-return series (dt, tr) over [pit_date, as_of].

    Single source of truth for both the headline terminal TR
    (`_compute_terminal_tr` returns its last bar) and the cohort equity curve
    (`compute_cohort_nav_curve` forward-fills it onto a month-end grid), so the
    curve's terminal equals the headline mean by construction.

    For a ticker that delists mid-window, `read_sep_prices` returns rows only
    through `lastpricedate`; `reconstruct_total_return` clamps to that range and
    the series ends at the last trading bar. The "no T-bill accrual on delisting
    cash" caveat is encoded downstream: the equity curve holds the terminal TR
    flat across the remaining grid points.

    SPLIT-ADJUSTED: the as-traded `closeunadj` jumps on a split ex-date, so a
    delisted/bankrupt name that reverse-splits before delisting would otherwise
    show an astronomically inflated terminal TR. The price is divided by the
    cumulative in-window split factor via `split_adjusted_prices_and_dividends`
    (split-like actions per `SPLIT_LIKE_ACTIONS` include `adrratiosplit`).

    AS-TRADED DIVIDENDS (ADR 0018): `read_actions_dividends` returns the cash
    actually paid per share, decoded from Sharadar's back-adjusted ACTIONS
    storage; left raw, a reverse-split name's stored dividend value mismatches
    the `closeunadj` basis by the product of later split ratios and explodes the
    reinvestment (this is why the bankrupt names previously read terminal TRs of
    79x to 255x even though their price collapsed to ~0). The in-window split
    co-adjustment then puts the as-traded dividend on the same basis as the
    adjusted price.

    Returns None when SEP has fewer than 2 rows in the window (pre-IPO or vendor
    gap). `expense_ratio_annual=Decimal("0")` is passed explicitly so the demo's
    CAGR is unencumbered by the SPY expense-ratio step the M1 reconciliation
    applies.
    """
    prices = source.read_sep_prices(
        ticker=ticker, start_dt=pit_date, end_dt=as_of_date
    )
    if prices.height < 2:
        return None
    splits = source.read_actions(
        ticker=ticker,
        start_dt=pit_date,
        end_dt=as_of_date,
        action_filter=SPLIT_LIKE_ACTIONS,
    ).select(
        pl.col("date").alias("ex_date"),
        pl.col("value").cast(pl.Float64).alias("ratio"),
    )
    dividends = source.read_actions_dividends(
        ticker=ticker, start_dt=pit_date, end_dt=as_of_date
    )
    adj_prices, adj_dividends = split_adjusted_prices_and_dividends(
        prices.select(pl.col("dt"), pl.col("closeunadj")), splits, dividends
    )
    return reconstruct_total_return(
        prices=adj_prices,
        dividends=adj_dividends,
        start_dt=pit_date,
        end_dt=as_of_date,
        expense_ratio_annual=Decimal("0"),
    )


def _compute_terminal_tr(
    source: SharadarDataSource,
    ticker: str,
    pit_date: date,
    as_of_date: date,
) -> float | None:
    """Buy-and-hold terminal total return = the last bar of `_member_tr_series`.

    Returns None when the series is None (fewer than 2 SEP rows in the window).
    """
    series = _member_tr_series(source, ticker, pit_date, as_of_date)
    return None if series is None else float(series["tr"][-1])


def _build_cohort(
    source: SharadarDataSource,
    members: list[AssetId],
    pit_date: date,
    as_of_date: date,
) -> _CohortBuildResult:
    """Compute terminal TRs for the cohort; track skipped AssetIds.

    Iterates `members` in `int(asset_id)` ascending order for determinism
    per docs/methodology/determinism.md Requirement 3. An asset whose
    ticker cannot be resolved at `pit_date` OR whose SEP series has
    fewer than 2 rows in the window is appended to `skipped_asset_ids`
    rather than dropped silently; the audit list surfaces in the report.
    """
    terminal_trs: list[float] = []
    skipped: list[AssetId] = []
    for asset_id in sorted(members, key=int):
        ticker = _resolve_ticker_at(source, asset_id, pit_date)
        if ticker is None:
            skipped.append(asset_id)
            continue
        terminal_tr = _compute_terminal_tr(
            source, ticker, pit_date, as_of_date
        )
        if terminal_tr is None:
            skipped.append(asset_id)
            continue
        terminal_trs.append(terminal_tr)
    return _CohortBuildResult(terminal_trs=terminal_trs, skipped_asset_ids=skipped)


def _cagr_from_terminal_tr(mean_terminal_tr: float, years: float) -> float:
    """Annualize a portfolio-level terminal TR over a calendar-year window.

    Per ADR 0002 acceptance criterion 1's "equal-weight CAGR delta"
    framing: the cohort CAGR is the geometric annualization of the
    arithmetic mean of per-ticker terminal TRs (which equals the
    portfolio NAV at as_of since each $1/N grows to TR_i * (1/N) so the
    portfolio is `mean(TR_i)`). 365.25-day year matches the calendar
    convention the survivorship literature uses for SI windows; the
    deviation from the 252-trading-day `annualized_return` convention
    is approximately 1 basis point over 15 years (Plan-reviewer
    Medium 6).
    """
    if mean_terminal_tr <= 0.0:
        return -1.0
    if years <= 0.0:
        return 0.0
    return float(mean_terminal_tr ** (1.0 / years) - 1.0)


def _median(values: list[float]) -> float:
    """Median of a non-empty list (mean of the two middle values for even n)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _cohort_month_end_grid(pit_date: date, as_of_date: date) -> tuple[date, ...]:
    """Month-end NYSE trading days strictly inside (pit_date, as_of_date),
    bracketed by both endpoints.

    The grid anchors at pit_date (cohort NAV = 1.0) and ends exactly at
    as_of_date so each member's forward-filled terminal lands on the same bar
    the headline terminal TR reads; the month-ends in between trace the path.
    Mirrors `momentum_rebalance_dates`: monthly_last_trading_day returns a
    frozenset and the TestClock pads +/-14 days, so filter to the window then
    sort.
    """
    clock = TestClock(start_dt=pit_date, end_dt=as_of_date)
    month_ends = (
        d
        for d in monthly_last_trading_day(clock.trading_days())
        if pit_date < d < as_of_date
    )
    return tuple(sorted({pit_date, *month_ends, as_of_date}))


def _sample_member_on_grid(
    series: pl.DataFrame, grid: tuple[date, ...]
) -> list[float]:
    """Forward-fill a member's (dt, tr) series onto the grid.

    A backward `join_asof` takes, at each grid date, the last tr at or before
    it: before the member's first bar gives 1.0 (the buy-and-hold base, no
    growth yet); after its last bar (a delisted member) gives the terminal tr
    held flat (the zero T-bill-accrual convention).
    """
    grid_df = pl.DataFrame({"dt": list(grid)}).with_columns(
        pl.col("dt").cast(pl.Date)
    )
    joined = grid_df.join_asof(
        series.select("dt", "tr").sort("dt"), on="dt", strategy="backward"
    )
    return [1.0 if v is None else float(v) for v in joined["tr"].to_list()]


def compute_cohort_nav_curve(
    source: SharadarDataSource,
    members: list[AssetId],
    pit_date: date,
    as_of_date: date,
    grid: tuple[date, ...],
) -> pl.DataFrame:
    """Equal-weight buy-and-hold cohort NAV over `grid`.

    For each cohort member resolvable + priced at pit_date (the SAME skip rules
    as `_build_cohort`, so the member set matches the headline), reconstruct its
    split- and as-traded-dividend-adjusted total-return series via
    `_member_tr_series`, forward-fill it onto `grid`, and average equal-weight.
    Because every member's series is normalized to 1.0 at pit_date,
    `nav[0] == 1.0` and `nav[-1]` equals the headline `cohort_mean_terminal_tr`
    (the figure pipeline asserts this invariant), so the two panels reconcile to
    the headline CAGR delta. Iterates members in `int(asset_id)` order
    (Determinism Requirement 3). Returns columns `dt`, `nav`.
    """
    grid_sorted = tuple(sorted(grid))
    sums = [0.0] * len(grid_sorted)
    n_members = 0
    for asset_id in sorted(members, key=int):
        ticker = _resolve_ticker_at(source, asset_id, pit_date)
        if ticker is None:
            continue
        series = _member_tr_series(source, ticker, pit_date, as_of_date)
        if series is None:
            continue
        sampled = _sample_member_on_grid(series, grid_sorted)
        for i, value in enumerate(sampled):
            sums[i] += value
        n_members += 1
    navs = (
        [s / n_members for s in sums]
        if n_members > 0
        else [1.0] * len(grid_sorted)
    )
    return pl.DataFrame(
        {"dt": list(grid_sorted), "nav": navs}
    ).with_columns(pl.col("dt").cast(pl.Date), pl.col("nav").cast(pl.Float64))


def build_survivorship_figure_data(
    source: SharadarDataSource,
    pit_date: date,
    as_of_date: date,
) -> tuple[SurvivorshipReport, pl.DataFrame, pl.DataFrame]:
    """The report plus the PIT and current cohort NAV curves for the two-panel
    figure (ADR 0002 line 96, ADR 0018).

    Public entry point for `scripts/figures/build_study_artifact.py`. Each curve
    is the equal-weight buy-and-hold cohort NAV over the shared month-end grid;
    by construction `pit_curve.nav[-1] == report.pit_cohort_mean_terminal_tr`
    and likewise for the current cohort, so the figure reconciles to the
    headline CAGR delta (the regenerate.py validator asserts this).
    """
    report = compute_survivorship_report(source, pit_date, as_of_date)
    grid = _cohort_month_end_grid(pit_date, as_of_date)
    pit_members = source.members_at("sp500", _to_nyse_close(pit_date))
    current_members = source.members_at("sp500", _to_nyse_close(as_of_date))
    pit_curve = compute_cohort_nav_curve(
        source, pit_members, pit_date, as_of_date, grid
    )
    current_curve = compute_cohort_nav_curve(
        source, current_members, pit_date, as_of_date, grid
    )
    return report, pit_curve, current_curve


def compute_survivorship_report(
    source: SharadarDataSource,
    pit_date: date,
    as_of_date: date,
) -> SurvivorshipReport:
    """Run the survivorship calculation end-to-end and produce the report.

    Pure function so tests can call it without subprocess overhead and
    assert against the structured `SurvivorshipReport` directly per
    Plan-reviewer Choice E.
    """
    pit_dt = _to_nyse_close(pit_date)
    as_of_dt = _to_nyse_close(as_of_date)

    pit_members = source.members_at("sp500", pit_dt)
    current_members = source.members_at("sp500", as_of_dt)

    pit_set = set(pit_members)
    current_set = set(current_members)
    survivor_set = pit_set & current_set

    pit_cohort = _build_cohort(source, pit_members, pit_date, as_of_date)
    current_cohort = _build_cohort(
        source, current_members, pit_date, as_of_date
    )

    delisted_pit_members = tuple(
        sorted(pit_set - current_set, key=int)
    )

    years = (as_of_date - pit_date).days / 365.25

    pit_mean_tr = (
        sum(pit_cohort.terminal_trs) / len(pit_cohort.terminal_trs)
        if pit_cohort.terminal_trs
        else 0.0
    )
    current_mean_tr = (
        sum(current_cohort.terminal_trs)
        / len(current_cohort.terminal_trs)
        if current_cohort.terminal_trs
        else 0.0
    )

    pit_median_tr = _median(pit_cohort.terminal_trs)
    current_median_tr = _median(current_cohort.terminal_trs)

    pit_cagr = _cagr_from_terminal_tr(pit_mean_tr, years)
    current_cagr = _cagr_from_terminal_tr(current_mean_tr, years)
    cagr_delta_bps = (current_cagr - pit_cagr) * 10_000.0

    return SurvivorshipReport(
        pit_date=pit_date,
        as_of_date=as_of_date,
        bundle_name=source.bundle_name,
        years=years,
        pit_count=len(pit_members),
        current_count=len(current_members),
        survivor_count=len(survivor_set),
        pit_cohort_priced_at_pit_date=len(pit_cohort.terminal_trs),
        current_cohort_priced_at_pit_date=len(current_cohort.terminal_trs),
        skipped_current_without_2010_price=tuple(
            current_cohort.skipped_asset_ids
        ),
        delisted_pit_members=delisted_pit_members,
        pit_cohort_mean_terminal_tr=pit_mean_tr,
        current_cohort_mean_terminal_tr=current_mean_tr,
        pit_cohort_median_terminal_tr=pit_median_tr,
        current_cohort_median_terminal_tr=current_median_tr,
        pit_cohort_cagr=pit_cagr,
        current_cohort_cagr=current_cagr,
        cagr_delta_bps=cagr_delta_bps,
    )


def render_headline_markdown(report: SurvivorshipReport) -> str:
    """Markdown shape of the four headline numbers + caveats."""
    lines: list[str] = [
        "# S&P 500 Survivorship Study",
        "",
        f"Bundle: {report.bundle_name}",
        f"PIT date: {report.pit_date.isoformat()}",
        f"As-of date: {report.as_of_date.isoformat()}",
        f"Years: {report.years:.4f}",
        "",
        "## Headline numbers",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| PIT S&P 500 count ({report.pit_date.isoformat()}) | {report.pit_count} |",
        f"| Current S&P 500 count ({report.as_of_date.isoformat()}) | {report.current_count} |",
        f"| Survivors (in both sets) | {report.survivor_count} |",
        f"| PIT cohort priced at pit_date | {report.pit_cohort_priced_at_pit_date} |",
        f"| Current cohort priced at pit_date | {report.current_cohort_priced_at_pit_date} |",
        f"| PIT cohort mean / median terminal TR | {report.pit_cohort_mean_terminal_tr:.3f}x / {report.pit_cohort_median_terminal_tr:.3f}x |",
        f"| Current cohort mean / median terminal TR | {report.current_cohort_mean_terminal_tr:.3f}x / {report.current_cohort_median_terminal_tr:.3f}x |",
        f"| PIT cohort equal-weight CAGR | {report.pit_cohort_cagr * 100:.4f}% |",
        f"| Current cohort equal-weight CAGR | {report.current_cohort_cagr * 100:.4f}% |",
        f"| CAGR delta (current minus PIT) | {report.cagr_delta_bps:+.2f} bps |",
        "",
        "## Caveats",
        "",
        (
            "1. **Buy-and-hold equal-weight, and what the delta measures.** This "
            "is a buy-and-hold cohort with no monthly rebalance, so it is NOT the "
            "monthly-rebalanced 1.4 to 1.5 percentage-point figure of "
            "Hsu-Hutchinson 2006 / Brown-Goetzmann 1992. It is also dominated by "
            "current-cohort INCLUSION bias: today's members were selected (by "
            "market cap) for having grown into the index, so buying them at "
            "pit-date prices captures that growth, which is a larger effect than "
            "pure delisting survivorship. The mean terminal TR is pulled up by a "
            "few hundred-x survivors (NVDA, AAPL), so the MEDIAN terminal TR is "
            "reported alongside it as the outlier-robust read. Treat the delta as "
            "the inclusion-plus-survivorship gap of this specific construction, "
            "not a literature-comparable survivorship-bias point estimate."
        ),
        (
            "2. **Zero T-bill accrual** on delisting cash. A PIT member that "
            "delists at terminal TR sits flat through `as_of` with no interest "
            "(the equity curve holds it flat at its terminal). The actual T-bill "
            "return over the window (~2.5% nominal) would add ~13 bps annualized; "
            "v1 simplicity."
        ),
        (
            f"3. **Skipped current members without pit-date prices**: "
            f"{len(report.skipped_current_without_2010_price)} AssetIds "
            f"(post-pit-date IPOs). The current cohort is computed over the "
            f"{report.current_cohort_priced_at_pit_date}-name subset that had a "
            f"SEP price at pit_date; this UNDERSTATES the current cohort's "
            f"terminal TR and SHRINKS the delta (the biggest such names, e.g. "
            f"TSLA, would widen it)."
        ),
        (
            f"4. **Delisted PIT members**: "
            f"{len(report.delisted_pit_members)} AssetIds (the "
            f"survivorship-signal source)."
        ),
        (
            "5. **Dividend / split basis (corrected).** Per-ticker total returns "
            "are split-adjusted AND use AS-TRADED dividends decoded from "
            "Sharadar's back-adjusted ACTIONS storage (ADR 0018). Left raw, a "
            "reverse-split name's stored dividend value mismatches the "
            "`closeunadj` price basis by the product of later split ratios, which "
            "previously inflated bankrupt names' REINVESTED terminal TR to 79x to "
            "255x even though their price collapsed to ~0; those names now read "
            "near zero, the honest buy-and-hold outcome. No per-name dividend "
            "data is dropped or capped."
        ),
    ]
    return "\n".join(lines)


def render_verbose_markdown(report: SurvivorshipReport) -> str:
    """Append the audit lists (delisted PIT members + skipped current)."""
    base = render_headline_markdown(report)
    lines = [base, "", "## Audit lists"]
    if report.delisted_pit_members:
        lines.append("")
        lines.append("### Delisted PIT members (in 2010 SP500, not in current)")
        lines.append("")
        lines.append(
            f"AssetIds: {[int(a) for a in report.delisted_pit_members]}"
        )
    if report.skipped_current_without_2010_price:
        lines.append("")
        lines.append("### Skipped current members (no 2010 price)")
        lines.append("")
        lines.append(
            "AssetIds: "
            f"{[int(a) for a in report.skipped_current_without_2010_price]}"
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "S&P 500 survivorship study: compute PIT vs current "
            "equal-weight CAGR delta per ADR 0002 acceptance criterion 1."
        ),
    )
    parser.add_argument(
        "--bundle",
        type=str,
        default=None,
        help=(
            "Snapshot bundle name (e.g. sharadar_2026-05-29); when omitted "
            "the CLI discovers the latest bundle by prefix."
        ),
    )
    parser.add_argument(
        "--bundle-prefix",
        type=str,
        default="sharadar",
        help="Prefix for `discover_latest_bundle` when --bundle is omitted.",
    )
    parser.add_argument(
        "--snapshots-root",
        type=Path,
        default=_DEFAULT_SNAPSHOTS_ROOT,
        help="Directory containing the manifest.toml and bundle subdirs.",
    )
    parser.add_argument(
        "--pit-date",
        type=date.fromisoformat,
        default=_DEFAULT_PIT_DATE,
        help="PIT cohort anchor date (ISO format; default 2010-01-04).",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help=(
            "Cohort termination date (ISO format). When omitted the CLI "
            "uses the bundle's manifest pull_date."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the Markdown report; default stdout.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Append the delisted-PIT and skipped-current audit lists.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns one of the locked exit codes per the module docstring; tests
    call this function directly with `argv=[...]` instead of going
    through subprocess (Plan-reviewer Choice E in-process pattern).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(level=getattr(logging, args.log_level))

    snapshots_root: Path = args.snapshots_root.resolve()

    bundle_name: str | None = args.bundle
    if bundle_name is None:
        bundle_name = discover_latest_bundle(
            snapshots_root, args.bundle_prefix
        )
        if bundle_name is None:
            _LOG.error(
                "no bundle with prefix %r found under %s; pull a "
                "Sharadar snapshot per docs/methodology/dataset_versioning.md.",
                args.bundle_prefix,
                snapshots_root,
            )
            return _EXIT_MISSING_BUNDLE

    try:
        source = SharadarDataSource(bundle_name, snapshots_root)
    except (FileNotFoundError, SnapshotMismatchError, ManifestParseError) as exc:
        _LOG.error("bundle load failed: %s", exc)
        return _EXIT_MISSING_BUNDLE
    except (DataQualityError, UniverseValidationError) as exc:
        _LOG.error("data quality contract failed: %s", exc)
        return _EXIT_DATA_QUALITY

    missing_tables = _REQUIRED_M3_TABLES - source.available_tables
    if missing_tables:
        _LOG.error(
            "bundle %r is missing required M3 tables: %s. "
            "The survivorship demo needs sp500.parquet + tickers.parquet "
            "+ sep.parquet. The M1 SPY-only bundle does not ship these; "
            "see docs/methodology/dataset_versioning.md for the full "
            "Sharadar Premium entitlement.",
            bundle_name,
            sorted(missing_tables),
        )
        return _EXIT_MISSING_M3_TABLES

    as_of_date = args.as_of or source.bundle_entry.pull_date

    # Per post-impl reviewer Delta 2: `SharadarSP500Universe` is lazily
    # constructed via `_sp500_universe` cached_property on first
    # `members_at` call. A `UniverseValidationError` from a defective
    # SP500 event log therefore surfaces inside
    # `compute_survivorship_report`, NOT inside `SharadarDataSource(...)`.
    # Wrap here so exit 3 fires for either failure path.
    try:
        report = compute_survivorship_report(
            source, args.pit_date, as_of_date
        )
    except (DataQualityError, UniverseValidationError) as exc:
        _LOG.error("data quality contract failed during report build: %s", exc)
        return _EXIT_DATA_QUALITY

    if report.pit_count == 0:
        _LOG.error(
            "PIT universe is empty at %s; check the SP500 event log.",
            args.pit_date.isoformat(),
        )
        return _EXIT_UNIVERSE_EMPTY

    markdown = (
        render_verbose_markdown(report)
        if args.verbose
        else render_headline_markdown(report)
    )

    if args.output is not None:
        args.output.write_text(markdown + "\n", encoding="utf-8")
        _LOG.info("wrote survivorship report to %s", args.output)
    else:
        print(markdown)

    if abs(report.cagr_delta_bps) > 500.0:
        _LOG.warning(
            "CAGR delta = %.2f bps exceeds 500 bps; sanity-check the "
            "bundle and the cohort sizes. Published survivorship-bias "
            "studies (Hsu-Hutchinson 2006; Brown-Goetzmann 1992) "
            "typically report 100-200 bps.",
            report.cagr_delta_bps,
        )

    return _EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
