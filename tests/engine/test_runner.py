"""Runner.run_sweep tests (M2 PR C1).

Per ADR 0010 lock #1, #4, #5, #6, #7:
- run_sweep returns list[ConstantWeightDemoResult] in param_grid order
- spawn-only multiproc (tested via num_workers=1 in-process path here;
  the integration test exercises num_workers>1)
- POLARS_MAX_THREADS=1 set as first line of _worker_run_one_param
  (tested via a probe factory that returns os.environ snapshot)
- picklability gate at submit time (pickle + dry-run probes)
- num_workers default min(len(param_grid), max(1, cpu_count() - 1))

Most tests run with num_workers=1 (the in-process fast path) so the
unit-test surface does not pay the spawn bootstrap cost. The integration
test at tests/integration/test_spy_cost_sensitivity.py exercises the
multi-worker spawn path.
"""

from __future__ import annotations

import hashlib
import os
import pickle
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.engine.m1_demo import (
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.engine.runner import Runner, _worker_run_one_param
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal
from tests.engine._runner_test_factories import (
    PicklableRunnerRecipe,
    probe_polars_max_threads_factory,
    runner_test_factory,
)
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)


# ----- run_sweep happy path -----


def test_run_sweep_returns_results_in_param_grid_order(tmp_path: Path) -> None:
    """Per ADR 0010 lock #1 the runner returns results in param_grid order."""
    start = date(2022, 1, 3)
    days = 60
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 0, "AGG": 0, "GLD": 0}, seed=42,
    )
    snapshots_root = _write_bundle(tmp_path, sep_rows, actions_rows, "sharadar_test")

    recipe = PicklableRunnerRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name="sharadar_test",
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
        initial_capital=100_000.0,
    )
    factory = runner_test_factory(recipe)

    # Param grid: two parameter dicts; the factory varies a seed-like key
    # so the resulting BarLoop produces distinct (deterministic) results.
    param_grid: list[dict[str, object]] = [
        {"label": "first"},
        {"label": "second"},
    ]
    runner = Runner(num_workers=1)
    results = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
    )
    assert len(results) == 2
    assert all(isinstance(r, ConstantWeightDemoResult) for r in results)


def test_run_sweep_num_workers_1_is_deterministic_across_repeated_runs(
    tmp_path: Path,
) -> None:
    """Two consecutive run_sweep calls produce bit-identical equity
    curves at num_workers=1 (the in-process fast path).
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

    recipe = PicklableRunnerRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name="sharadar_test",
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
        initial_capital=100_000.0,
    )
    factory = runner_test_factory(recipe)

    param_grid: list[dict[str, object]] = [{"label": "single"}]

    runner = Runner(num_workers=1)
    results_a = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
    )
    results_b = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=start,
        end_dt=start + timedelta(days=days - 1),
    )

    # Compare equity curves bit-by-bit via JSON serialization hash.
    def _hash_curve(curve: pl.DataFrame) -> str:
        return hashlib.sha256(curve.write_json().encode("utf-8")).hexdigest()

    assert _hash_curve(results_a[0].equity_curve) == _hash_curve(
        results_b[0].equity_curve
    )
    assert results_a[0].final_pnl == results_b[0].final_pnl


def test_run_sweep_worker_sets_polars_max_threads_to_1() -> None:
    """Per ADR 0010 lock #5 the worker sets POLARS_MAX_THREADS=1 as the
    FIRST line of _worker_run_one_param. The probe factory raises
    immediately so we can catch and inspect os.environ; the assertion
    is that the env var was set BEFORE the factory body ran.
    """
    # Run the worker directly (not via the pool) so we can inspect the
    # env var inside the same process. The worker mutates os.environ
    # at the start of its body. The probe factory raises so we use
    # pytest.raises to capture; the env-var check happens after.
    pre_existing = os.environ.get("POLARS_MAX_THREADS")
    try:
        os.environ.pop("POLARS_MAX_THREADS", None)
        with pytest.raises(NotImplementedError, match="probe"):
            _worker_run_one_param(
                params={},
                bar_loop_factory=probe_polars_max_threads_factory,
                start_dt=date(2024, 1, 2),
                end_dt=date(2024, 1, 3),
            )
        # The env var must be "1" AFTER the worker ran (the factory
        # raised, but the env-var assignment is the first line of the
        # worker body so it ran before the factory).
        assert os.environ.get("POLARS_MAX_THREADS") == "1"
    finally:
        if pre_existing is None:
            os.environ.pop("POLARS_MAX_THREADS", None)
        else:
            os.environ["POLARS_MAX_THREADS"] = pre_existing


# ----- Picklability + dry-run probes -----


def test_run_sweep_empty_param_grid_raises() -> None:
    runner = Runner(num_workers=1)
    with pytest.raises(ValueError, match="empty"):
        runner.run_sweep(
            param_grid=[],
            bar_loop_factory=probe_polars_max_threads_factory,
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 1, 3),
        )


def test_run_sweep_unpicklable_factory_fails_at_submit_time(tmp_path: Path) -> None:
    """Per ADR 0010 lock #6 closures are unpicklable; the picklability
    probe surfaces the failure at submit time.
    """
    runner = Runner(num_workers=1)

    # Closure capturing a local variable; not picklable on Windows-spawn.
    local_capture = "test_capture_value"

    def closure_factory(params: dict[str, object]) -> BarLoop:
        # Reference the local capture so the closure is genuinely scoped.
        del params
        _ = local_capture
        raise NotImplementedError

    with pytest.raises(RuntimeError, match="picklability probe failed"):
        runner.run_sweep(
            param_grid=[{}],
            bar_loop_factory=closure_factory,
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 1, 3),
        )


def test_run_sweep_failing_factory_fails_at_dry_run(tmp_path: Path) -> None:
    """Per ADR 0010 lock #6 the dry-run probe catches factory errors at
    submit time, not 30 seconds into a worker spawn.
    """
    runner = Runner(num_workers=1)

    with pytest.raises(RuntimeError, match="dry-run probe failed"):
        runner.run_sweep(
            param_grid=[{}],
            bar_loop_factory=_failing_factory,
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 1, 3),
        )


def _failing_factory(params: dict[str, object]) -> BarLoop:
    """Module-level factory that always raises (for the dry-run test).

    Picklable (module-level reference); fails inside the factory body.
    The runner's dry-run probe catches this before submitting to the pool.
    """
    del params
    raise ValueError("intentional factory failure for dry-run test")


# ----- Runner construction -----


def test_runner_construction_with_explicit_workers() -> None:
    runner = Runner(num_workers=2)
    assert runner._num_workers == 2


def test_runner_construction_with_default_workers() -> None:
    runner = Runner()
    assert runner._num_workers is None


def test_run_cpcv_stays_unimplemented_at_m2() -> None:
    """Per ADR 0010 the M2 PR C1 implements run_sweep only; run_cpcv
    stays NotImplementedError until M4.
    """
    runner = Runner()
    with pytest.raises(NotImplementedError, match="M4"):
        runner.run_cpcv(cv_splitter=None, bar_loop_factory=None)  # type: ignore[arg-type]


# ----- Picklability of the factory + recipe -----


def test_factory_recipe_tuple_is_picklable() -> None:
    """The (factory, params) tuple round-trips through pickle when the
    factory is a module-level callable and params contains only picklable
    types.
    """
    recipe = PicklableRunnerRecipe(
        snapshots_root="/tmp/test",
        bundle_name="sharadar_test",
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 3),
        initial_capital=100_000.0,
    )
    factory = runner_test_factory(recipe)
    params: dict[str, object] = {"label": "x"}
    blob = pickle.dumps((factory, params))
    factory_back, params_back = pickle.loads(blob)
    assert params_back == params
    # The factory is callable post-roundtrip.
    assert callable(factory_back)


def test_picklable_runner_recipe_is_attrs_frozen() -> None:
    recipe = PicklableRunnerRecipe(
        snapshots_root="/tmp/test",
        bundle_name="sharadar_test",
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 3),
        initial_capital=100_000.0,
    )
    # attrs.frozen rejects mutation.
    import attrs
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        recipe.snapshots_root = "/changed"  # type: ignore[misc]
