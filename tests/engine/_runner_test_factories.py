"""Module-level test factories for Runner.run_sweep tests.

Per ADR 0010 lock #6 the runner's bar_loop_factory must be a module-level
callable so it pickles cleanly under multiprocessing.spawn on Windows.
Closures (including pytest test fixtures) are NOT picklable; this module
provides the module-level factories tests can pass to the runner.

The leading-underscore prefix on the module name signals "test
infrastructure, not a test module" so pytest's collection skips it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

import attrs

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


@attrs.frozen(slots=True)
class PicklableRunnerRecipe:
    """Picklable bundle of fixed inputs the test factories close over.

    Per ADR 0010 lock #8 the recipe carries only picklable types
    (str for paths, primitives for dates and floats).
    """

    snapshots_root: str
    bundle_name: str
    start_dt: date
    end_dt: date
    initial_capital: float


@attrs.frozen(slots=True)
class _RecipeBoundFactory:
    """attrs.frozen wrapper that binds a recipe to the build function.

    Module-level + attrs.frozen + __call__ makes the object picklable
    under spawn on Windows (unlike functools.partial which can have
    quirks with __main__-defined functions).
    """

    recipe: PicklableRunnerRecipe

    def __call__(self, params: dict[str, object]) -> BarLoop:
        return _build_constant_weight_bar_loop(params, self.recipe)


def runner_test_factory(
    recipe: PicklableRunnerRecipe,
) -> Callable[[dict[str, object]], BarLoop]:
    """Build a module-level factory bound to a recipe."""
    return _RecipeBoundFactory(recipe=recipe)


def _build_constant_weight_bar_loop(
    params: dict[str, object],
    recipe: PicklableRunnerRecipe,
) -> BarLoop:
    """Build a SPY/AGG/GLD constant-weight BarLoop for the runner tests.

    The params dict is consumed only via dict access; the test factories
    do not branch on params values. (Params exist to exercise the runner's
    param-grid plumbing, not to vary the BarLoop's behavior.)
    """
    del params
    snapshots_root = Path(recipe.snapshots_root)
    data_source = SharadarDataSource(recipe.bundle_name, snapshots_root)
    clock = TestClock(start_dt=recipe.start_dt, end_dt=recipe.end_dt)
    tickers = ("SPY", "AGG", "GLD")
    asset_ids = tuple(sorted(ticker_to_asset_id(t) for t in tickers))
    universe = fixed_universe_from_tickers(tickers)
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    prices_by_ticker = {
        ticker_to_asset_id(t): data_source.read_sep_prices(
            ticker=t, start_dt=recipe.start_dt, end_dt=recipe.end_dt
        )
        for t in tickers
    }
    price_index: dict[tuple, float] = {}
    for asset_id, frame in prices_by_ticker.items():
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id, dt):  # type: ignore[no-untyped-def]
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
        initial_capital=recipe.initial_capital,
    )


def probe_polars_max_threads_factory(params: dict[str, object]) -> BarLoop:
    """Factory used by test_run_sweep_worker_sets_polars_max_threads_to_1.

    The factory itself raises (we just want to confirm the env var was
    set BEFORE the factory was invoked); the test catches the raise and
    inspects os.environ. The factory is module-level so it pickles cleanly.
    """
    del params
    raise NotImplementedError(
        "probe_polars_max_threads_factory only validates POLARS_MAX_THREADS=1"
    )
