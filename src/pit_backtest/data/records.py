"""Inner-loop record types: attrs frozen with slots.

These types cross the inner BarLoop millions of times per backtest. Per
docs/methodology/pydantic_polars_boundary.md, they must be attrs (not
Pydantic) to meet the 60-second performance budget from ADR 0001 decision 13.

The Pydantic validated counterparts (PydanticPriceRecord, etc.) live in
data/validated.py and are constructed exactly once per row at adapter load.

Discriminated-union pattern: CorporateAction is a type alias over its
concrete subclasses (SplitAction, DelistingStockAcquisitionAction). The
action_type Literal on each subclass is the discriminator; pattern
matching on the concrete type is the recommended dispatch. The union
approach avoids the attrs+slots inheritance gotcha where a subclass
default field cannot precede a parent no-default field.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, NewType

import attrs

# Persistent asset identifier. Per ADR 0003 decision 8, AssetId is a NewType
# over int so that the v1 Sharadar permaticker can be the concrete carrier
# without locking the type system to Sharadar. v2 can introduce other
# identifier kinds (CRSP PERMNO, ISIN) without a type migration.
AssetId = NewType("AssetId", int)


@attrs.frozen(slots=True)
class PriceRecord:
    """A single daily-bar price observation.

    The dual-timestamp fields (period_end_dt, available_dt) carry the PIT
    discipline from ADR 0001 decision 9. For daily bars these are typically
    equal, but the model is structured so that the same record type
    accommodates pre-open and intra-bar revisions in v1.1.
    """

    asset_id: AssetId
    period_end_dt: datetime  # bar close, America/New_York 16:00 ET
    available_dt: datetime  # when this bar became observable; for daily bars, == period_end_dt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_adjustment: Decimal  # split + dividend cumulative factor at period_end_dt


@attrs.frozen(slots=True)
class FundamentalRecord:
    """A single PIT fundamental field.

    Per ADR 0003 architecture sketch, fundamentals are filtered to PIT
    flavors (ARQ, ART, ARY); MRQ and MRY are rejected at adapter load.
    """

    asset_id: AssetId
    period_end_dt: datetime  # quarter end (Sharadar calendardate)
    available_dt: datetime  # SEC submission date (Sharadar datekey)
    field: str
    value: Decimal
    flavor: Literal["ARQ", "ART", "ARY"]


# Corporate actions: unit transformations on existing shares.
# Per ADR 0003 decision 2, cash flows live in a separate CashFlow stream.


@attrs.frozen(slots=True)
class SplitAction:
    """A forward or reverse split.

    ratio = 2.0 for a 2-for-1 forward split (one old share becomes two new).
    ratio = 0.5 for a 1-for-2 reverse split.
    """

    asset_id: AssetId
    ex_date: datetime  # all adjustments apply on ex-date per ADR 0001 decision 6
    ratio: Decimal
    action_type: Literal["split"] = "split"


@attrs.frozen(slots=True)
class DelistingStockAcquisitionAction:
    """A delisting via stock-for-stock acquisition.

    The acquirer's permaticker and the share-exchange ratio are recorded
    so the engine can move the position into the acquirer's asset id.

    Cash-acquisition delistings flow through CashFlow, not here. Per ADR
    0002 decision 16, v1 treats stock acquisitions as cash-equivalent at
    the announced deal price; the structural support for share-exchange
    accounting lives here for v1.1.
    """

    asset_id: AssetId
    ex_date: datetime
    acquirer_asset_id: AssetId
    exchange_ratio: Decimal  # new shares per old share
    action_type: Literal["delisting_stock_acquisition"] = "delisting_stock_acquisition"


# Type alias for the corporate-action discriminated union. The action_type
# Literal on each subclass is the discriminator; mypy and pattern matching
# dispatch correctly off concrete-type isinstance checks.
CorporateAction = SplitAction | DelistingStockAcquisitionAction


# Cash flows: cash movements into or out of the portfolio.
# Per ADR 0003 decision 2, this is a separate stream from CorporateAction.

CashFlowType = Literal[
    "cash_dividend",
    "delisting_cash_proceeds",
    "spinoff_cash_equivalent",
    # v1.1: borrow_fee, short_rebate, securities_lending_revenue
]


@attrs.frozen(slots=True)
class CashFlow:
    """A cash movement on a specific date.

    asset_id is None for portfolio-level cash flows (e.g., management-fee
    accruals in v1.1). For per-asset flows (dividends, delisting cash
    proceeds, spin-off cash equivalents), asset_id identifies the source.
    """

    asset_id: AssetId | None
    dt: datetime  # ex-date for dividends; settlement date for delisting cash
    flow_type: CashFlowType
    amount: Decimal  # per-share amount; portfolio impact = amount * shares_held
