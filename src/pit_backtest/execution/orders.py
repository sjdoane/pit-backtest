"""Order and Fill record types; FillPriceModel enum.

Per ADR 0001 decision 7 and ADR 0003 architecture: every Order requires
an explicit FillPriceModel (no default); MOO and MOC are special cases of
OPEN and CLOSE with extra slippage applied by the cost layer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

import attrs

from pit_backtest.data.records import AssetId


class FillPriceModel(Enum):
    """Per ADR 0001 decision 7: every Order requires a FillPriceModel.

    No default. Forgetting to specify is a TypeError at Order construction,
    not a silent default that researchers discover months later.
    """

    OPEN = "open"
    CLOSE = "close"
    VWAP = "vwap"
    ARRIVAL = "arrival"
    NEXT_BAR_OPEN = "next_bar_open"


@attrs.frozen(slots=True)
class Order:
    """A trade request submitted to the matching engine.

    quantity is signed: positive = buy, negative = sell. Notional sign-
    conventions for cash flows are derived from quantity at fill time.
    """

    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price_model: FillPriceModel
    submit_dt: datetime


@attrs.frozen(slots=True)
class Fill:
    """A single execution result.

    Per ADR 0003 decision 6, MatchingEngine.submit returns list[Fill] so
    partial fills and multi-fill auctions express naturally. quantity here
    may be less than the originating Order.quantity for partial fills.

    Per ADR 0003 decision 14, slippage and impact are split conceptually:
    slippage_bps is the model's component attributable to crossing the
    spread; temporary_impact_bps is the model's component attributable to
    the order's size; permanent_impact_per_share is what the
    ImpactedPriceSource will apply to subsequent reads for this asset.
    """

    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price: Decimal
    slippage_bps: Decimal
    temporary_impact_bps: Decimal
    permanent_impact_per_share: Decimal
    commission: Decimal
    dt: datetime
