"""Deterministic Polars frame helpers.

Per docs/methodology/determinism.md Requirement 3: every frame that exits
a non-trivial Polars operation is explicitly sorted before consumption.
The sorted_by helper is the single API the engine uses; reviewers grep for
sorted_by to verify discipline.
"""

from __future__ import annotations

import polars as pl


def sorted_by(df: pl.DataFrame, *keys: str) -> pl.DataFrame:
    """Sort df by keys, asserting deterministic order at call site.

    Wraps df.sort(*keys) so grepping for sorted_by surfaces every sort
    boundary in the engine. Reviewers gate determinism on this discipline.
    """
    return df.sort(by=list(keys))


def sorted_lazy_by(lf: pl.LazyFrame, *keys: str) -> pl.LazyFrame:
    """Lazy-frame variant of sorted_by."""
    return lf.sort(by=list(keys))
