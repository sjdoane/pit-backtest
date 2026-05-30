"""BarLoop.timing_breakdown() opt-in instrumentation tests (M2 PR D).

Per ADR 0005 lock #12 and ADR 0012 lock #7 the BarLoop ctor accepts
`enable_timing: bool = False` and `timing_breakdown()` returns a
sorted `list[tuple[str, float]]` of step name + accumulated seconds.
Default-off paths leave the accumulator empty and the method returns
an empty list.

Per ADR 0012 lock #7 timing values are explicitly OUT of the
determinism invariant; tests assert ORDERING and STRUCTURAL
properties of the breakdown, not bit-identical values.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from pit_backtest.data.records import AssetId
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
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)


def _build_bar_loop(tmp_path: Path, enable_timing: bool) -> BarLoop:
    start = date(2024, 1, 2)
    days = 60
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=42
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    data_source = SharadarDataSource("sharadar_test", snapshots_root)
    clock = TestClock(start_dt=start, end_dt=start + timedelta(days=days - 1))
    asset_ids = (
        ticker_to_asset_id("SPY"),
        ticker_to_asset_id("AGG"),
        ticker_to_asset_id("GLD"),
    )
    universe = fixed_universe_from_tickers(("SPY", "AGG", "GLD"))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    prices_by_ticker = {
        ticker_to_asset_id(t): data_source.read_sep_prices(
            ticker=t, start_dt=start, end_dt=start + timedelta(days=days - 1)
        )
        for t in ("SPY", "AGG", "GLD")
    }
    price_index: dict[tuple, float] = {}
    for asset_id, frame in prices_by_ticker.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt) -> float | None:  # type: ignore[no-untyped-def]
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    matcher = CloseFillMatchingEngine(clock=clock)

    return BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matcher,
        clock=clock,
        tickers=asset_ids,
        initial_capital=100_000.0,
        enable_timing=enable_timing,
    )


def test_timing_breakdown_empty_when_default_off(tmp_path: Path) -> None:
    """Default-off (no `enable_timing=True`) returns an empty list."""
    bar_loop = _build_bar_loop(tmp_path, enable_timing=False)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    assert bar_loop.timing_breakdown() == []


def test_timing_breakdown_populates_when_enabled(tmp_path: Path) -> None:
    """When `enable_timing=True` the per-step accumulator populates."""
    bar_loop = _build_bar_loop(tmp_path, enable_timing=True)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    breakdown = bar_loop.timing_breakdown()
    assert len(breakdown) > 0
    # Every entry is (str, float) and the float is non-negative.
    for name, seconds in breakdown:
        assert isinstance(name, str)
        assert isinstance(seconds, float)
        assert seconds >= 0.0


def test_timing_breakdown_sorted_by_step_name(tmp_path: Path) -> None:
    """Per ADR 0012 lock #7 the return is sorted by step name."""
    bar_loop = _build_bar_loop(tmp_path, enable_timing=True)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    breakdown = bar_loop.timing_breakdown()
    names = [name for name, _ in breakdown]
    assert names == sorted(names)


def test_timing_breakdown_contains_expected_step_buckets(tmp_path: Path) -> None:
    """Per the BarLoop docstring the v1 buckets are
    preload/signal/policy/matcher/snapshot. With a 60-day window the
    constant-weight monthly rebalance produces at least one rebalance,
    so all five buckets accumulate non-zero time.
    """
    bar_loop = _build_bar_loop(tmp_path, enable_timing=True)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    breakdown = dict(bar_loop.timing_breakdown())
    # Preload + signal + policy + snapshot always accumulate; matcher
    # accumulates only on bars with fills (at least one rebalance bar
    # in 60 days).
    assert "preload" in breakdown
    assert "signal" in breakdown
    assert "policy" in breakdown
    assert "snapshot" in breakdown
    assert "matcher" in breakdown


def test_timing_does_not_affect_equity_curve(tmp_path: Path) -> None:
    """Per ADR 0012 lock #7 timing values are OUT of the determinism
    invariant, but the equity curve IS in it. Enabling timing must
    not change the equity curve bit-by-bit.
    """
    start = date(2024, 1, 2)
    days = 60

    bar_loop_off = _build_bar_loop(tmp_path / "off", enable_timing=False)
    result_off = bar_loop_off.run(start_dt=start, end_dt=start + timedelta(days=days - 1))

    bar_loop_on = _build_bar_loop(tmp_path / "on", enable_timing=True)
    result_on = bar_loop_on.run(start_dt=start, end_dt=start + timedelta(days=days - 1))

    # Equity curves must match bit-by-bit; timing instrumentation is a
    # measurement, not a participant in the computation.
    assert result_off.final_pnl == result_on.final_pnl
    assert result_off.final_nav == result_on.final_nav
    assert result_off.equity_curve.equals(result_on.equity_curve)


def _build_m2_matcher_bar_loop(tmp_path: Path, enable_timing: bool) -> BarLoop:
    """Build a BarLoop wired with the M2 SquareRootImpactMatchingEngine.

    Per post-impl reviewer Finding 10 the timing-breakdown coverage on
    CloseFillMatchingEngine alone leaves a gap: the M2 matcher does the
    actual Almgren + commission + impacted-source work, and a regression
    there would show different bucket distribution than the M1 path.
    This builder uses the M2 matcher so the timing test exercises the
    real bucket the bench/examples actually invoke.
    """
    from decimal import Decimal

    from pit_backtest.data.sources.base import ImpactedPriceSource
    from pit_backtest.execution.cost.commission import PerShareCommission
    from pit_backtest.execution.cost.impact import (
        MarketStateLookup,
        MarketStateRow,
        SquareRootImpactCostModel,
    )
    from pit_backtest.execution.matching import SquareRootImpactMatchingEngine

    start = date(2024, 1, 2)
    days = 60
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=42
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    data_source = SharadarDataSource("sharadar_test", snapshots_root)
    clock = TestClock(start_dt=start, end_dt=start + timedelta(days=days - 1))
    asset_ids = (
        ticker_to_asset_id("SPY"),
        ticker_to_asset_id("AGG"),
        ticker_to_asset_id("GLD"),
    )
    universe = fixed_universe_from_tickers(("SPY", "AGG", "GLD"))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    prices_by_ticker = {
        ticker_to_asset_id(t): data_source.read_sep_prices(
            ticker=t, start_dt=start, end_dt=start + timedelta(days=days - 1)
        )
        for t in ("SPY", "AGG", "GLD")
    }
    price_index: dict[tuple, float] = {}
    market_state_by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for asset_id, frame in prices_by_ticker.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])
            market_state_by_key[(asset_id, row["dt"])] = MarketStateRow(
                sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
            )

    def price_lookup(asset_id: AssetId, dt) -> float | None:  # type: ignore[no-untyped-def]
        d = dt.date() if hasattr(dt, "date") else dt
        return price_index.get((asset_id, d))

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    impacted_source = ImpactedPriceSource(raw=data_source)
    cost_model = SquareRootImpactCostModel(
        market_state=MarketStateLookup(by_key=market_state_by_key)
    )
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    matcher = SquareRootImpactMatchingEngine(
        clock=clock,
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted_source,
    )

    return BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matcher,
        clock=clock,
        tickers=asset_ids,
        initial_capital=100_000.0,
        impacted_source=impacted_source,
        cost_estimator=cost_model,
        enable_timing=enable_timing,
    )


def test_timing_breakdown_covers_square_root_matcher_path(tmp_path: Path) -> None:
    """Per post-impl reviewer Finding 10 the timing buckets are also
    populated when the M2 matcher (SquareRootImpactMatchingEngine) is
    wired. The matcher bucket should be NON-TRIVIAL because the matcher
    runs Almgren + commission + impacted-source updates per fill.
    """
    bar_loop = _build_m2_matcher_bar_loop(tmp_path, enable_timing=True)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    breakdown = dict(bar_loop.timing_breakdown())
    assert "preload" in breakdown
    assert "signal" in breakdown
    assert "policy" in breakdown
    assert "snapshot" in breakdown
    assert "matcher" in breakdown
    # The matcher bucket is non-trivial: at least one rebalance produced
    # a fill that ran the Almgren formula. This is a structural assertion
    # (positive seconds), not a magnitude assertion (timing values are
    # OUT of the determinism invariant per ADR 0012 lock #7).
    assert breakdown["matcher"] > 0.0


def test_timing_breakdown_returns_new_list_each_call(tmp_path: Path) -> None:
    """The method returns a fresh list (defensive copy) so callers
    cannot mutate the accumulator dict from the outside.
    """
    bar_loop = _build_bar_loop(tmp_path, enable_timing=True)
    start = date(2024, 1, 2)
    bar_loop.run(start_dt=start, end_dt=start + timedelta(days=59))
    breakdown_1 = bar_loop.timing_breakdown()
    breakdown_2 = bar_loop.timing_breakdown()
    assert breakdown_1 == breakdown_2
    # Mutating one does not affect the other (defensive: list[tuple] is
    # not deeply mutable but the outer list is).
    breakdown_1.append(("test_marker", 99.0))
    assert ("test_marker", 99.0) not in breakdown_2
