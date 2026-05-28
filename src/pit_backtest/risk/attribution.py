"""Factor attribution: minimal at v1, expanded in v1.1.

Per ADR 0001 decision 5, risk decomposition is its own layer. The v1
deliverable is the data hooks so the scorecard's risk-adjusted section
can populate; full factor attribution against a fitted Fama-French style
block lives in v1.1.
"""

from __future__ import annotations

from typing import Protocol

import polars as pl


class RiskAttributor(Protocol):
    """Decomposes a return series into factor contributions."""

    def attribute(self, returns: pl.DataFrame) -> pl.DataFrame:
        """Return a frame keyed by date with columns per attributed factor."""
        ...
