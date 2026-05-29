"""Layer 2 1e-10 invariant tests (M2 PR B).

Per ADR 0005 step 13 / ADR 0009 lock #13 the Layer 2 invariant ships as
TWO tests with honest names:

1. test_zero_cost_matcher_path_equals_close_fill_matcher_path_to_1e_minus_10:
   asserts that the M2 zero-cost matcher (SquareRootImpactMatchingEngine +
   NoImpact + zero PerShareCommission + fresh ImpactedPriceSource) produces
   the same per-bar equity curve as the M1 baseline (CloseFillMatchingEngine)
   on the existing synthetic 2-year SPY/AGG/GLD fixture. Catches a refactor
   that breaks dispatch equivalence at zero cost.

2. test_zero_cost_matcher_path_equals_reference_function_to_1e_minus_10:
   asserts that the M2 zero-cost matcher produces the same equity curve
   as the pure-Python reference function (engine/reference.py). Catches a
   cost-model failure class that NoImpact does not exercise plus a
   matcher Decimal round-trip bug.

The fixtures are shared with tests/integration/test_constant_weight_demo.py
via helper imports so both tests exercise the same arithmetic.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource
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
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.commission import PerShareCommission
from pit_backtest.execution.cost.impact import NoImpact
from pit_backtest.execution.matching import (
    CloseFillMatchingEngine,
    SquareRootImpactMatchingEngine,
)
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)


def _build_dual_bar_loops(
    snapshots_root: Path,
    bundle_name: str,
    tickers: tuple[str, ...],
    start_dt: date,
    end_dt: date,
    initial_capital: float,
) -> tuple[BarLoop, BarLoop]:
    """Construct M1-baseline and M2-zero-cost BarLoops reading the same
    Sharadar bundle.

    The two BarLoops share:
    - data_source (fresh per BarLoop because SharadarDataSource caches
      lazy frames; sharing would create cross-talk in the LazyFrame plan
      optimizer)
    - tickers
    - rebalance calendar
    - initial capital

    They differ ONLY in the matching engine. The M1 baseline uses
    CloseFillMatchingEngine; the M2 zero-cost wires
    SquareRootImpactMatchingEngine with NoImpact + zero PerShareCommission
    + ImpactedPriceSource. The expected behavior at zero cost is per-bar
    bit-identical equity curves.
    """
    asset_ids = tuple(sorted(ticker_to_asset_id(t) for t in tickers))
    universe = fixed_universe_from_tickers(tickers)

    # Build M1 baseline.
    data_source_m1 = SharadarDataSource(bundle_name, snapshots_root)
    clock_m1 = TestClock(start_dt=start_dt, end_dt=end_dt)
    rebalance_dates = monthly_last_trading_day(clock_m1.trading_days())
    signal_m1 = EqualWeightSignal(tickers=asset_ids)

    prices_by_asset = {
        ticker_to_asset_id(t): data_source_m1.read_sep_prices(
            ticker=t, start_dt=start_dt, end_dt=end_dt
        )
        for t in tickers
    }
    price_index: dict[tuple[AssetId, date], float] = {}
    for asset_id, frame in prices_by_asset.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt) -> float | None:  # type: ignore[no-untyped-def]
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    policy_m1 = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matcher_m1 = CloseFillMatchingEngine(clock=clock_m1)
    bar_loop_m1 = BarLoop(
        data_source=data_source_m1,
        universe=universe,
        signal=signal_m1,
        policy=policy_m1,
        matching_engine=matcher_m1,
        clock=clock_m1,
        tickers=asset_ids,
        initial_capital=initial_capital,
    )

    # Build M2 zero-cost.
    data_source_m2 = SharadarDataSource(bundle_name, snapshots_root)
    clock_m2 = TestClock(start_dt=start_dt, end_dt=end_dt)
    signal_m2 = EqualWeightSignal(tickers=asset_ids)
    policy_m2 = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    impacted_source = ImpactedPriceSource(raw=data_source_m2)
    no_impact = NoImpact(unsuitable_for_deployment=True)
    zero_commission = PerShareCommission(rate_per_share=Decimal("0"))
    matcher_m2 = SquareRootImpactMatchingEngine(
        clock=clock_m2,
        cost_model=no_impact,  # type: ignore[arg-type]
        commission=zero_commission,
        impacted_source=impacted_source,
    )
    bar_loop_m2 = BarLoop(
        data_source=data_source_m2,
        universe=universe,
        signal=signal_m2,
        policy=policy_m2,
        matching_engine=matcher_m2,
        clock=clock_m2,
        tickers=asset_ids,
        initial_capital=initial_capital,
        impacted_source=impacted_source,
    )

    return bar_loop_m1, bar_loop_m2


def test_zero_cost_matcher_path_equals_close_fill_matcher_path_to_1e_minus_10(
    tmp_path: Path,
) -> None:
    """Layer 2a: matcher-vs-matcher 1e-10 equity-curve match.

    Catches the failure class: a refactor that breaks the
    SquareRootImpactMatchingEngine's dispatch path equivalence to the
    M1 CloseFillMatchingEngine at zero cost (e.g., a new Decimal round-
    trip in the fill_price path that introduces 1 ULP error).
    """
    start = date(2022, 1, 3)
    days = 700
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 4, "AGG": 12, "GLD": 0}, seed=42,
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    initial_capital = 1_000_000.0

    with pytest.warns(UserWarning, match="overstate"):
        bar_loop_m1, bar_loop_m2 = _build_dual_bar_loops(
            snapshots_root=snapshots_root,
            bundle_name="sharadar_test",
            tickers=("SPY", "AGG", "GLD"),
            start_dt=start,
            end_dt=start + timedelta(days=days - 1),
            initial_capital=initial_capital,
        )

    result_m1 = bar_loop_m1.run(start_dt=start, end_dt=start + timedelta(days=days - 1))
    result_m2 = bar_loop_m2.run(start_dt=start, end_dt=start + timedelta(days=days - 1))

    curve_m1 = result_m1.equity_curve
    curve_m2 = result_m2.equity_curve
    assert curve_m1.height == curve_m2.height, (
        f"M1 produced {curve_m1.height} bars; M2 zero-cost produced "
        f"{curve_m2.height}"
    )

    tol_per_bar = 1e-10 * initial_capital
    asset_ids = tuple(ticker_to_asset_id(t) for t in ("SPY", "AGG", "GLD"))
    for i in range(curve_m1.height):
        r_m1 = curve_m1.row(i, named=True)
        r_m2 = curve_m2.row(i, named=True)
        assert r_m1["dt"] == r_m2["dt"]
        assert abs(r_m1["cash"] - r_m2["cash"]) < tol_per_bar, (
            f"cash diverged at bar {i} ({r_m1['dt']}): "
            f"M1={r_m1['cash']}, M2={r_m2['cash']}, "
            f"diff={r_m1['cash'] - r_m2['cash']}"
        )
        assert abs(r_m1["nav"] - r_m2["nav"]) < tol_per_bar, (
            f"nav diverged at bar {i} ({r_m1['dt']}): "
            f"M1={r_m1['nav']}, M2={r_m2['nav']}, "
            f"diff={r_m1['nav'] - r_m2['nav']}"
        )
        for ticker_id in asset_ids:
            col = f"shares_{ticker_id}"
            assert abs(r_m1[col] - r_m2[col]) < 1e-10, (
                f"{col} diverged at bar {i} ({r_m1['dt']}): "
                f"M1={r_m1[col]}, M2={r_m2[col]}"
            )

    assert abs(result_m1.final_pnl - result_m2.final_pnl) < tol_per_bar, (
        f"final_pnl drift {abs(result_m1.final_pnl - result_m2.final_pnl)} "
        f"exceeds 1e-10 tolerance "
        f"(M1={result_m1.final_pnl}, M2={result_m2.final_pnl})"
    )


def test_zero_cost_matcher_path_equals_reference_function_to_1e_minus_10(
    tmp_path: Path,
) -> None:
    """Layer 2b: matcher-vs-reference 1e-10 equity-curve match.

    Catches the failure class: a bug in the cost-model formula that
    NoImpact does not exercise plus a matcher Decimal round-trip bug.
    The reference function is the pure-Python scalar loop from
    engine/reference.py; it is structurally independent of the matcher
    dispatch, so this test catches what 2a does not.
    """
    start = date(2022, 1, 3)
    days = 700
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 4, "AGG": 12, "GLD": 0}, seed=42,
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    initial_capital = 1_000_000.0

    tickers = ("SPY", "AGG", "GLD")
    asset_ids = tuple(sorted(ticker_to_asset_id(t) for t in tickers))

    # Build M2 zero-cost BarLoop.
    with pytest.warns(UserWarning, match="overstate"):
        _bar_loop_m1, bar_loop_m2 = _build_dual_bar_loops(
            snapshots_root=snapshots_root,
            bundle_name="sharadar_test",
            tickers=tickers,
            start_dt=start,
            end_dt=start + timedelta(days=days - 1),
            initial_capital=initial_capital,
        )

    end_dt = start + timedelta(days=days - 1)
    result_m2 = bar_loop_m2.run(start_dt=start, end_dt=end_dt)

    # Build reference inputs.
    data_source_ref = SharadarDataSource("sharadar_test", snapshots_root)
    clock_ref = TestClock(start_dt=start, end_dt=end_dt)
    prices_by_asset = {
        ticker_to_asset_id(t): data_source_ref.read_sep_prices(
            ticker=t, start_dt=start, end_dt=end_dt
        )
        for t in tickers
    }
    dividends_by_asset = {
        ticker_to_asset_id(t): data_source_ref.read_actions_dividends(
            ticker=t, start_dt=start, end_dt=end_dt
        )
        for t in tickers
    }
    rebalance_dates = monthly_last_trading_day(clock_ref.trading_days())
    trading_days_in_window = tuple(
        d for d in clock_ref.trading_days() if start <= d <= end_dt
    )

    reference_rows = reference_constant_weight_pnl(
        prices_by_asset=prices_by_asset,
        dividends_by_asset=dividends_by_asset,
        rebalance_dates=rebalance_dates,
        trading_days=trading_days_in_window,
        tickers=asset_ids,
        initial_capital=initial_capital,
    )
    reference_curve = reference_to_polars(reference_rows)

    # Per-bar comparison
    curve_m2 = result_m2.equity_curve
    assert curve_m2.height == reference_curve.height

    tol_per_bar = 1e-10 * initial_capital
    for i in range(curve_m2.height):
        e = curve_m2.row(i, named=True)
        r = reference_curve.row(i, named=True)
        assert e["dt"] == r["dt"]
        assert abs(e["cash"] - r["cash"]) < tol_per_bar
        assert abs(e["nav"] - r["nav"]) < tol_per_bar
        for ticker_id in asset_ids:
            col = f"shares_{ticker_id}"
            assert abs(e[col] - r[col]) < 1e-10

    final_diff = abs(
        result_m2.final_pnl - (reference_rows[-1].nav - initial_capital)
    )
    assert final_diff < tol_per_bar


def test_no_impact_warning_emitted_at_matcher_construction(tmp_path: Path) -> None:
    """Per ADR 0005 step 7 NoImpact emits a runtime warning at
    construction; the Layer 2 builders catch it explicitly.
    """
    start = date(2022, 1, 3)
    days = 60
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=7).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=7,
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    initial_capital = 100_000.0
    with pytest.warns(UserWarning, match="overstate"):
        _bar_loop_m1, _bar_loop_m2 = _build_dual_bar_loops(
            snapshots_root=snapshots_root,
            bundle_name="sharadar_test",
            tickers=("SPY", "AGG", "GLD"),
            start_dt=start,
            end_dt=start + timedelta(days=days - 1),
            initial_capital=initial_capital,
        )
