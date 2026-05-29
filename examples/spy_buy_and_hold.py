"""SPY buy-and-hold demo (M1 acceptance criterion 1).

Loads the Sharadar SEP + ACTIONS snapshot, reconstructs the SPY total-return
series with same-day-at-close dividend reinvestment, prints the annualized
return. With --compare-to-ssga, also loads the SSGA SPY snapshot and prints
the reconciliation delta in basis points.

Usage:

    python -m examples.spy_buy_and_hold \\
        --sharadar-bundle sharadar_2026-05-28 \\
        --start-dt 2005-01-03 \\
        --end-dt 2024-12-31 \\
        --log-level INFO

    python -m examples.spy_buy_and_hold \\
        --sharadar-bundle sharadar_2026-05-28 \\
        --ssga-bundle spy_ssga_2026-05-28 \\
        --start-dt 2015-01-02 \\
        --end-dt 2024-12-31 \\
        --ssga-period 10y \\
        --compare-to-ssga

Snapshots are expected under data/snapshots/ at the repo root. Use
--snapshots-root to point elsewhere. If --sharadar-bundle is omitted, the
latest bundle matching `sharadar_*` is used.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pit_backtest.data.adjustments import annualized_return, reconstruct_total_return
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    SPY_EXPENSE_RATIO_POST_2003,
    discover_latest_bundle,
    reconcile_spy,
)
from pit_backtest.utils.logging import configure_logging, get_logger


_DEFAULT_SNAPSHOTS_ROOT = Path(__file__).resolve().parent.parent / "data" / "snapshots"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SPY buy-and-hold demo with optional SSGA reconciliation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sharadar-bundle",
        type=str,
        default=None,
        help="Sharadar snapshot bundle name (e.g., sharadar_2026-05-28). "
        "If omitted, the latest matching bundle is used.",
    )
    parser.add_argument(
        "--ssga-bundle",
        type=str,
        default=None,
        help="SSGA SPY snapshot bundle (e.g., spy_ssga_2026-05-28). "
        "Required with --compare-to-ssga.",
    )
    parser.add_argument(
        "--snapshots-root",
        type=Path,
        default=_DEFAULT_SNAPSHOTS_ROOT,
        help="Directory containing the manifest.toml and bundle subdirectories.",
    )
    parser.add_argument(
        "--start-dt",
        type=date.fromisoformat,
        default=date(2005, 1, 3),
        help="Reconciliation window start (inclusive).",
    )
    parser.add_argument(
        "--end-dt",
        type=date.fromisoformat,
        default=date(2024, 12, 31),
        help="Reconciliation window end (inclusive).",
    )
    parser.add_argument(
        "--expense-ratio",
        type=Decimal,
        default=SPY_EXPENSE_RATIO_POST_2003,
        help="Annual expense ratio applied as drag (default: SPY post-2003-11).",
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
        help="If set, load the SSGA snapshot and print the reconciliation delta.",
    )
    parser.add_argument(
        "--ssga-period",
        type=str,
        default="10y",
        help="SSGA published period label to compare against.",
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

        log.info(
            "loading_ssga_bundle",
            extra={"bundle": ssga_bundle},
        )
        ssga = SSGASpyReference(ssga_bundle, args.snapshots_root)
        report = reconcile_spy(
            sharadar=sharadar,
            ssga=ssga,
            start_dt=args.start_dt,
            end_dt=args.end_dt,
            ssga_period_label=args.ssga_period,
            spy_ticker=args.ticker,
            expense_ratio_annual=args.expense_ratio,
        )
        print(report.render_evidence_line())
        return 0 if report.passes_kill_gate() else 1

    # No SSGA comparison: just print the engine's annualized return.
    prices = sharadar.read_sep_prices(
        ticker=args.ticker, start_dt=args.start_dt, end_dt=args.end_dt
    )
    prices_for_tr = prices.select(prices["dt"], prices["closeunadj"].alias("close"))
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
