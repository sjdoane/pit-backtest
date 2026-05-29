"""Constant-weight monthly rebalance demo: engine vs reference, 1e-10.

ADR 0002 acceptance criterion 2:
> "A constant-weight monthly rebalance on three names (SPY, AGG, GLD with
> equal target weights) produces final P&L within floating-point precision
> (1e-10) of a spreadsheet hand calculation."

Per the M1-day-3 skeptical-reviewer pass, the "spreadsheet" is a pure
Python scalar reference function in `engine/reference.py` that performs
EXACTLY the same float operations in EXACTLY the same order as the M1
BarLoop. The test asserts engine and reference produce identical equity
curves to 1e-10 over the same Polars input frames.

Two modes:
1. Synthetic-fixture (CI-enabled): builds a Sharadar-shaped 3-name 2-year
   fixture under tmp_path with seeded RNG, runs both pipelines, asserts
   equality to 1e-10.
2. Real-snapshot (CI-skipped): runs against data/snapshots/sharadar_<YYYY-MM-DD>/,
   real SPY/AGG/GLD over 2005-2024. Marked @pytest.mark.snapshot.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.m1_demo import (
    asset_id_to_ticker,
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.engine.reference import (
    reference_constant_weight_pnl,
    reference_to_polars,
)
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"


# ----- Synthetic fixture builders -----


def _generate_synthetic_prices(
    start: date,
    days: int,
    seed: int,
    n_tickers: int = 3,
) -> dict[str, list[dict[str, object]]]:
    """Build a 3-ticker price walk with seeded RNG.

    Each ticker follows a geometric Brownian walk with a small daily drift
    and noise. No dividends; dividends are added separately. Prices are
    set on consecutive calendar days; the test only iterates the dates
    that fall in the TestClock's trading-day cache (NYSE).
    """
    rng = np.random.default_rng(seed)
    ticker_names = ["SPY", "AGG", "GLD"][:n_tickers]
    start_prices = [400.0, 100.0, 180.0][:n_tickers]
    daily_drift = [0.0003, 0.0001, 0.0002][:n_tickers]
    daily_vol = [0.012, 0.004, 0.010][:n_tickers]

    rows_by_ticker: dict[str, list[dict[str, object]]] = {
        t: [] for t in ticker_names
    }
    prices = list(start_prices)
    for i in range(days):
        d = start + timedelta(days=i)
        for j, ticker in enumerate(ticker_names):
            log_return = daily_drift[j] + daily_vol[j] * rng.standard_normal()
            prices[j] = prices[j] * np.exp(log_return)
            rows_by_ticker[ticker].append(
                {
                    "ticker": ticker,
                    "date": d,
                    "open": prices[j],
                    "high": prices[j] * 1.001,
                    "low": prices[j] * 0.999,
                    "close": prices[j],
                    "closeunadj": prices[j],
                    "volume": 1_000_000,
                }
            )
    return rows_by_ticker


def _generate_synthetic_dividends(
    start: date,
    days: int,
    n_per_year_by_ticker: dict[str, int],
    seed: int,
) -> list[dict[str, object]]:
    """Build a list of dividend ACTIONS rows. Equal-spaced ex-dates per
    ticker, fixed per-share amount.
    """
    rng = np.random.default_rng(seed + 7)
    rows: list[dict[str, object]] = []
    for ticker, n_per_year in n_per_year_by_ticker.items():
        if n_per_year == 0:
            continue
        interval = max(1, int(252 / n_per_year))
        amount = float(rng.uniform(0.5, 2.0))
        i = interval
        while i < days:
            rows.append(
                {
                    "ticker": ticker,
                    "date": start + timedelta(days=i),
                    "action": "dividend",
                    "value": amount,
                }
            )
            i += interval
    # Add a sentinel non-dividend row so the parquet has at least one
    # non-empty action even if no dividends fall in the window.
    rows.append(
        {
            "ticker": "SPY",
            "date": start + timedelta(days=days + 1000),
            "action": "split",
            "value": 1.0,
        }
    )
    return rows


def _write_bundle(
    tmp_path: Path,
    sep_rows: list[dict[str, object]],
    actions_rows: list[dict[str, object]],
    bundle_name: str = "sharadar_test",
) -> Path:
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    sep_path = bundle_dir / "sep.parquet"
    pl.DataFrame(sep_rows).write_parquet(sep_path)
    actions_path = bundle_dir / "actions.parquet"
    pl.DataFrame(actions_rows).write_parquet(actions_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()

    manifest = f"""
[snapshots.{bundle_name}]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.{bundle_name}.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = {len(sep_rows)} }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_path.stat().st_size}, row_count = {len(actions_rows)} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    return snapshots_root


def _build_constant_weight_setup(
    snapshots_root: Path,
    bundle_name: str,
    tickers: tuple[str, ...],
    start_dt: date,
    end_dt: date,
    initial_capital: float,
) -> tuple[BarLoop, dict[AssetId, pl.DataFrame], dict[AssetId, pl.DataFrame], frozenset[date], tuple[date, ...]]:
    """Construct the BarLoop and return the inputs the reference function needs."""
    data_source = SharadarDataSource(bundle_name, snapshots_root)
    clock = TestClock(start_dt=start_dt, end_dt=end_dt)
    asset_ids = tuple(sorted(ticker_to_asset_id(t) for t in tickers))
    universe = fixed_universe_from_tickers(tickers)

    # Rebalance calendar per ADR 0004: monthly last trading day, computed
    # once and shared between Policy and reference function.
    rebalance_dates = monthly_last_trading_day(clock.trading_days())

    signal = EqualWeightSignal(tickers=asset_ids)
    # Per-Policy price_lookup closure reads from the cached SharadarDataSource.
    prices_by_asset = {
        ticker_to_asset_id(t): data_source.read_sep_prices(
            ticker=t, start_dt=start_dt, end_dt=end_dt
        )
        for t in tickers
    }
    dividends_by_asset = {
        ticker_to_asset_id(t): data_source.read_actions_dividends(
            ticker=t, start_dt=start_dt, end_dt=end_dt
        )
        for t in tickers
    }
    # Build the same (ticker, dt) -> closeunadj index the BarLoop uses.
    price_index: dict[tuple[AssetId, date], float] = {}
    for asset_id, frame in prices_by_asset.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt) -> float | None:
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matching_engine = CloseFillMatchingEngine(clock=clock)
    bar_loop = BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matching_engine,
        clock=clock,
        tickers=asset_ids,
        initial_capital=initial_capital,
    )

    trading_days_in_window = tuple(
        d for d in clock.trading_days() if start_dt <= d <= end_dt
    )
    return (
        bar_loop,
        prices_by_asset,
        dividends_by_asset,
        rebalance_dates,
        trading_days_in_window,
    )


# ----- Synthetic mode -----


def test_engine_matches_reference_synthetic_2year_3name(tmp_path: Path) -> None:
    """The headline M1 acceptance test in synthetic mode.

    Builds a 2-year, 3-ticker, seeded-RNG synthetic fixture. Runs engine
    and reference. Asserts equity curves match to 1e-10 per bar and
    final_pnl matches to 1e-10 absolute.
    """
    start = date(2022, 1, 3)
    days = 700  # ~2 calendar years
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start,
        days,
        {"SPY": 4, "AGG": 12, "GLD": 0},
        seed=42,
    )

    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    initial_capital = 1_000_000.0
    bar_loop, prices_by_asset, dividends_by_asset, rebalance_dates, trading_days_in_window = (
        _build_constant_weight_setup(
            snapshots_root=snapshots_root,
            bundle_name="sharadar_test",
            tickers=("SPY", "AGG", "GLD"),
            start_dt=start,
            end_dt=start + timedelta(days=days - 1),
            initial_capital=initial_capital,
        )
    )

    # Run engine
    engine_result = bar_loop.run(
        start_dt=start, end_dt=start + timedelta(days=days - 1)
    )

    # Run reference on the SAME inputs
    asset_ids = tuple(sorted(prices_by_asset.keys()))
    reference_rows = reference_constant_weight_pnl(
        prices_by_asset=prices_by_asset,
        dividends_by_asset=dividends_by_asset,
        rebalance_dates=rebalance_dates,
        trading_days=trading_days_in_window,
        tickers=asset_ids,
        initial_capital=initial_capital,
    )
    reference_curve = reference_to_polars(reference_rows)

    # Per-bar equity-curve comparison
    engine_curve = engine_result.equity_curve
    assert engine_curve.height == reference_curve.height, (
        f"engine produced {engine_curve.height} bars; reference produced "
        f"{reference_curve.height}"
    )

    tol_per_bar = 1e-10 * initial_capital  # ~1e-4 dollars at $1M notional
    for i in range(engine_curve.height):
        e = engine_curve.row(i, named=True)
        r = reference_curve.row(i, named=True)
        assert e["dt"] == r["dt"]
        assert abs(e["cash"] - r["cash"]) < tol_per_bar, (
            f"cash diverged at bar {i} ({e['dt']}): "
            f"engine={e['cash']}, reference={r['cash']}, "
            f"diff={e['cash'] - r['cash']}"
        )
        assert abs(e["nav"] - r["nav"]) < tol_per_bar, (
            f"nav diverged at bar {i} ({e['dt']}): "
            f"engine={e['nav']}, reference={r['nav']}, "
            f"diff={e['nav'] - r['nav']}"
        )
        for ticker_id in asset_ids:
            col = f"shares_{ticker_id}"
            assert abs(e[col] - r[col]) < 1e-10, (
                f"{col} diverged at bar {i} ({e['dt']}): "
                f"engine={e[col]}, reference={r[col]}"
            )

    # Final P&L: per ADR 0002 criterion 2, within 1e-10 of the reference.
    final_diff = abs(engine_result.final_pnl - (reference_rows[-1].nav - initial_capital))
    assert final_diff < 1e-10 * initial_capital, (
        f"final_pnl drift {final_diff} exceeds 1e-10 tolerance "
        f"(engine={engine_result.final_pnl}, "
        f"reference={reference_rows[-1].nav - initial_capital})"
    )

    # Sanity: at least 20 rebalances over 2 years.
    assert engine_result.n_rebalances >= 20, (
        f"expected ~24 rebalances over 2 years; got {engine_result.n_rebalances}"
    )


def test_engine_handles_first_bar_as_cash_only_per_adr_0004(tmp_path: Path) -> None:
    """ADR 0004 invariant: start_dt is NOT forced as a rebalance date.

    Start the backtest on a known mid-month date; assert the first bar
    shows nav == initial_capital (no rebalance) and shares are all zero.
    The first rebalance happens on the first scheduled date >= start_dt.
    """
    start = date(2023, 1, 3)  # Tuesday, mid-month
    days = 60  # cover at least one month-end
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=7).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=7
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    initial_capital = 100_000.0
    bar_loop, *_ = _build_constant_weight_setup(
        snapshots_root=snapshots_root,
        bundle_name="sharadar_test",
        tickers=("SPY", "AGG", "GLD"),
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
        initial_capital=initial_capital,
    )
    result = bar_loop.run(start_dt=start, end_dt=start + timedelta(days=days - 1))

    first_bar = result.equity_curve.row(0, named=True)
    assert first_bar["cash"] == pytest.approx(initial_capital, abs=1e-9)
    assert first_bar["nav"] == pytest.approx(initial_capital, abs=1e-9)
    # All shares zero on first bar (no rebalance executed yet).
    for ticker_id in (AssetId(0), AssetId(1), AssetId(2)):
        assert first_bar[f"shares_{ticker_id}"] == pytest.approx(0.0, abs=1e-12)


# ----- Real-snapshot mode -----


@pytest.mark.snapshot
def test_engine_matches_reference_real_spy_agg_gld_2005_2024() -> None:
    """The headline test against real Sharadar data.

    Gated on snapshot availability; skipped in CI per
    docs/methodology/dataset_versioning.md.
    """
    sharadar_bundle = discover_latest_bundle(_SNAPSHOTS_ROOT, "sharadar")
    if sharadar_bundle is None:
        pytest.skip(
            "no sharadar snapshot in data/snapshots/; pull per "
            "docs/methodology/dataset_versioning.md to run this test"
        )

    start = date(2005, 1, 4)
    end = date(2024, 12, 31)
    initial_capital = 1_000_000.0

    bar_loop, prices_by_asset, dividends_by_asset, rebalance_dates, trading_days_in_window = (
        _build_constant_weight_setup(
            snapshots_root=_SNAPSHOTS_ROOT,
            bundle_name=sharadar_bundle,
            tickers=("SPY", "AGG", "GLD"),
            start_dt=start,
            end_dt=end,
            initial_capital=initial_capital,
        )
    )

    engine_result = bar_loop.run(start_dt=start, end_dt=end)
    asset_ids = tuple(sorted(prices_by_asset.keys()))
    reference_rows = reference_constant_weight_pnl(
        prices_by_asset=prices_by_asset,
        dividends_by_asset=dividends_by_asset,
        rebalance_dates=rebalance_dates,
        trading_days=trading_days_in_window,
        tickers=asset_ids,
        initial_capital=initial_capital,
    )

    final_pnl_engine = engine_result.final_pnl
    final_pnl_reference = reference_rows[-1].nav - initial_capital
    diff = abs(final_pnl_engine - final_pnl_reference)
    print(
        f"constant_weight 2005-2024 SPY/AGG/GLD: "
        f"engine_pnl=${final_pnl_engine:+,.6f}, "
        f"reference_pnl=${final_pnl_reference:+,.6f}, "
        f"diff=${diff:.10f}, "
        f"tolerance={1e-10 * initial_capital:.10f}"
    )
    assert diff < 1e-10 * initial_capital, (
        f"engine vs reference final_pnl divergence ${diff} exceeds 1e-10 "
        f"of initial_capital (${1e-10 * initial_capital})"
    )
