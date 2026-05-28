"""CVSplitter, PurgedKFoldSplitter, WalkForwardSplitter, CPCVSplitter.

Per ADR 0001 decision 3: CPCV is primary; walk-forward is a CPCV
configuration with one path. Per ADR 0003 decision 17: WalkForwardSplitter
ships alongside as a sanity-check baseline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol

import attrs
import polars as pl


@attrs.frozen(slots=True)
class Split:
    """A single train/test split produced by a CVSplitter."""

    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]
    embargo_indices: tuple[int, ...]


class CVSplitter(Protocol):
    """Cross-validation splitter on time-ordered observations."""

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        """Yield one Split per fold (or per CPCV path)."""
        ...


class PurgedKFoldSplitter(CVSplitter):
    """LdP chapter 7 purged k-fold with embargo."""

    def __init__(self, k: int, embargo_pct: float = 0.05) -> None:
        raise NotImplementedError("M4 deliverable")

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        raise NotImplementedError("M4 deliverable")


class WalkForwardSplitter(CVSplitter):
    """Single-path baseline; per ADR 0003 decision 17 catches a class of
    CPCV implementation bugs.
    """

    def __init__(self, train_end: datetime, test_start: datetime) -> None:
        raise NotImplementedError("M4 deliverable")

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        raise NotImplementedError("M4 deliverable")


class CPCVSplitter(CVSplitter):
    """Combinatorial Purged Cross-Validation.

    Produces phi(N, k) = (k/N) * C(N, k) paths. Default N=6, k=2 gives 5
    paths; the acceptance criterion in ADR 0002 decision 2 is N=6, k=2.
    """

    def __init__(
        self, n_groups: int, k_test: int, embargo_pct: float = 0.05
    ) -> None:
        raise NotImplementedError("M4 deliverable")

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        raise NotImplementedError("M4 deliverable")
