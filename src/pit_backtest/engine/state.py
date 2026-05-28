"""PortfolioState: positions, cash, P&L.

The mutable form is used inside the BarLoop; the immutable snapshot
(frozen attrs) is what crosses boundaries to analytics and to the
BacktestResult render path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import attrs

from pit_backtest.data.records import AssetId


@attrs.define(slots=True)
class PortfolioState:
    """Mutable per-bar portfolio state. Lives inside the BarLoop.

    For external consumers, .snapshot() returns a frozen PortfolioSnapshot
    that is safe to retain across bars.
    """

    cash: Decimal
    positions: dict[AssetId, Decimal]
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    def snapshot(self, dt: datetime) -> "PortfolioSnapshot":
        raise NotImplementedError("M1 deliverable")


@attrs.frozen(slots=True)
class PortfolioSnapshot:
    """Immutable point-in-time portfolio state.

    Returned by PortfolioState.snapshot(). Safe to retain; safe to compare
    across bars; safe to feed into the analytics layer.
    """

    dt: datetime
    cash: Decimal
    positions: dict[AssetId, Decimal]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_value: Decimal
