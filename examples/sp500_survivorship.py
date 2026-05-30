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
  - Per-ticker terminal total return via `reconstruct_total_return` on SEP
    closeunadj + ACTIONS dividends.
  - Cohort terminal TR = arithmetic mean of per-ticker terminal TRs.
  - CAGR = cohort_terminal_TR ** (1 / years) - 1; years = calendar days
    between pit_date and as_of divided by 365.25.
  - PIT cohort: all S&P 500 members at pit_date; a member that delists
    mid-window has terminal TR = closeunadj-at-lastpricedate / 2010-price
    held flat to as_of (zero T-bill accrual; v1 simplicity).
  - Current cohort: members at as_of WHO HAD a SEP price at pit_date. A
    post-pit_date IPO (no 2010 price) is SKIPPED with a logged warning;
    the cohort CAGR is computed over the 2010-priced subset only.

Caveats surfaced in the rendered Markdown report (Plan-reviewer High 4):
  1. Buy-and-hold (no rebalance) UNDERSTATES the published 1.4-1.5 pp
     Hsu-Hutchinson / Brown-Goetzmann figure (monthly rebalance).
  2. Zero T-bill accrual on delisting cash adds an estimated ~13 bps
     annualized bias over 15 years (T-bill 2010-2025 ~2.5% nominal).
  3. Skipping current members without 2010 prices SHRINKS the delta
     because TSLA / SNOW / etc. would have boosted current cohort TR.

The two-panel equity-curve plot contemplated at ADR 0002 line 96 is
DEFERRED to M5 `scripts/figures/` per Plan-reviewer High 5; an amendment
footer at `docs/decisions/0002-roadmap-review.md` documents the deferral.

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

from pit_backtest.data.adjustments import reconstruct_total_return
from pit_backtest.data.contracts import DataQualityError
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.manifest import (
    ManifestParseError,
    SnapshotMismatchError,
)
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import UniverseValidationError
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
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


def _compute_terminal_tr(
    source: SharadarDataSource,
    ticker: str,
    pit_date: date,
    as_of_date: date,
) -> float | None:
    """Buy-and-hold terminal total return over [pit_date, as_of_date].

    For a ticker that delists mid-window, `read_sep_prices` returns rows
    only through `lastpricedate`; `reconstruct_total_return` clamps to
    that range and the terminal TR equals the buy-and-hold value at the
    last trading bar. The "no T-bill accrual on delisting cash" caveat
    is structurally encoded here: the cash sits at terminal TR through
    `as_of` with zero growth.

    Returns None when SEP has fewer than 2 rows in the window (pre-IPO
    or vendor gap). `expense_ratio_annual=Decimal("0")` is passed
    explicitly so the demo's CAGR is unencumbered by the SPY expense
    ratio step that the M1 reconciliation applies.
    """
    prices = source.read_sep_prices(
        ticker=ticker, start_dt=pit_date, end_dt=as_of_date
    )
    if prices.height < 2:
        return None
    # Alias closeunadj -> close per the M1 SPY TR pattern at
    # examples/spy_buy_and_hold.py:221 and spy_reconciliation.py:611.
    # `reconstruct_total_return` reads the `close` column and adds the
    # explicit ACTIONS dividends; using the back-adjusted `close` would
    # double-count dividends (the back-adjustment already bakes them in).
    prices_for_tr = prices.select(
        pl.col("dt"), pl.col("closeunadj").alias("close")
    )
    dividends = source.read_actions_dividends(
        ticker=ticker, start_dt=pit_date, end_dt=as_of_date
    )
    tr_series = reconstruct_total_return(
        prices=prices_for_tr,
        dividends=dividends,
        start_dt=pit_date,
        end_dt=as_of_date,
        expense_ratio_annual=Decimal("0"),
    )
    return float(tr_series["tr"][-1])


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
        f"| PIT cohort equal-weight CAGR | {report.pit_cohort_cagr * 100:.4f}% |",
        f"| Current cohort equal-weight CAGR | {report.current_cohort_cagr * 100:.4f}% |",
        f"| CAGR delta (current minus PIT) | {report.cagr_delta_bps:+.2f} bps |",
        "",
        "## Caveats",
        "",
        (
            "1. **Buy-and-hold equal-weight** with no monthly rebalance. "
            "The published Hsu-Hutchinson 2006 + Brown-Goetzmann 1992 "
            "survivorship literature typically reports monthly-rebalanced "
            "CAGR deltas of 1.4 to 1.5 percentage points; this "
            "buy-and-hold demo will UNDERSTATE that headline because "
            "delisted positions stop contributing to the equity book."
        ),
        (
            "2. **Zero T-bill accrual** on delisting cash. A PIT member "
            "that delists at price P sits at terminal TR = P / "
            "initial_price through `as_of`, with no interest. The actual "
            "T-bill return 2010-2025 (~2.5% nominal) would add ~13 bps "
            "annualized; v1 simplicity."
        ),
        (
            f"3. **Skipped current members without 2010 prices**: "
            f"{len(report.skipped_current_without_2010_price)} AssetIds "
            f"(post-2010 IPOs). The current cohort CAGR is computed over "
            f"the {report.current_cohort_priced_at_pit_date}-name subset "
            f"that had a SEP price at pit_date; this UNDERSTATES the "
            f"current cohort's terminal TR and SHRINKS the delta."
        ),
        (
            f"4. **Delisted PIT members**: "
            f"{len(report.delisted_pit_members)} AssetIds (the "
            f"survivorship-signal source)."
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
