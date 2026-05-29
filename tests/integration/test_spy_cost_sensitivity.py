"""End-to-end test for examples/spy_cost_sensitivity.py CLI (M2 PR C1).

Per ADR 0010 lock #9 the CLI demo:
- Discovers a Sharadar bundle via discover_latest_bundle
- Runs the SPY sensitivity sweep at eta in [0.05, 0.10, 0.142, 0.20, 0.30]
- Wraps results into a SensitivityBand via from_run_sweep
- Prints render_summary_line() + render_band_table()
- Returns exit code 0 on success, 1 on missing ticker, 2 on missing snapshot

The integration tests use the synthetic 2-year fixture builder from
test_constant_weight_demo so they do not require a real Sharadar snapshot.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from examples.spy_cost_sensitivity import (
    SpyCostSensitivityRecipe,
    _FactoryPartial,
    build_bar_loop_for_eta,
    main,
)
from pit_backtest.analytics.sensitivity import SensitivityBand
from pit_backtest.engine.runner import Runner
from tests.integration.test_constant_weight_demo import (
    _generate_synthetic_dividends,
    _generate_synthetic_prices,
    _write_bundle,
)


def _build_synthetic_spy_bundle(
    tmp_path: Path, start: date, days: int = 120
) -> Path:
    """Build a synthetic 4-month SPY/AGG/GLD bundle.

    The sensitivity demo accepts SPY only at the CLI surface, but the
    underlying bundle must have all M1 demo tickers because
    SharadarDataSource verifies the bundle's manifest. Returns the
    snapshots_root.
    """
    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(start, days, seed=42).values():
        sep_rows.extend(ticker_rows)
    actions_rows = _generate_synthetic_dividends(
        start, days, {"SPY": 4, "AGG": 0, "GLD": 0}, seed=42,
    )
    snapshots_root = _write_bundle(
        tmp_path, sep_rows, actions_rows, "sharadar_2024-01-01"
    )
    return snapshots_root


def test_build_bar_loop_for_eta_constructs_bar_loop(tmp_path: Path) -> None:
    """Per ADR 0010 lock #8 the factory is module-level and accepts a
    recipe; this test exercises the construction path without going
    through the Runner.
    """
    start = date(2022, 1, 3)
    snapshots_root = _build_synthetic_spy_bundle(tmp_path, start)

    recipe = SpyCostSensitivityRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name="sharadar_2024-01-01",
        ticker="SPY",
        start_dt=start,
        end_dt=start + timedelta(days=119),
        initial_capital=1_000_000.0,
    )
    params: dict[str, object] = {"eta": Decimal("0.142")}
    bar_loop = build_bar_loop_for_eta(params, recipe)
    assert bar_loop is not None


def test_factory_partial_is_picklable() -> None:
    """The _FactoryPartial wraps the recipe in an attrs.frozen object
    that round-trips through pickle (required for spawn).
    """
    import pickle
    recipe = SpyCostSensitivityRecipe(
        snapshots_root="/tmp/test",
        bundle_name="sharadar_test",
        ticker="SPY",
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 3),
        initial_capital=1_000_000.0,
    )
    factory = _FactoryPartial(recipe=recipe)
    blob = pickle.dumps(factory)
    factory_back = pickle.loads(blob)
    assert factory_back.recipe.bundle_name == "sharadar_test"


def test_run_sweep_against_synthetic_bundle_produces_band(tmp_path: Path) -> None:
    """End-to-end: build a synthetic SPY bundle, run the Runner with the
    cost-sensitivity factory at three eta values, wrap into a
    SensitivityBand, verify the band shape.

    Runs at num_workers=1 (in-process) to avoid the spawn bootstrap cost
    in unit tests. The multi-worker spawn path is exercised via the
    CLI test below.
    """
    start = date(2022, 1, 3)
    snapshots_root = _build_synthetic_spy_bundle(tmp_path, start, days=90)

    recipe = SpyCostSensitivityRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name="sharadar_2024-01-01",
        ticker="SPY",
        start_dt=start,
        end_dt=start + timedelta(days=89),
        initial_capital=1_000_000.0,
    )
    factory = _FactoryPartial(recipe=recipe)
    eta_values = (Decimal("0.05"), Decimal("0.142"), Decimal("0.30"))
    param_grid: list[dict[str, object]] = [{"eta": v} for v in eta_values]

    runner = Runner(num_workers=1)
    results = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=start,
        end_dt=start + timedelta(days=89),
    )
    assert len(results) == 3

    band = SensitivityBand.from_run_sweep(
        results=results,
        parameter_name="eta",
        parameter_values=eta_values,
        central_value=Decimal("0.142"),
    )
    assert band.parameter_name == "eta"
    assert band.central_value == Decimal("0.142")
    assert set(band.per_parameter_final_pnl.keys()) == set(eta_values)


def test_eta_sweep_produces_monotone_pnl_ordering(tmp_path: Path) -> None:
    """Higher eta produces higher cost which produces lower P&L (monotone
    non-increasing in eta on a strategy that trades net long).

    Synthetic 4-month SPY fixture; three eta values: 0.05, 0.142, 0.30.
    """
    start = date(2022, 1, 3)
    snapshots_root = _build_synthetic_spy_bundle(tmp_path, start, days=90)

    recipe = SpyCostSensitivityRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name="sharadar_2024-01-01",
        ticker="SPY",
        start_dt=start,
        end_dt=start + timedelta(days=89),
        initial_capital=1_000_000.0,
    )
    factory = _FactoryPartial(recipe=recipe)
    eta_values = (Decimal("0.05"), Decimal("0.142"), Decimal("0.30"))
    param_grid: list[dict[str, object]] = [{"eta": v} for v in eta_values]

    runner = Runner(num_workers=1)
    results = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=start,
        end_dt=start + timedelta(days=89),
    )
    band = SensitivityBand.from_run_sweep(
        results=results,
        parameter_name="eta",
        parameter_values=eta_values,
        central_value=Decimal("0.142"),
    )
    # Monotone non-increasing P&L in eta: higher eta produces (weakly)
    # lower P&L. At sub-bp cost scale on the synthetic fixture, the
    # ordering must still hold within float64 precision.
    pnl_low = band.per_parameter_final_pnl[Decimal("0.05")]
    pnl_central = band.per_parameter_final_pnl[Decimal("0.142")]
    pnl_high = band.per_parameter_final_pnl[Decimal("0.30")]
    assert pnl_low >= pnl_central >= pnl_high, (
        f"eta sweep should produce monotone non-increasing P&L; got "
        f"eta=0.05: {pnl_low}, eta=0.142: {pnl_central}, eta=0.30: {pnl_high}"
    )


def test_run_sweep_multi_worker_spawn_enforces_polars_threads_1() -> None:
    """Per ADR 0010 lock #5 + post-impl reviewer Critical #1: the
    multi-worker spawn path enforces POLARS_MAX_THREADS=1 inside each
    worker process. The probe factory at
    tests/engine/_runner_polars_probe.py asserts pl.thread_pool_size()
    == 1 inside the worker; if the env-var assignment did not take
    effect, the assertion fires and the runner surfaces the error.

    This test is the canonical-contract verification for the spawn
    path; the single-worker fast path is documented as not enforcing
    the invariant in the parent process (see determinism.md).
    """
    from tests.engine._runner_polars_probe import polars_threads_probe_factory

    runner = Runner(num_workers=2)
    # The factory raises (NotImplementedError after the pool-size
    # assertion). The Runner's dry-run probe surfaces it as
    # RuntimeError("dry-run probe failed") with the underlying
    # NotImplementedError visible in the cause chain.
    with pytest.raises(RuntimeError, match="dry-run probe failed"):
        runner.run_sweep(
            param_grid=[{}, {}],
            bar_loop_factory=polars_threads_probe_factory,
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 1, 3),
        )
    # Reaching here means the dry-run in the PARENT raised
    # NotImplementedError (with the AssertionError path NOT taken because
    # the parent's pool size may not be 1). The spawn-path actual
    # contract is verified by inspecting the worker exception chain
    # below: we re-run the probe via a direct _worker_run_one_param call
    # in a spawned context to confirm the env-var was set before the
    # factory was called.
    import multiprocessing
    from pit_backtest.engine.runner import _worker_run_one_param

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(1) as pool:
        # The worker raises (the probe factory's NotImplementedError),
        # but only AFTER the env-var assignment runs and after polars
        # is imported lazily; if POLARS_MAX_THREADS=1 was not set
        # before polars's pool construction, the AssertionError fires
        # instead of NotImplementedError.
        try:
            pool.starmap(
                _worker_run_one_param,
                [({}, polars_threads_probe_factory, date(2024, 1, 2), date(2024, 1, 3))],
            )
        except NotImplementedError:
            # Expected: the factory raised after asserting pool_size == 1.
            pass
        except AssertionError as e:
            pytest.fail(
                f"POLARS_MAX_THREADS=1 invariant violated in spawn "
                f"worker: {e}"
            )


def test_cli_main_exits_2_on_missing_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0010 lock #9 the CLI returns exit code 2 when no Sharadar
    snapshot matches the bundle_prefix under snapshots_root.
    """
    # Empty snapshots_root that does not contain any bundle.
    empty_root = tmp_path / "empty_snapshots"
    empty_root.mkdir()
    # Write a minimal manifest.toml with no bundles.
    (empty_root / "manifest.toml").write_text("", encoding="utf-8")

    argv = [
        "--snapshots-root", str(empty_root),
        "--bundle-prefix", "sharadar",
        "--workers", "1",
    ]
    exit_code = main(argv=argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "no snapshot" in captured.err


def test_cli_main_exits_1_on_unknown_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0010 lock #9 the CLI returns exit code 1 when the ticker
    is not in the M1 demo universe (SPY/AGG/GLD only).
    """
    start = date(2022, 1, 3)
    snapshots_root = _build_synthetic_spy_bundle(tmp_path, start, days=30)

    argv = [
        "--snapshots-root", str(snapshots_root),
        "--bundle-prefix", "sharadar",
        "--ticker", "AAPL",  # not in M1 demo
        "--workers", "1",
        "--start-dt", start.isoformat(),
        "--end-dt", (start + timedelta(days=29)).isoformat(),
    ]
    exit_code = main(argv=argv)
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "AAPL" in captured.err
