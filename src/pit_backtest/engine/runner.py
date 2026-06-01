"""Runner: multiprocess CPCV path and parameter-sweep orchestrator.

Per ADR 0003 architecture: parallelism lives in the Runner, not the
BarLoop. Each worker is an independent process with a read-only data view;
no shared state. Per docs/methodology/determinism.md Requirement 5: every
worker sets POLARS_MAX_THREADS=1 before importing Polars to keep aggregation
order deterministic.

Per ADR 0010 (M2 PR C1) lock #1 the run_sweep API returns a raw
`list[ConstantWeightDemoResult]` in param_grid order; analytics-layer
wrapping into `SensitivityBand` happens at
`analytics.sensitivity.SensitivityBand.from_run_sweep`.

Per ADR 0010 lock #4 and lock #5 the multiproc context is spawn on both
Linux and Windows, and `_worker_run_one_param` sets POLARS_MAX_THREADS=1
as the first line of its body (no module imports above it; no Polars
import in the worker bootstrap path).

Per ADR 0010 lock #6 the picklability gate at submit time runs BOTH
`pickle.dumps((bar_loop_factory, param_grid[0]))` AND a synchronous
dry-run of `bar_loop_factory(param_grid[0])` in the parent process
BEFORE submitting to the pool. The dry-run catches non-picklable
closure capture and module-level side-effect ordering surprises that
the pickle probe alone would miss.

Per ADR 0010 lock #7 `num_workers` default is
`min(len(param_grid), max(1, cpu_count() - 1))` reserving one core
for the parent process.
"""

from __future__ import annotations

import math
import multiprocessing
import pickle
from datetime import date
from typing import TYPE_CHECKING, Callable

from pit_backtest.analytics.distribution import BacktestPathDistribution
from pit_backtest.analytics.result_adapter import to_backtest_result
from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.cv import CPCVSplitter, Split, contiguous_folds
from pit_backtest.validation.trial_registry import TrialRegistry

if TYPE_CHECKING:
    # Polars must NOT be imported at runtime module top: a spawn-bootstrapped
    # worker sets POLARS_MAX_THREADS=1 as the first line of _worker_run_one_param
    # before any Polars import, and a module-top `import polars` would defeat
    # that (ADR 0010 lock #5; enforced by
    # tests/lint/test_runner_worker_polars_threads_first.py). The pl annotations
    # below are strings under `from __future__ import annotations`; the single
    # runtime use (pl.concat / pl.DataFrame in _stitch_path) imports pl locally.
    import polars as pl


def _worker_run_one_param(
    params: dict[str, object],
    bar_loop_factory: Callable[[dict[str, object]], BarLoop],
    start_dt: date,
    end_dt: date,
) -> ConstantWeightDemoResult:
    """Worker bootstrap that builds one BarLoop and runs it.

    Per ADR 0010 lock #5 `POLARS_MAX_THREADS=1` MUST be the first
    executable statement before any Polars-importing module is loaded.
    The lint test at tests/lint/test_runner_worker_polars_threads_first.py
    AST-walks this function body and asserts the invariant.

    `bar_loop_factory` is a module-level callable per ADR 0010 lock #6;
    closures are not picklable under spawn on Windows. The factory must
    accept the params dict and return a BarLoop ready to run.
    """
    # MUST be the first executable statement. No imports above this line.
    import os
    os.environ["POLARS_MAX_THREADS"] = "1"

    bar_loop = bar_loop_factory(params)
    return bar_loop.run(start_dt=start_dt, end_dt=end_dt)


def _assert_cell_partition(
    path_map: tuple[tuple[int, ...], ...],
    splits: list[Split],
    phi: int,
    n_groups: int,
) -> None:
    """Cross-check the CPCV cell-partition invariant (M4 PR 3b).

    Each of the phi paths must tile all n_groups exactly once: position g of
    every path holds a combination index whose test set includes group g. This
    is a correctness gate on `path_assignments()` / `split()`, NOT caller-input
    validation, so it raises `AssertionError` directly rather than using a bare
    `assert` statement: a bare assert is stripped under `python -O`, and a
    violation of this invariant would silently corrupt the stitched per-path
    curve. The same invariant is independently pinned in tests/validation.
    """
    if len(path_map) != phi:
        raise AssertionError(
            f"path_assignments() returned {len(path_map)} paths; expected "
            f"expected_path_count()={phi}"
        )
    for path in path_map:
        if len(path) != n_groups:
            raise AssertionError(
                f"CPCV path {path} has length {len(path)}; expected "
                f"n_groups={n_groups}"
            )
        for g in range(n_groups):
            combo_idx = path[g]
            if g not in splits[combo_idx].test_groups:
                raise AssertionError(
                    f"CPCV cell-partition violation: path position {g} maps to "
                    f"combination {combo_idx} whose test_groups "
                    f"{splits[combo_idx].test_groups} do not include group {g}"
                )


def _stitch_path(
    path: tuple[int, ...],
    segment_by_group: dict[int, pl.DataFrame],
    initial_capital: float,
) -> pl.DataFrame:
    """Concatenate the per-group [dt, nav] segments into one path equity curve.

    For a deterministic factor every combination's group-g out-of-sample
    segment is identical, so path j (which `_assert_cell_partition` verified
    tiles groups 0..N-1) is stitched by concatenating `segment_by_group[g]` for
    g in 0..N-1 in timeline order; the `path` combination indices select
    nothing here (that is the degeneracy of ADR 0016 dec 4). The running NAV
    level is carried across the N-1 seams so per-group growth factors compound
    and within-group per-bar returns are preserved exactly; an implicit 0%
    return is injected at each seam (the prior group's last NAV equals the next
    group's rescaled first NAV).

    With the zero-cost matcher the demos wire there is no commission seam cost:
    the stitch only rescales levels, and the stitched path differs from a
    single contiguous run only by the omitted inter-group gap-day bars. The
    ADR 0016 commission seam artifact and the contiguous level reference are
    PR 3 deliverables against the real cost-bearing bundle, not this body.
    """
    import polars as pl  # local import; see the module-top TYPE_CHECKING note

    running = initial_capital
    dt_parts: list[pl.Series] = []
    nav_parts: list[pl.Series] = []
    for g in range(len(path)):
        seg_nav = segment_by_group[g]["nav"]
        factor = running / float(seg_nav[0])
        rescaled = seg_nav * factor
        dt_parts.append(segment_by_group[g]["dt"])
        nav_parts.append(rescaled)
        running = float(rescaled[-1])
    stitched = pl.DataFrame(
        {"dt": pl.concat(dt_parts), "nav": pl.concat(nav_parts)}
    )
    if stitched.schema["dt"] != pl.Date:
        raise ValueError(
            f"stitched CPCV path dt column must be pl.Date for the adapter's "
            f"year attribution; got {stitched.schema['dt']}"
        )
    if (
        not stitched["dt"].is_sorted(descending=False)
        or stitched["dt"].n_unique() != stitched.height
    ):
        raise ValueError(
            "stitched CPCV path dt column is not strictly ascending across "
            "group seams; the per-group windows must be contiguous and "
            "non-overlapping"
        )
    return stitched


def _is_flat(nav: pl.Series) -> bool:
    """Whether the per-bar returns have zero variance (an all-flat curve).

    Mirrors the `to_backtest_result` std==0 guard so a flat path is skipped
    BEFORE adapting, without coupling to the adapter's message string. The
    adapter stays the sole authority for the analytics; this is the skip gate.
    A too-short curve (< 2 returns) is NOT treated as flat: it is a genuine
    t_obs < 2 misuse the adapter must raise on, so this returns False for it
    and lets the adapter's loud failure propagate.
    """
    returns = nav.pct_change().drop_nulls()
    if returns.len() < 2:
        return False
    std = returns.std()
    return std is None or std == 0.0


def _is_flat_curve_error(exc: ValueError) -> bool:
    """Whether a `to_backtest_result` ValueError is its flat-curve rejection.

    Used ONLY as a desync diagnostic: if `_is_flat` passed a curve but the
    adapter still rejects it as flat, the two flatness decisions disagree (a
    bug in `_is_flat`), which `run_cpcv` surfaces loudly rather than silently
    skipping. It is never a normal control-flow path.
    """
    msg = str(exc)
    return "non-flat" in msg or "zero variance" in msg


def _wrap_stitched_as_demo(
    stitched: pl.DataFrame,
    template: ConstantWeightDemoResult,
    initial_capital: float,
    n_rebalances: int,
) -> ConstantWeightDemoResult:
    """Wrap a stitched [dt, nav] path curve as a ConstantWeightDemoResult.

    `to_backtest_result` reads only the equity_curve (dt + nav), the
    sharadar_bundle (DSR fingerprint), the confidence_tier, and the tickers
    (length only); it never reads `n_rebalances`, the shares_*, or the cash
    columns, so the minimal [dt, nav] curve suffices. Identity metadata
    (tickers, confidence_tier, sharadar_bundle) is copied from the group-0
    template; the start/end span, n_trading_days, and n_rebalances reflect the
    FULL stitched path (n_rebalances is the sum across the group segments), so
    the scorecard metadata is honest even though the adapter does not consume
    n_rebalances.
    """
    final_nav = float(stitched["nav"][-1])
    return ConstantWeightDemoResult(
        final_pnl=final_nav - initial_capital,
        final_nav=final_nav,
        initial_capital=initial_capital,
        equity_curve=stitched,
        n_trading_days=stitched.height,
        n_rebalances=n_rebalances,
        tickers=template.tickers,
        start_dt=stitched["dt"][0],
        end_dt=stitched["dt"][-1],
        confidence_tier=template.confidence_tier,
        sharadar_bundle=template.sharadar_bundle,
    )


class Runner:
    """Orchestrates CPCV paths and parameter sweeps across worker processes.

    `run_sweep` (M2 PR C1, ADR 0010) runs a parameter sweep across worker
    processes and returns a raw `list[ConstantWeightDemoResult]`. `run_cpcv`
    (M5 PR 2c, ADR 0016) runs CPCV by evaluating the strategy per contiguous
    group and stitching per-path equity curves; it runs in-process (no worker
    pool) because the N group-backtests are few and the per-path stitching is
    pure-Python.
    """

    __slots__ = ("_num_workers",)

    def __init__(self, num_workers: int | None = None) -> None:
        """Construct a Runner.

        num_workers defaults to None at construction; the actual default
        is computed lazily in run_sweep based on the param_grid size and
        cpu_count. Callers that want explicit control pass an int.
        """
        self._num_workers = num_workers

    def run_cpcv(
        self,
        cv_splitter: CPCVSplitter,
        observations: pl.DataFrame,
        label_horizons: pl.Series,
        bar_loop_factory: Callable[[date, date], BarLoop],
        *,
        registry: TrialRegistry,
        strategy_family: str,
        universe_id: str,
        periods_per_year: int = 252,
    ) -> BacktestPathDistribution[BacktestResult]:
        """Run CPCV and return the per-path BacktestResult distribution.

        Implements the ADR 0016 contract. For the M5 deterministic momentum
        factor the phi(N, k) reconstructed paths coincide (ADR 0016 dec 4):
        this body runs EXACTLY N per-group backtests (NOT C(N, k)), stitches
        each path from the per-group out-of-sample segments (the per-path
        combination indices from `path_assignments()` are a cell-partition
        cross-check only, not a segment selector), and reports the near-zero
        path dispersion as the instructive degeneracy finding rather than
        dressing a single line up as a distribution.

        The four positionals are ADR-locked (dec 1). The keyword-only
        registry/strategy_family/universe_id/periods_per_year feed
        `to_backtest_result` per the 2026-06-01 amendment footer; the
        phi-identical path trials are isolated into a
        `f"{strategy_family}::cpcv_paths"` sub-family over the passed
        registry's db file at naive_effective_n=1 so they cannot inflate the
        study family's Deflated Sharpe (Plan-reviewer Critical 2).

        Args:
          cv_splitter: the CPCVSplitter; N is read from `cv_splitter.n_groups`.
          observations: one row per rebalance; a pl.Date 'dt' column, ascending.
          label_horizons: per-observation label end dates (pl.Date). Validated
            by the splitter, but it does NOT affect the deterministic-factor
            output (the embargo-invariance test proves this); it shapes
            purge/embargo for the future ML per-combination-fit case (ADR
            0016 dec 7), which never runs here.
          bar_loop_factory: builds a backtest scoped to a contiguous
            [group_start, group_end] window; the body calls
            `.run(start_dt=group_start, end_dt=group_end)` per group.
          registry: the study trial registry; only its db_path is used (the
            path trials go to the namespaced sub-family, not the study family).
          strategy_family: the study family; path trials use
            `f"{strategy_family}::cpcv_paths"`.
          universe_id: the universe id recorded with each path trial.
          periods_per_year: annualization factor passed to the adapter.

        Raises:
          ValueError: propagated from `cv_splitter.split` on bad
            observations/label_horizons; from the adapter on a genuine
            t_obs < 2 misuse (re-raised, not skipped); and from
            BacktestPathDistribution when every path was skipped as flat (an
            empty distribution).

        Warns:
          When `expected_path_count()` < 30 (the BacktestPathDistribution
          sparse-path stability warning), i.e. the N=6 k=2 = 5 case.
        """
        # 1. Validate inputs via the splitter (eager drain; index-set math
        #    only, NOT a backtest per combination). Raises ValueError on a bad
        #    'dt' column, bad label_horizons, or observations.height < N.
        splits = list(cv_splitter.split(observations, label_horizons))

        # 2. Derive N and the per-group contiguous windows from the splitter's
        #    own source of truth (the same remainder-front partition split()
        #    uses internally), not a re-implementation.
        dt_values = observations["dt"].to_list()
        n_obs = observations.height
        n_groups = cv_splitter.n_groups
        folds = contiguous_folds(n_obs, n_groups)

        # 3. Cell-partition cross-check (correctness gate; selects no segment).
        path_map = cv_splitter.path_assignments()
        phi = cv_splitter.expected_path_count()
        _assert_cell_partition(path_map, splits, phi, n_groups)

        # 4. Run EXACTLY n_groups per-group backtests; build the group -> [dt,
        #    nav] segment map. For a deterministic factor every combination's
        #    group-g segment is identical, so one run per group suffices.
        segment_by_group: dict[int, pl.DataFrame] = {}
        initial_capital: float | None = None
        template: ConstantWeightDemoResult | None = None
        total_n_rebalances = 0
        for g in range(n_groups):
            gs, ge = folds[g]
            group_start = dt_values[gs]
            group_end = dt_values[ge - 1]
            group_result = bar_loop_factory(group_start, group_end).run(
                start_dt=group_start, end_dt=group_end
            )
            segment_by_group[g] = group_result.equity_curve.select(
                ["dt", "nav"]
            ).sort("dt")
            total_n_rebalances += group_result.n_rebalances
            if initial_capital is None:
                initial_capital = group_result.initial_capital
                template = group_result
        # n_groups >= 2 is guaranteed by CPCVSplitter (n_groups >= 2), so the
        # loop ran at least once and both are set.
        assert initial_capital is not None and template is not None

        # 5. Isolate the phi-identical path trials from the study DSR family
        #    (Critical 2): a derived sibling registry over the SAME db file at
        #    naive_effective_n=1, recording into a `::cpcv_paths` sub-family.
        #    The forced naive=1 degenerates each per-path DSR query to PSR (it
        #    never hits the single-trial-with-naive>1 raise), and the namespace
        #    keeps the study family's (n_effective, v_sr) untouched.
        iso = TrialRegistry(registry.db_path, naive_effective_n=1)
        path_family = f"{strategy_family}::cpcv_paths"

        # 6. Stitch each path, gating flat paths and (defensively) NaN sr_hat.
        path_results: list[BacktestResult] = []
        for path in path_map:
            stitched = _stitch_path(path, segment_by_group, initial_capital)
            if _is_flat(stitched["nav"]):
                # The pre-check is the sole skip authority; a flat per-path
                # curve cannot yield a finite Sharpe, so skip before adapting.
                continue
            demo = _wrap_stitched_as_demo(
                stitched, template, initial_capital, total_n_rebalances
            )
            try:
                result = to_backtest_result(
                    demo,
                    registry=iso,
                    strategy_family=path_family,
                    universe_id=universe_id,
                    periods_per_year=periods_per_year,
                )
            except ValueError as exc:
                if _is_flat_curve_error(exc):
                    # The pre-check passed this curve but the adapter rejects
                    # it as flat: a _is_flat/adapter desync, not a skip-worthy
                    # path. Surface it loudly rather than swallowing it.
                    raise ValueError(
                        "run_cpcv: the non-flat pre-check and to_backtest_result"
                        " disagree on flatness; investigate _is_flat. Adapter "
                        f"said: {exc}"
                    ) from exc
                # A genuine t_obs < 2 (or other) misuse propagates unchanged.
                raise
            if math.isnan(result.sr_hat):
                # ADR 0015 dec 5 NaN gate at the construction site. Practically
                # unreachable through the adapter (it raises on zero variance
                # before computing sr_hat = mean/std), so this is the
                # belt-and-suspenders obligation, not a reachable branch.
                continue
            path_results.append(result)

        # 7. Construct the distribution. Raises on an all-skipped (empty)
        #    result; warns when phi < 30 (the N=6 k=2 = 5 sparse-path case).
        return BacktestPathDistribution(path_results, phi)

    def run_sweep(
        self,
        param_grid: list[dict[str, object]],
        bar_loop_factory: Callable[[dict[str, object]], BarLoop],
        *,
        start_dt: date,
        end_dt: date,
    ) -> list[ConstantWeightDemoResult]:
        """Run a parameter sweep across worker processes.

        Per ADR 0010 lock #1 returns a raw list[ConstantWeightDemoResult]
        in param_grid order. Analytics-layer wrapping into SensitivityBand
        happens at SensitivityBand.from_run_sweep.

        Per ADR 0010 lock #4 the multiproc context is spawn on both
        platforms; fork-on-Linux is rejected because the parent process's
        already-initialized Polars thread pool would be inherited and
        violate the POLARS_MAX_THREADS=1 invariant.

        Per ADR 0010 lock #6 the picklability gate runs at submit time:
        BOTH pickle.dumps((bar_loop_factory, param_grid[0])) AND a
        synchronous dry-run of bar_loop_factory(param_grid[0]) in the
        parent process BEFORE submitting to the pool.

        Per ADR 0010 lock #7 num_workers defaults to
        min(len(param_grid), max(1, cpu_count() - 1)).

        Raises:
        - ValueError if param_grid is empty.
        - RuntimeError if the picklability or dry-run probe fails (the
          message names which probe and includes the underlying error).
        """
        if not param_grid:
            raise ValueError("param_grid is empty; nothing to sweep")

        # Picklability + dry-run probe per ADR 0010 lock #6.
        first_params = param_grid[0]
        try:
            pickle.dumps((bar_loop_factory, first_params))
        except (pickle.PicklingError, AttributeError, TypeError) as e:
            raise RuntimeError(
                f"Runner.run_sweep picklability probe failed: "
                f"pickle.dumps((bar_loop_factory, param_grid[0])) raised "
                f"{type(e).__name__}: {e}. The factory must be a module-"
                f"level callable (closures are not picklable under spawn "
                f"on Windows); the params dict must contain only picklable "
                f"types (Decimal, str, int, float, tuple of these)."
            ) from e
        try:
            _ = bar_loop_factory(first_params)
        except Exception as e:
            raise RuntimeError(
                f"Runner.run_sweep dry-run probe failed: "
                f"bar_loop_factory(param_grid[0]) raised "
                f"{type(e).__name__}: {e}. The factory must construct a "
                f"BarLoop synchronously without side effects that would "
                f"fail under spawn (e.g., missing snapshots, network "
                f"calls at module import time)."
            ) from e

        if self._num_workers is None:
            num_workers = min(
                len(param_grid),
                max(1, multiprocessing.cpu_count() - 1),
            )
        else:
            num_workers = self._num_workers

        ctx = multiprocessing.get_context("spawn")
        args_list = [
            (params, bar_loop_factory, start_dt, end_dt) for params in param_grid
        ]

        # Single-worker fast path: run inline so tests do not pay the
        # spawn bootstrap cost when num_workers == 1. The inline path is
        # semantically equivalent to the pool path (sets POLARS_MAX_THREADS
        # in-process, calls _worker_run_one_param). Determinism is
        # preserved because both paths invoke the same function with the
        # same arguments in param_grid order.
        if num_workers == 1:
            # Single-worker fast path: run inline in the parent process.
            #
            # CAVEAT (post-impl reviewer Critical #1): the parent's Polars
            # thread pool may have already been constructed BEFORE the
            # env-var assignment inside `_worker_run_one_param` runs,
            # because the runner module's own imports transitively load
            # Polars (via `bar_loop`, `constant_weight_result`, etc.).
            # The env-var assignment is therefore a no-op for the
            # parent's already-fixed pool. The ADR 0010 lock #5
            # invariant (`pl.thread_pool_size() == 1` in the worker)
            # holds ONLY for the multi-worker spawn path below where
            # each worker is a fresh Python process.
            #
            # Callers that want bit-identical determinism across
            # `num_workers=1` runs must ensure the parent process's
            # pool size is stable across the two runs (typically by
            # constructing the Runner before any Polars DataFrame
            # operation that would trigger pool construction). The
            # multi-worker spawn path is the canonical contract; this
            # fast path is a unit-test convenience.
            results = [_worker_run_one_param(*args) for args in args_list]
            return results

        with ctx.Pool(num_workers) as pool:
            results = pool.starmap(_worker_run_one_param, args_list)
        return results
