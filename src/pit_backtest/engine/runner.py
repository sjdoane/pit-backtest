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

import multiprocessing
import pickle
from datetime import date
from typing import Callable

from pit_backtest.analytics.distribution import BacktestPathDistribution
from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.cv import CPCVSplitter


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


class Runner:
    """Orchestrates CPCV paths and parameter sweeps across worker processes.

    M2 PR C1 implements `run_sweep` per ADR 0010. M4 will add `run_cpcv`;
    the stub continues to raise NotImplementedError.
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
        bar_loop_factory: Callable[[], BarLoop],
    ) -> BacktestPathDistribution[BacktestResult]:
        raise NotImplementedError("M4 deliverable")

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
