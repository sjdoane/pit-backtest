"""MatchingEngine: translates Orders to Fills, applies impact, applies commission.

Per ADR 0003 decision 6: submit returns list[Fill] (empty = no fill, multi =
partial-fill or multi-bar rollover). Per ADR 0003 decision 7 (cost layer):
queries FillCostComputer for the per-fill breakdown.

Partial fills via participation-rate cap (default 10% of bar volume) with
rollover (default 5-bar decay per ADR 0003 decision 20).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.execution.clock import Clock
from pit_backtest.execution.cost.base import FillCostComputer
from pit_backtest.execution.orders import Fill, Order


@attrs.frozen(slots=True)
class MarketState:
    """Per-bar snapshot the matching engine needs to compute fills.

    open, high, low, close are impacted prices (already adjusted by the
    ImpactedPriceSource decorator). volume is unimpacted bar volume.
    """

    asset_id: AssetId
    dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class MatchingEngine:
    """Order-to-fill translation with participation-cap partial fills."""

    def __init__(
        self,
        cost_computer: FillCostComputer,
        clock: Clock,
        max_participation_pct: Decimal = Decimal("0.10"),
        partial_fill_decay_bars: int = 5,
    ) -> None:
        raise NotImplementedError("M1 deliverable (close-price fills only); M2 extends")

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        """Apply fill_price_model, compute cost via FillCostComputer,
        return one or more Fill records.

        Empty list = no fill (e.g., market closed for the bar, or volume
        is zero). One Fill = full fill in this bar. Multiple Fills =
        partial fill with rollover (M2+).
        """
        raise NotImplementedError("M1 deliverable")
