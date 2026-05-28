"""PitDataSource protocol and ImpactedPriceSource decorator.

Per ADR 0003 architecture sketch and decision 3: PitDataSource exposes
per-row reads (get_price, get_fundamental, get_corporate_actions, get_cash_flows,
members_at, get_delisting) plus the forward-compatibility seam get_table.

ImpactedPriceSource (decision 3) is a decorator over a raw PitDataSource
that applies cumulative permanent impact from past fills to every price
read. Signal.compute and portfolio valuation always see impacted prices.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

import polars as pl

from pit_backtest.data.records import (
    AssetId,
    CashFlow,
    CorporateAction,
)


PriceField = Literal["open", "high", "low", "close", "volume"]
FundamentalFlavor = Literal["ARQ", "ART", "ARY"]


class PitDataSource(Protocol):
    """Point-in-time vendor data source.

    All reads gate on available_dt; methods raise if a caller asks for
    data with available_dt > dt.
    """

    def get_price(
        self, asset_id: AssetId, dt: datetime, field: PriceField
    ) -> Decimal:
        """Return the raw bar value at dt. No adjustment."""
        ...

    def get_fundamental(
        self,
        asset_id: AssetId,
        available_dt: datetime,
        field: str,
        flavor: FundamentalFlavor,
    ) -> Decimal | None:
        """Return the most recent fundamental with available_dt <= the given dt.

        Returns None if no such record exists.
        """
        ...

    def get_corporate_actions(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CorporateAction]:
        """All unit-transformation corporate actions for asset_id with
        ex_date in [start_dt, end_dt].
        """
        ...

    def get_cash_flows(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CashFlow]:
        """All cash flows (dividends, delisting cash proceeds, spin-off
        cash equivalents) for asset_id with dt in [start_dt, end_dt].
        """
        ...

    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        """Universe membership; backs the Universe protocol."""
        ...

    def get_delisting(self, asset_id: AssetId) -> CashFlow | CorporateAction | None:
        """Delisting record if one exists; None if asset is still active.

        Cash delistings are returned as CashFlow; stock-for-stock as
        DelistingStockAcquisitionAction.
        """
        ...

    def get_table(self, table_name: str) -> pl.LazyFrame:
        """Forward-compatibility seam for v1.1 alternative-data adapters.

        At v1 this dispatches to the per-table methods above. v1.1 alternative
        data sources can implement only get_table to avoid implementing the
        full per-row protocol.
        """
        ...


class ImpactedPriceSource:
    """Decorator over a raw PitDataSource.

    Maintains a per-asset cumulative permanent-impact register from past
    fills; applies it to every price read so Signal.compute and portfolio
    valuation always see impacted prices.

    Reset by Backtest.__init__. Per ADR 0003 decision 3, this is the
    architectural mechanism that replaces the PermanentImpactRegister
    component from the pre-review architecture.
    """

    def __init__(self, raw: PitDataSource) -> None:
        raise NotImplementedError("M2 deliverable")

    def get_price(
        self, asset_id: AssetId, dt: datetime, field: PriceField
    ) -> Decimal:
        raise NotImplementedError("M2 deliverable")
