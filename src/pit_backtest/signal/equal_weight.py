"""EqualWeightSignal: emits equal weight per ticker on every bar.

Used by the constant-weight monthly rebalance demo (ADR 0002 acceptance
criterion 2). The Policy combines these weights with the rebalance-date
calendar to decide when to trade.
"""

from __future__ import annotations

from datetime import datetime

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.data.universe import Universe
from pit_backtest.signal.base import PitView


@attrs.frozen(slots=True)
class EqualWeightSignal:
    """Constant equal weight per ticker.

    Lookback days = 0 (signal does not depend on history). Universe and
    pit_view are accepted for protocol conformance but ignored; the M1
    BarLoop drives the rebalance dates via the Policy's calendar.

    Returns a dict[AssetId, float] sorted by AssetId for determinism.
    Weights sum to 1.0 when all tickers are live.
    """

    tickers: tuple[AssetId, ...]  # sorted ascending at construction

    def required_lookback_days(self) -> int:
        return 0

    def compute(
        self, universe: Universe, dt: datetime, pit_view: PitView
    ) -> dict[AssetId, float]:
        n = len(self.tickers)
        if n == 0:
            return {}
        weight = 1.0 / n
        return {ticker: weight for ticker in sorted(self.tickers)}
