"""Runner: multiprocess CPCV path and parameter-sweep orchestrator.

Per ADR 0003 architecture: parallelism lives in the Runner, not the
BarLoop. Each worker is an independent process with a read-only data view;
no shared state. Per docs/methodology/determinism.md Requirement 5: every
worker sets POLARS_MAX_THREADS=1 before importing Polars to keep aggregation
order deterministic.
"""

from __future__ import annotations

from typing import Callable

import polars as pl

from pit_backtest.analytics.distribution import BacktestPathDistribution
from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.validation.cv import CPCVSplitter


class Runner:
    """Orchestrates CPCV paths and parameter sweeps across worker processes."""

    def __init__(self, num_workers: int | None = None) -> None:
        raise NotImplementedError("M4 deliverable (CPCV); M2 deliverable (sweep)")

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
    ) -> pl.DataFrame:
        """Sweep-mode results tagged ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION
        until corrected by a CPCV+DSR pass.
        """
        raise NotImplementedError("M2 deliverable (sensitivity bands)")
