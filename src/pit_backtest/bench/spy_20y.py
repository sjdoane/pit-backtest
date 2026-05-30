# Synthetic data per dataset_versioning.md CI gap; real-data perf number
# is captured in the PR description per the local-run gate.
"""SPY 20-year synthetic backtest harness (M2 PR D Phase 0).

Per ADR 0005 step 16 PR D and ADR 0005 final lock #11 the perf budget
runs on synthetic data because the Sharadar Premium snapshot is not
distributable to CI. The synthetic price walk uses the seeded RNG
helpers from `tests/integration/test_constant_weight_demo.py` so the
output is bit-deterministic given the same seed (per
`docs/methodology/determinism.md` Requirement 2).

Per ADR 0012 lock #4 the test statistic is the median of N timed runs
after K discarded warmup runs. The default is N=7, K=1 per ADR 0012
lock #4. A single-sample run is forbidden in `bench/compare.py`.

The script writes a JSON file with the schema:

  {
    "schema_version": 1,
    "median_seconds": float,
    "stdev_seconds": float,
    "min_seconds": float,
    "max_seconds": float,
    "n_runs": int,
    "warmup": int,
    "runner_image_sha": str | null,
    "commit_sha": str | null,
    "measured_at": str (ISO 8601),
    "python_version": str,
    "polars_version": str,
    "numpy_version": str,
    "platform": str
  }

CLI invocation:

  python -m pit_backtest.bench.spy_20y --runs 7 --warmup 1 --output current.json

The script is the producer; `bench/compare.py` is the consumer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl


_SCHEMA_VERSION: int = 1


def _generate_synthetic_prices(
    start: date, days: int, seed: int, n_tickers: int = 1
) -> dict[str, list[dict[str, object]]]:
    """Build a single-ticker synthetic SPY price walk for the bench.

    Mirrors the helper in `tests/integration/test_constant_weight_demo.py`
    but scoped to the SPY-only single-ticker case the bench needs.
    Seed is fixed at the CLI surface so two consecutive runs at the
    same seed produce bit-identical equity curves.
    """
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    price = 400.0
    daily_drift = 0.0003
    daily_vol = 0.012
    for i in range(days):
        d = start + timedelta(days=i)
        log_return = daily_drift + daily_vol * rng.standard_normal()
        price = price * float(np.exp(log_return))
        rows.append(
            {
                "ticker": "SPY",
                "date": d,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "closeunadj": price,
                "volume": 1_000_000,
            }
        )
    return {"SPY": rows}


def _write_synthetic_bundle(
    snapshots_root: Path,
    sep_rows: list[dict[str, object]],
    bundle_name: str = "sharadar_bench",
) -> None:
    """Write the synthetic SEP parquet and manifest entry for the bench."""
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    sep_path = bundle_dir / "sep.parquet"
    pl.DataFrame(sep_rows).write_parquet(sep_path)
    actions_path = bundle_dir / "actions.parquet"
    # Empty actions (no dividends) keeps the bench focused on per-bar dispatch.
    pl.DataFrame(
        {"ticker": ["SPY"], "date": [date(2099, 1, 1)], "action": ["split"], "value": [1.0]}
    ).write_parquet(actions_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.{bundle_name}]
source = "synthetic"
pull_date = 2026-01-01

[snapshots.{bundle_name}.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = {len(sep_rows)} }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_path.stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")


def _build_and_run_bar_loop(snapshots_root: Path, enable_timing: bool) -> float:
    """Build the SPY-only constant-weight BarLoop and run it once.

    Returns wall-clock seconds for the `run()` call. The harness is
    intentionally minimal: no impacted source, no real cost model
    (NoImpact-equivalent via the no-op estimator), no commission.
    The point is to measure per-bar dispatch overhead, not cost-model
    arithmetic (which `tests/execution/cost/test_impact.py` covers).

    Per ADR 0012 lock #7 timing is opt-in; `enable_timing=True` enables
    the per-step accumulator. The wall-clock return value is the
    `run()` call's `time.perf_counter()` delta regardless of
    enable_timing.
    """
    # Imports are inside the function so the harness can be invoked
    # without paying the full M2 import cost when only the JSON schema
    # is needed (e.g., a future schema-only validator).
    from pit_backtest.data.sources.sharadar import SharadarDataSource
    from pit_backtest.engine.bar_loop import BarLoop
    from pit_backtest.engine.calendar import monthly_last_trading_day
    from pit_backtest.engine.m1_demo import (
        fixed_universe_from_tickers,
        ticker_to_asset_id,
    )
    from pit_backtest.execution.clock import TestClock
    from pit_backtest.execution.matching import CloseFillMatchingEngine
    from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
    from pit_backtest.signal.equal_weight import EqualWeightSignal

    data_source = SharadarDataSource("sharadar_bench", snapshots_root)
    start = date(2005, 1, 3)
    end = start + timedelta(days=20 * 365 - 1)
    clock = TestClock(start_dt=start, end_dt=end)
    asset_ids = (ticker_to_asset_id("SPY"),)
    universe = fixed_universe_from_tickers(("SPY",))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    prices_frame = data_source.read_sep_prices(
        ticker="SPY", start_dt=start, end_dt=end
    )
    price_index: dict[tuple[object, ...], float] = {}
    for row in prices_frame.iter_rows(named=True):
        price_index[(ticker_to_asset_id("SPY"), row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: object, dt: object) -> float | None:
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matcher = CloseFillMatchingEngine(clock=clock)
    bar_loop = BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matcher,
        clock=clock,
        tickers=asset_ids,
        initial_capital=1_000_000.0,
        enable_timing=enable_timing,
    )

    perf_start = time.perf_counter()
    bar_loop.run(start_dt=start, end_dt=end)
    return time.perf_counter() - perf_start


def _measure(runs: int, warmup: int, snapshots_root: Path) -> list[float]:
    """Run the BarLoop `warmup + runs` times; return the `runs` timed deltas.

    The warmup runs are discarded (they typically pay JIT / cache /
    page-fault setup cost on cold processes; the timed runs measure the
    steady-state per-run cost).

    `enable_timing` is intentionally False at both call sites: the bench
    measures wall-clock per-run via `time.perf_counter()` and reports
    median+stdev across runs. `BarLoop.timing_breakdown()` is a separate
    debug aid (per ADR 0005 lock #12 / ADR 0012 lock #7) for triaging
    a specific regression AFTER the warning fires; turning it on inside
    the bench would add measurement overhead and contaminate the median.
    A future PR that wants to capture the breakdown alongside wall-clock
    can add a `--enable-timing` CLI flag, but the Phase 0 contract is
    median + stdev only.
    """
    timings: list[float] = []
    for _ in range(warmup):
        _build_and_run_bar_loop(snapshots_root, enable_timing=False)
    for _ in range(runs):
        timings.append(_build_and_run_bar_loop(snapshots_root, enable_timing=False))
    return timings


def _build_record(timings: list[float], runs: int, warmup: int) -> dict[str, object]:
    """Build the JSON record per ADR 0012 lock #1 schema."""
    median = statistics.median(timings)
    stdev = statistics.stdev(timings) if len(timings) >= 2 else 0.0
    return {
        "schema_version": _SCHEMA_VERSION,
        "median_seconds": median,
        "stdev_seconds": stdev,
        "min_seconds": min(timings),
        "max_seconds": max(timings),
        "n_runs": runs,
        "warmup": warmup,
        "runner_image_sha": os.environ.get("RUNNER_IMAGE_SHA"),
        "commit_sha": os.environ.get("GITHUB_SHA"),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "polars_version": pl.__version__,
        "numpy_version": np.__version__,
        "platform": f"{platform.system()}-{platform.machine()}",
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--runs", type=int, default=7, help="number of timed runs (default 7)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="number of warmup runs to discard (default 1)",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="RNG seed for the synthetic price walk"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=20 * 365,
        help="number of calendar days in the synthetic bundle (default 20 years)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="path to write the JSON record (default: stdout)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 1
    if args.warmup < 0:
        print("--warmup must be >= 0", file=sys.stderr)
        return 1
    start = date(2005, 1, 3)
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, args.days, args.seed).values():
        sep_rows.extend(ticker_rows)

    with tempfile.TemporaryDirectory() as tmpdir:
        snapshots_root = Path(tmpdir)
        _write_synthetic_bundle(snapshots_root, sep_rows)
        timings = _measure(args.runs, args.warmup, snapshots_root)

    record = _build_record(timings, args.runs, args.warmup)
    if args.output is not None:
        args.output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    else:
        print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
