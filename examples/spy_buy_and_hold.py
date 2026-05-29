"""SPY buy-and-hold demo (M1 acceptance criterion 1 per ADR 0006).

Loads the Sharadar SEP + ACTIONS snapshot. Without --compare-to-ssga,
reconstructs the SPY total-return series over a configurable window
(default 2005-2024) and prints the annualized return; useful as an
engine-only inspection tool. With --compare-to-ssga, ignores the
window flags and instead runs the trailing-period reconciliation
against SSGA's published 1y / 3y / 5y / 10y / SI annualizations
anchored on SSGA's as_of_date (per ADR 0006); prints the multi-window
evidence line and exits 0 on PASS, 1 on FAIL, 2 on NEEDS_DATA or any
missing-bundle condition.

Per ADR 0006 the --start-dt / --end-dt / --ssga-period flags are
honored only by the inspection path; the kill-gate path is driven by
SSGA's as_of_date and is not user-windowable.

Usage examples (PowerShell):

    uv run python -m examples.spy_buy_and_hold `
        --sharadar-bundle sharadar_2026-05-29 `
        --start-dt 2005-01-03 --end-dt 2024-12-31 --log-level INFO

    uv run python -m examples.spy_buy_and_hold `
        --sharadar-bundle sharadar_2026-05-29 `
        --ssga-bundle spy_ssga_2026-05-29 `
        --compare-to-ssga

Snapshots default to <repo-root>/data/snapshots/. Use --snapshots-root
to point elsewhere. If --sharadar-bundle / --ssga-bundle are omitted,
the latest matching `sharadar_*` / `spy_ssga_*` bundles are used.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl

from pit_backtest.data.adjustments import annualized_return, reconstruct_total_return
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    SPY_EXPENSE_RATIO_SCHEDULE,
    discover_latest_bundle,
    reconcile_spy_trailing,
)
from pit_backtest.utils.logging import configure_logging, get_logger


_DEFAULT_SNAPSHOTS_ROOT = Path(__file__).resolve().parent.parent / "data" / "snapshots"
# Inspection-path default expense ratio (post-2003-11 SPY rate). The
# inspection path takes a Decimal scalar; only the kill-gate path uses
# the full SPY_EXPENSE_RATIO_SCHEDULE for the SI window step.
_INSPECTION_EXPENSE_RATIO = Decimal("0.000945")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "SPY buy-and-hold demo. Without --compare-to-ssga, runs an "
            "engine-only reconstruction over [start-dt, end-dt]. With "
            "--compare-to-ssga, runs the ADR 0006 trailing-period kill gate."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sharadar-bundle",
        type=str,
        default=None,
        help=(
            "Sharadar snapshot bundle name (e.g., sharadar_2026-05-29). "
            "If omitted, the latest matching bundle is used."
        ),
    )
    parser.add_argument(
        "--ssga-bundle",
        type=str,
        default=None,
        help=(
            "SSGA SPY snapshot bundle (e.g., spy_ssga_2026-05-29). "
            "Required with --compare-to-ssga."
        ),
    )
    parser.add_argument(
        "--snapshots-root",
        type=Path,
        default=_DEFAULT_SNAPSHOTS_ROOT,
        help="Directory containing manifest.toml and bundle subdirectories.",
    )
    parser.add_argument(
        "--start-dt",
        type=date.fromisoformat,
        default=date(2005, 1, 3),
        help=(
            "Inspection-path window start (inclusive). Ignored when "
            "--compare-to-ssga is set."
        ),
    )
    parser.add_argument(
        "--end-dt",
        type=date.fromisoformat,
        default=date(2024, 12, 31),
        help=(
            "Inspection-path window end (inclusive). Ignored when "
            "--compare-to-ssga is set."
        ),
    )
    parser.add_argument(
        "--expense-ratio",
        type=Decimal,
        default=_INSPECTION_EXPENSE_RATIO,
        help=(
            "Inspection-path annualized expense-ratio drag (default: SPY "
            "post-2003-11). Ignored when --compare-to-ssga is set "
            "(the kill gate uses the full SPY_EXPENSE_RATIO_SCHEDULE)."
        ),
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default="SPY",
        help="SEP ticker to read.",
    )
    parser.add_argument(
        "--compare-to-ssga",
        action="store_true",
        help=(
            "If set, run the ADR 0006 trailing-period kill gate against "
            "SSGA's published 1y/3y/5y/10y/SI returns and print the "
            "evidence line. Exits 0=PASS, 1=FAIL, 2=NEEDS_DATA."
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Stdlib logging level for the structured log output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    log = get_logger("examples.spy_buy_and_hold")

    sharadar_bundle = args.sharadar_bundle or discover_latest_bundle(
        args.snapshots_root, "sharadar"
    )
    if sharadar_bundle is None:
        log.error(
            "no_sharadar_bundle",
            extra={"snapshots_root": str(args.snapshots_root)},
        )
        print(
            f"no Sharadar bundle under {args.snapshots_root}; pull per "
            f"docs/methodology/dataset_versioning.md and retry",
            file=sys.stderr,
        )
        return 2

    log.info(
        "loading_sharadar_bundle",
        extra={"bundle": sharadar_bundle, "root": str(args.snapshots_root)},
    )
    sharadar = SharadarDataSource(sharadar_bundle, args.snapshots_root)

    if args.compare_to_ssga:
        ssga_bundle = args.ssga_bundle or discover_latest_bundle(
            args.snapshots_root, "spy_ssga"
        )
        if ssga_bundle is None:
            log.error(
                "no_ssga_bundle",
                extra={"snapshots_root": str(args.snapshots_root)},
            )
            print(
                f"--compare-to-ssga requested but no spy_ssga bundle under "
                f"{args.snapshots_root}",
                file=sys.stderr,
            )
            return 2

        log.info("loading_ssga_bundle", extra={"bundle": ssga_bundle})
        ssga = SSGASpyReference(ssga_bundle, args.snapshots_root)
        report = reconcile_spy_trailing(
            sharadar=sharadar,
            ssga=ssga,
            expense_ratio_schedule=SPY_EXPENSE_RATIO_SCHEDULE,
            spy_ticker=args.ticker,
        )
        print(report.render_evidence_line())
        if report.overall_verdict == "PASS":
            return 0
        if report.overall_verdict == "FAIL":
            return 1
        return 2  # NEEDS_DATA

    # Inspection path: engine-only reconstruction over [start_dt, end_dt].
    if args.start_dt < date(2003, 11, 1) <= args.end_dt:
        log.warning(
            "scalar_expense_ratio_crosses_2003_11_step",
            extra={
                "start_dt": args.start_dt.isoformat(),
                "end_dt": args.end_dt.isoformat(),
                "note": (
                    "the kill-gate path uses SPY_EXPENSE_RATIO_SCHEDULE for "
                    "the step; the inspection path's scalar --expense-ratio "
                    "is approximate across 2003-11-01"
                ),
            },
        )
    prices = sharadar.read_sep_prices(
        ticker=args.ticker, start_dt=args.start_dt, end_dt=args.end_dt
    )
    prices_for_tr = prices.select(pl.col("dt"), pl.col("closeunadj").alias("close"))
    dividends = sharadar.read_actions_dividends(
        ticker=args.ticker, start_dt=args.start_dt, end_dt=args.end_dt
    )
    tr = reconstruct_total_return(
        prices_for_tr,
        dividends,
        start_dt=args.start_dt,
        end_dt=args.end_dt,
        expense_ratio_annual=args.expense_ratio,
    )
    ann = annualized_return(tr)
    final_tr = float(tr["tr"][-1])
    log.info(
        "buy_and_hold_complete",
        extra={
            "ticker": args.ticker,
            "n_trading_days": tr.height,
            "final_tr_index": f"{final_tr:.6f}",
            "annualized_pct": f"{ann * 100:.4f}",
        },
    )
    print(
        f"SPY buy-and-hold {args.start_dt}..{args.end_dt}: "
        f"annualized_return = {ann * 100:.4f}% "
        f"(final_tr_index = {final_tr:.6f}, "
        f"n_trading_days = {tr.height}, "
        f"sharadar_bundle = {sharadar_bundle})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
