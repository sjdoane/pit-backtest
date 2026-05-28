"""BacktestPathDistribution: container for CPCV path-distributed results.

Per ADR 0001 decision 3 and ADR 0003 architecture: any single-Sharpe API
on CPCV is a correctness bug. The distribution exposes percentiles,
medians, and aggregation methods; mean is available but render-path
enforcement requires the path count be surfaced alongside.
"""

from __future__ import annotations

import warnings
from typing import Generic, TypeVar

T = TypeVar("T")


_MIN_STABLE_PATH_COUNT = 30


class BacktestPathDistribution(Generic[T]):
    """Container for the multiple paths produced by CPCV.

    Warns at construction if path_count < 30 (the stability threshold from
    the CPCV guardrails in ADR 0001 reviewer pass; below this, the
    distribution itself is too noisy to rank confidently).
    """

    def __init__(self, paths: list[T], path_count: int) -> None:
        self._paths = paths
        self.path_count = path_count
        if path_count < _MIN_STABLE_PATH_COUNT:
            warnings.warn(
                f"CPCV path count {path_count} below stability threshold "
                f"({_MIN_STABLE_PATH_COUNT}); distribution statistics may be noisy.",
                stacklevel=2,
            )

    def percentiles(self, percentiles: list[float]) -> dict[float, T]:
        raise NotImplementedError("M4 deliverable")

    def median(self) -> T:
        raise NotImplementedError("M4 deliverable")

    def p10(self) -> T:
        raise NotImplementedError("M4 deliverable")

    def p90(self) -> T:
        raise NotImplementedError("M4 deliverable")
