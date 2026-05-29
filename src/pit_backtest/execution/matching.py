"""MatchingEngine protocol and CloseFillMatchingEngine for M1.

Per ADR 0003 decision 6: submit returns list[Fill] (empty = no fill, multi =
partial-fill or multi-bar rollover).

CloseFillMatchingEngine is the M1 implementation: every order fills at
today's close with zero slippage, zero impact, zero commission. Per the
M1-day-3 skeptical reviewer, this is a real engine class (not a private
helper inside BarLoop) so the M2 SquareRootImpactMatchingEngine can be
swapped in with identical surface area and the re-run is interpretable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.execution.clock import Clock
from pit_backtest.execution.orders import Fill, FillPriceModel, Order


@attrs.frozen(slots=True)
class MarketState:
    """Per-bar snapshot the matching engine needs to compute fills.

    open, high, low, close are impacted prices (already adjusted by the
    ImpactedPriceSource decorator in M2+). volume is unimpacted bar volume.
    For the M1 CloseFillMatchingEngine, only close is consulted; the other
    fields are present for forward compatibility with M2 cost models.
    """

    asset_id: AssetId
    dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class UnsupportedFillPriceModelError(NotImplementedError):
    """Raised when an Order's fill_price_model is not supported by the
    current MatchingEngine implementation. CloseFillMatchingEngine raises
    this for any model other than FillPriceModel.CLOSE.
    """


class MatchingEngine(Protocol):
    """Order-to-fill translation. Per ADR 0003 decision 6 returns list[Fill]."""

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        """Apply fill_price_model, compute cost, return zero or more Fill records.

        Empty list = no fill (e.g., market closed for the bar, or volume
        is zero). One Fill = full fill in this bar. Multiple Fills =
        partial fill with rollover (M2+).
        """
        ...


@attrs.frozen(slots=True)
class CloseFillMatchingEngine:
    """M1 matching engine: every order fills at today's close, zero cost.

    Accepts only FillPriceModel.CLOSE. Raises UnsupportedFillPriceModelError
    for other models so a future bug (e.g., M5 momentum signal accidentally
    using NEXT_BAR_OPEN) surfaces at the matcher, not as a silent close-price
    substitution.

    Slippage, temporary impact, permanent impact, commission all zero.
    M2's SquareRootImpactMatchingEngine replaces this with a real cost
    model; the BarLoop wiring is unchanged.
    """

    clock: Clock

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        if order.fill_price_model != FillPriceModel.CLOSE:
            raise UnsupportedFillPriceModelError(
                f"CloseFillMatchingEngine accepts only FillPriceModel.CLOSE; "
                f"got {order.fill_price_model}. M2 SquareRootImpactMatchingEngine "
                f"adds the other fill-price models."
            )
        if order.asset_id != market_state.asset_id:
            raise ValueError(
                f"order asset_id {order.asset_id} does not match market_state "
                f"asset_id {market_state.asset_id}"
            )
        return [
            Fill(
                order_id=order.order_id,
                asset_id=order.asset_id,
                quantity=order.quantity,
                fill_price=market_state.close,
                slippage_bps=Decimal("0"),
                temporary_impact_bps=Decimal("0"),
                permanent_impact_per_share=Decimal("0"),
                commission=Decimal("0"),
                dt=market_state.dt,
            )
        ]
