"""Cost-estimator wiring assertion (M2 PR B; ADR 0003 decision 4).

Per ADR 0009 lock #5 the BarLoop's ctor now accepts a `cost_estimator`
keyword that replaces the _NoopCostEstimator stand-in. The
EqualWeightMonthlyRebalancePolicy does NOT consult the cost_estimator
in its current implementation (the v1 equal-weight policy ignores
costs), so this test cannot assert observable behavior. Instead it
asserts the wiring is STRUCTURAL: a SquareRootImpactCostModel passed
through the BarLoop is retained as the BarLoop's cost_estimator (per
ADR 0003 decision 4, the policy receives the same object).

Future M3 policies that consult `cost_estimator.estimate(...)` will
exercise the wiring observably; the assertion here prevents the wiring
from silently regressing to _NoopCostEstimator before M3 ships.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.engine.bar_loop import BarLoop, _NoopCostEstimator
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.m1_demo import (
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.impact import (
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)


def test_bar_loop_uses_noop_cost_estimator_by_default(tmp_path: Path) -> None:
    """When cost_estimator is omitted, the BarLoop uses _NoopCostEstimator
    (preserving M1 behavior).
    """
    start = date(2022, 1, 3)
    days = 30
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=1).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=1
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    data_source = SharadarDataSource("sharadar_test", snapshots_root)
    clock = TestClock(start_dt=start, end_dt=start + timedelta(days=days - 1))
    asset_ids = (ticker_to_asset_id("SPY"), ticker_to_asset_id("AGG"), ticker_to_asset_id("GLD"))
    universe = fixed_universe_from_tickers(("SPY", "AGG", "GLD"))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    def price_lookup(asset_id: AssetId, dt) -> float | None:  # type: ignore[no-untyped-def]
        return None

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
        initial_capital=100_000.0,
    )
    # Internal state: cost_estimator is the no-op.
    assert isinstance(bar_loop._cost_estimator, _NoopCostEstimator)


def test_bar_loop_wires_explicit_cost_estimator_to_policy(tmp_path: Path) -> None:
    """When cost_estimator is provided, the BarLoop uses it directly
    (not wrapped, not replaced).
    """
    start = date(2022, 1, 3)
    days = 30
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=2).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=2
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    data_source = SharadarDataSource("sharadar_test", snapshots_root)
    clock = TestClock(start_dt=start, end_dt=start + timedelta(days=days - 1))
    asset_ids = (ticker_to_asset_id("SPY"),)
    universe = fixed_universe_from_tickers(("SPY",))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    def price_lookup(asset_id: AssetId, dt) -> float | None:  # type: ignore[no-untyped-def]
        return None

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matcher = CloseFillMatchingEngine(clock=clock)

    # Build a real SquareRootImpactCostModel as the cost estimator.
    lookup = MarketStateLookup(
        by_key={
            (ticker_to_asset_id("SPY"), date(2022, 1, 4)): MarketStateRow(
                sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
            )
        }
    )
    cost_model = SquareRootImpactCostModel(market_state=lookup)

    bar_loop = BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matcher,
        clock=clock,
        tickers=asset_ids,
        initial_capital=100_000.0,
        cost_estimator=cost_model,
    )
    # Wiring assertion: the BarLoop stored the real cost model, not
    # the no-op stand-in.
    #
    # ADR 0011 lock #5: this identity assertion is LOAD-BEARING for the
    # tolerance contract dormancy. The fact that policy and matcher
    # share the SAME cost-model instance is precisely why the tolerance
    # check cannot fire at M2 (both estimate() and compute() calls
    # resolve to the same MarketStateLookup row and produce bit-identical
    # outputs). A future contributor at M3 who wants to ACTIVATE the
    # tolerance contract must introduce distinct policy-time vs
    # matcher-time MarketStateLookup snapshots; that activation
    # supersedes this assertion. Do NOT delete this line as a "cleanup"
    # without superseding ADR 0011.
    assert bar_loop._cost_estimator is cost_model
    assert not isinstance(bar_loop._cost_estimator, _NoopCostEstimator)


def test_cost_estimator_estimate_returns_non_zero_for_sized_order() -> None:
    """Standalone verification that the wired estimator produces a
    non-zero estimate for a SPY-shaped order. Locks the contract that
    the policy COULD observe a non-zero cost (the M2 EqualWeight policy
    ignores cost; M3 policies will not).
    """
    lookup = MarketStateLookup(
        by_key={
            (AssetId(1), date(2024, 4, 30)): MarketStateRow(
                sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
            )
        }
    )
    cost_model = SquareRootImpactCostModel(market_state=lookup)

    from datetime import datetime
    estimate = cost_model.estimate(
        asset_id=AssetId(1),
        shares=Decimal("2000"),
        direction="buy",
        dt=datetime(2024, 4, 30, 16, 0),
    )
    assert estimate > Decimal("0"), (
        f"SPY $1M monthly Almgren estimate should be > 0; got {estimate}"
    )
