"""JT1993 12-month total-return momentum, excluding the most recent month.

Used by the M5 worked study per ADR 0002 decision 20.
"""

from __future__ import annotations

from datetime import datetime

from pit_backtest.data.records import AssetId
from pit_backtest.data.universe import Universe
from pit_backtest.signal.base import PitView, Signal


class Momentum12_1Signal(Signal):
    """Jegadeesh-Titman 1993 12-1 momentum.

    Score = 12-month total return through one month before dt.
    Implemented on adjusted close (perspective_dt = dt) so splits and
    dividends are correctly incorporated.
    """

    def required_lookback_days(self) -> int:
        # 252 trading days + ~21 trading days buffer for the 1-month skip
        return 273

    def compute(
        self, universe: Universe, dt: datetime, pit_view: PitView
    ) -> dict[AssetId, float]:
        raise NotImplementedError("M5 deliverable")
