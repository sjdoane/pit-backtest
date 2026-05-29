"""Constant-weight monthly rebalance demo (M1 acceptance criterion 2).

Loads the Sharadar SEP + ACTIONS snapshot, runs the M1 BarLoop on three
equal-weighted names (default SPY, AGG, GLD) with monthly rebalances on
the last NYSE trading day of each calendar month (per ADR 0004), and
prints the final P&L plus a one-line equity-curve summary.

With --diff-against-reference, also runs the pure Python scalar
reference function on the same input frames and prints the engine-vs-
reference final P&L delta. This is the local-runtime version of the
synthetic 2-year integration test (engine == reference to 1e-10).

Usage:

    python -m examples.constant_weight_three_names \\
        --sharadar-bundle sharadar_2026-05-28 \\
        --start-dt 2010-01-04 \\
        --end-dt 2024-12-31 \\
        --initial-capital 1000000 \\
        --log-level INFO

    python -m examples.constant_weight_three_names \\
        --tickers SPY,AGG,GLD \\
        --diff-against-reference

Snapshots are expected under data/snapshots/. Use --snapshots-root to
point elsewhere. If --sharadar-bundle is omitted, the latest bundle
matching `sharadar_*` is used.

Exit codes: 0 on success; 1 on engine-vs-reference divergence > 1e-10;
2 on missing snapshot.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.m1_demo import (
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.engine.reference import reference_constant_weight_pnl
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal
from pit_backtest.utils.logging import configure_logging, get_logger


_DEFAULT_SNAPSHOTS_ROOT = Path(__file__).resolve().parent.parent / "data" / "snapshots"
_RECONCILIATION_TOLERANCE_FRACTION = 1e-10


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Equal-weight monthly rebalance demo for the M1 3-name strategy.",
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
        "--snapshots-root",
        type=Path,
        default=_DEFAULT_SNAPSHOTS_ROOT,
        help="Directory containing manifest.toml and bundle subdirectories.",
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default="SPY,AGG,GLD",
        help="Comma-separated ticker list. Must be in the M1 demo ticker map "
        "(currently SPY, AGG, GLD).",
    )
    parser.add_argument(
        "--start-dt",
        type=date.fromisoformat,
        default=date(2010, 1, 4),
        help="Backtest window start (inclusive). Default 2010-01-04 is "
        "conservatively after GLD inception (2004-11-18).",
    )
    parser.add_argument(
        "--end-dt",
        type=date.fromisoformat,
        default=date(2024, 12, 31),
        help="Backtest window end (inclusive).",
    )
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=1_000_000.0,
        help="Initial portfolio cash in dollars.",
    )
    parser.add_argument(
        "--diff-against-reference",
        action="store_true",
        help="Also run the pure Python scalar reference function and print "
        "the engine-vs-reference final P&L delta. Asserts within 1e-10 of "
        "initial_capital.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    log = get_logger("examples.constant_weight_three_names")

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

    tickers_list = tuple(t.strip() for t in args.tickers.split(",") if t.strip())
    if not tickers_list:
        print("--tickers must contain at least one symbol", file=sys.stderr)
        return 2
    try:
        asset_ids = tuple(sorted(ticker_to_asset_id(t) for t in tickers_list))
    except KeyError as e:
        log.error("unknown_ticker", extra={"reason": str(e)})
        print(
            f"ticker not in M1 demo map: {e}. The M1 demo map covers SPY, "
            f"AGG, GLD; M3 IdentifierResolver replaces this.",
            file=sys.stderr,
        )
        return 2

    log.info(
        "loading_sharadar_bundle",
        extra={"bundle": sharadar_bundle, "tickers": ",".join(tickers_list)},
    )
    sharadar = SharadarDataSource(sharadar_bundle, args.snapshots_root)

    clock = TestClock(start_dt=args.start_dt, end_dt=args.end_dt)
    universe = fixed_universe_from_tickers(tickers_list)
    rebalance_dates = monthly_last_trading_day(clock.trading_days())

    # Build the cached price index that both the Policy and (optionally) the
    # reference function need. Read once via the SharadarDataSource so the
    # eager Polars frame is shared across consumers.
    prices_by_asset = {
        ticker_to_asset_id(t): sharadar.read_sep_prices(
            ticker=t, start_dt=args.start_dt, end_dt=args.end_dt
        )
        for t in tickers_list
    }
    dividends_by_asset = {
        ticker_to_asset_id(t): sharadar.read_actions_dividends(
            ticker=t, start_dt=args.start_dt, end_dt=args.end_dt
        )
        for t in tickers_list
    }
    price_index: dict[tuple, float] = {}
    for asset_id, frame in prices_by_asset.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id, dt) -> float | None:
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    signal = EqualWeightSignal(tickers=asset_ids)
    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matching_engine = CloseFillMatchingEngine(clock=clock)
    bar_loop = BarLoop(
        data_source=sharadar,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matching_engine,
        clock=clock,
        tickers=asset_ids,
        initial_capital=args.initial_capital,
    )

    engine_result = bar_loop.run(start_dt=args.start_dt, end_dt=args.end_dt)
    print(engine_result.render_summary_line())

    if args.diff_against_reference:
        trading_days_in_window = tuple(
            d for d in clock.trading_days() if args.start_dt <= d <= args.end_dt
        )
        reference_rows = reference_constant_weight_pnl(
            prices_by_asset=prices_by_asset,
            dividends_by_asset=dividends_by_asset,
            rebalance_dates=rebalance_dates,
            trading_days=trading_days_in_window,
            tickers=asset_ids,
            initial_capital=args.initial_capital,
        )
        ref_final_pnl = reference_rows[-1].nav - args.initial_capital
        diff = engine_result.final_pnl - ref_final_pnl
        tolerance = _RECONCILIATION_TOLERANCE_FRACTION * args.initial_capital
        verdict = "PASS" if abs(diff) < tolerance else "FAIL"
        log.info(
            "engine_vs_reference",
            extra={
                "engine_pnl": f"{engine_result.final_pnl:+,.6f}",
                "reference_pnl": f"{ref_final_pnl:+,.6f}",
                "diff": f"{diff:+,.10f}",
                "tolerance": f"{tolerance:.10f}",
                "verdict": verdict,
            },
        )
        print(
            f"engine_vs_reference: {verdict} "
            f"(diff = ${diff:+.10f}, tolerance = ${tolerance:.10f})"
        )
        return 0 if abs(diff) < tolerance else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
