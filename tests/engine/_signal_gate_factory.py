"""Synthetic fixture for the signal_calendar gate tests (M5 PR 3a).

The perf gate (BarLoop.signal_calendar) skips signal.compute on non-calendar
bars; it is behavior-preserving because the rebalance policy emits empty targets
off its own calendar, so the gated (stale) signal_output is never consumed by an
order. These helpers build the constant-weight SPY/AGG/GLD demo (use_real_pit_view
defaults False, so the gate is exercised without the heavy real-PitView path)
with a COUNTING spy signal so a test can assert (a) byte-identical equity curves
gate-on vs gate-off and (b) signal.compute fires only on calendar bars.

The leading-underscore module name keeps pytest from collecting it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import Universe
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.m1_demo import (
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.base import PitView
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)

_TICKERS = ("SPY", "AGG", "GLD")
_BUNDLE = "sharadar_gate_test"
_START = date(2022, 1, 3)
_DAYS = 120  # ~4 calendar months -> 3 in-window monthly rebalances


class CountingSignal:
    """A deterministic equal-score Signal that counts its compute calls.

    Returns a uniform score for the fixed AssetIds (so the composed
    EqualWeightMonthlyRebalancePolicy holds the constant-weight book), and
    records every (calls, call_dts) so a test can assert the gate fires the
    signal only on calendar bars. Mutability is fine for a test double.
    """

    __test__ = False  # not a pytest test class

    def __init__(self, asset_ids: tuple[AssetId, ...]) -> None:
        self._asset_ids = asset_ids
        self.calls = 0
        self.call_dts: list[date] = []

    def required_lookback_days(self) -> int:
        return 0

    def compute(
        self, universe: Universe, dt: datetime, pit_view: PitView
    ) -> dict[AssetId, float]:
        self.calls += 1
        self.call_dts.append(dt.date() if isinstance(dt, datetime) else dt)
        return {asset_id: 1.0 for asset_id in self._asset_ids}


def gate_asset_ids() -> tuple[AssetId, ...]:
    """The fixed SPY/AGG/GLD AssetIds, sorted (BarLoop sorts tickers anyway)."""
    return tuple(sorted(ticker_to_asset_id(t) for t in _TICKERS))


def write_gate_bundle(tmp_path: Path) -> tuple[Path, date, date]:
    """Write the synthetic SPY/AGG/GLD bundle; return (root, start, end)."""
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(_START, _DAYS, seed=11).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        _START, _DAYS, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=11
    )
    root = _write_bundle(tmp_path, sep_rows, actions_rows, _BUNDLE)
    return root, _START, _START + timedelta(days=_DAYS - 1)


def gate_rebalance_dates(start: date, end: date) -> frozenset[date]:
    """The monthly-last-trading-days inside [start, end].

    monthly_last_trading_day returns a frozenset, and the TestClock caches a
    +/-14 day pad, so filter to the window (the same gotcha as the CPCV
    fixture). The policy rebalances on these and the gate fires the signal on
    these, so spy.calls (gated) == len(this).
    """
    clock = TestClock(start_dt=start, end_dt=end)
    return frozenset(
        d for d in monthly_last_trading_day(clock.trading_days())
        if start <= d <= end
    )


def build_gate_bar_loop(
    snapshots_root: Path,
    start: date,
    end: date,
    *,
    signal: CountingSignal,
    signal_calendar: frozenset[date] | None,
) -> BarLoop:
    """Build the constant-weight BarLoop with the counting signal.

    The policy ALWAYS rebalances on the full in-window calendar; only the
    BarLoop's signal_calendar varies (None = compute every bar; the full
    calendar = gated; a subset = the misuse case that must raise).
    """
    source = SharadarDataSource(_BUNDLE, snapshots_root)
    clock = TestClock(start_dt=start, end_dt=end)
    universe = fixed_universe_from_tickers(_TICKERS)
    asset_ids = gate_asset_ids()
    policy_rebalances = gate_rebalance_dates(start, end)

    price_index: dict[tuple[AssetId, date], float] = {}
    for ticker in _TICKERS:
        frame = source.read_sep_prices(ticker=ticker, start_dt=start, end_dt=end)
        asset_id = ticker_to_asset_id(ticker)
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt: datetime) -> float | None:
        d = dt.date() if isinstance(dt, datetime) else dt
        return price_index.get((asset_id, d))

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=policy_rebalances, price_lookup=price_lookup
    )
    return BarLoop(
        data_source=source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=CloseFillMatchingEngine(clock=clock),
        clock=clock,
        tickers=asset_ids,
        initial_capital=100_000.0,
        signal_calendar=signal_calendar,
    )
