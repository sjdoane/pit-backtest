"""Signal protocol and PitView callable type.

Per ADR 0003 decision 5: Signal.compute returns dict[AssetId, float]
(score per asset). The engine attaches dt; signals do not.

The pit_view callable is the engine's structural lookahead protection.
It returns a Polars LazyFrame sliced to available_dt < dt (strict less
than). Signals operate on this view; the engine never passes data with
available_dt == dt to a signal compute call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol

import polars as pl

from pit_backtest.data.records import AssetId
from pit_backtest.data.universe import Universe


# A pit_view callable: takes a table name and returns a LazyFrame sliced to
# available_dt < the current bar's dt. The engine constructs this closure
# per bar and passes it to Signal.compute. Per docs/methodology/determinism.md
# trust boundary #1, signals must use pit_view and not bypass it to read
# the same tables directly.
PitView = Callable[[str], pl.LazyFrame]


class Signal(Protocol):
    """Cross-sectional signal computed at each rebalance bar."""

    def required_lookback_days(self) -> int:
        """How many trading days of history this signal needs."""
        ...

    def compute(
        self, universe: Universe, dt: datetime, pit_view: PitView
    ) -> dict[AssetId, float]:
        """Compute scores for every member of the universe at dt.

        Returns a dict[AssetId, float] sorted by AssetId for determinism.
        Missing assets (insufficient history, NaN inputs) are omitted from
        the result rather than included with NaN scores; the policy layer
        treats absence as "no opinion" and absence is symmetric to a zero
        score in the rank-then-select policies.
        """
        ...
