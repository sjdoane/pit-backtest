"""Module-level probe factory for the multi-worker spawn-path determinism
test.

Per ADR 0010 lock #5 and post-impl reviewer Critical #1, the runner's
spawn path is the canonical contract for POLARS_MAX_THREADS=1
enforcement. This module provides a module-level factory that the
test passes to Runner.run_sweep with num_workers >= 2 so each worker
is a fresh process; the factory asserts pl.thread_pool_size() == 1
inside the worker and raises if not.

The leading underscore on the module name signals "test infrastructure"
so pytest's collection skips it.
"""

from __future__ import annotations

from pit_backtest.engine.bar_loop import BarLoop


def polars_threads_probe_factory(params: dict[str, object]) -> BarLoop:
    """Factory that asserts pl.thread_pool_size() == 1 inside the worker.

    Called by `_worker_run_one_param` AFTER the env-var assignment runs.
    Imports polars lazily so the import itself does not break the test.
    Raises AssertionError if the pool size is not 1; the test catches
    via the Runner's dry-run probe path (which surfaces the underlying
    error in a RuntimeError).
    """
    del params
    import polars as pl
    pool_size = pl.thread_pool_size()
    if pool_size != 1:
        raise AssertionError(
            f"POLARS_MAX_THREADS=1 invariant violated: "
            f"pl.thread_pool_size() == {pool_size} in the worker"
        )
    raise NotImplementedError(
        "polars_threads_probe_factory only validates pool size"
    )
