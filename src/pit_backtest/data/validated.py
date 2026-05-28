"""Pydantic boundary types for adapter load.

These models validate raw vendor data (Sharadar parquet rows, SSGA CSV rows)
exactly once at adapter load, then convert to the attrs counterpart in
data/records.py via .to_attrs(). The Pydantic object is discarded after
conversion; the engine never sees it again.

See docs/methodology/pydantic_polars_boundary.md for the boundary contract.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pit_backtest.data.records import (
    AssetId,
    CashFlow,
    CashFlowType,
    FundamentalRecord,
    PriceRecord,
    SplitAction,
)

# Shared config: frozen, no implicit assignment validation, arbitrary types
# (Decimal, datetime) allowed. The validation happens once at construction;
# subsequent attribute access never re-validates.
_BOUNDARY_CONFIG = ConfigDict(
    frozen=True,
    arbitrary_types_allowed=True,
    validate_assignment=False,
    str_strip_whitespace=True,
)


class PydanticPriceRecord(BaseModel):
    """Validates a single SEP-equivalent price row at adapter load.

    Constructed once per parquet row; .to_attrs() called immediately;
    PydanticPriceRecord is then discarded.
    """

    model_config = _BOUNDARY_CONFIG

    asset_id: AssetId
    period_end_dt: datetime
    available_dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_adjustment: Decimal

    def to_attrs(self) -> PriceRecord:
        raise NotImplementedError("M1 deliverable")


class PydanticFundamentalRecord(BaseModel):
    """Validates a single SF1-equivalent fundamental row at adapter load."""

    model_config = _BOUNDARY_CONFIG

    asset_id: AssetId
    period_end_dt: datetime
    available_dt: datetime
    field: str
    value: Decimal
    flavor: Literal["ARQ", "ART", "ARY"]

    def to_attrs(self) -> FundamentalRecord:
        raise NotImplementedError("M3 deliverable")


class PydanticSplitAction(BaseModel):
    """Validates a single split row at adapter load."""

    model_config = _BOUNDARY_CONFIG

    asset_id: AssetId
    ex_date: datetime
    ratio: Decimal

    def to_attrs(self) -> SplitAction:
        raise NotImplementedError("M3 deliverable")


class PydanticCashFlow(BaseModel):
    """Validates a dividend, delisting cash proceeds, or spin-off cash
    equivalent at adapter load.
    """

    model_config = _BOUNDARY_CONFIG

    asset_id: AssetId | None
    dt: datetime
    flow_type: CashFlowType
    amount: Decimal

    def to_attrs(self) -> CashFlow:
        raise NotImplementedError("M3 deliverable")
