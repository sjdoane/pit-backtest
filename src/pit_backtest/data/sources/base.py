"""PitDataSource protocol and ImpactedPriceSource decorator.

Per ADR 0003 architecture sketch and decision 3: PitDataSource exposes
per-row reads (get_price, get_fundamental, get_corporate_actions, get_cash_flows,
members_at, get_delisting) plus the forward-compatibility seam get_table.

ImpactedPriceSource (decision 3) is a standalone decorator over a raw
PitDataSource that maintains a per-asset cumulative permanent-impact
register. The matcher updates the register after each fill; Signal.compute
and portfolio valuation read impacted prices via adjust_price(). Per ADR
0009 lock #1 and #12, the decorator does NOT inherit from PitDataSource at
M2 (the per-row get_price path is M3 deliverable). Per ADR 0009 lock #11,
the decorator's mutable register is trust boundary item 12; Signal and
Policy code must never import this module.
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
    """Standalone decorator that applies cumulative permanent impact to
    subsequent price reads.

    Per ADR 0003 decision 3: the architectural mechanism for impact
    feedback into the data layer. Per ADR 0005 step 5: intercepts price
    reads only; volume bypasses by contract (callers route volume through
    the raw source directly). Per ADR 0009 lock #1: does NOT inherit from
    PitDataSource at M2; the per-row get_price path is M3 deliverable.
    Per ADR 0009 lock #12: trust boundary item 12; Signal.compute() and
    Policy.target_positions() must never import this module.

    The register is `dict[AssetId, Decimal]` of signed dollars per share.
    A buy fill adds positive cumulative (lifts subsequent reads); a sell
    fill adds negative cumulative (lowers subsequent reads). The matcher
    is the sole caller of apply_permanent_impact; the BarLoop is the sole
    caller of adjust_price.

    The class is NOT attrs.frozen because the register is mutable; it
    uses __slots__ to lock the field shape and avoid silent attribute
    typos. Two instance fields only: `_raw` (the wrapped source; never
    consulted at M2 because callers route reads through the raw source
    and pass the result to adjust_price) and `_cumulative_per_share`.
    """

    __slots__ = ("_raw", "_cumulative_per_share")

    def __init__(self, raw: PitDataSource) -> None:
        self._raw = raw
        self._cumulative_per_share: dict[AssetId, Decimal] = {}

    def apply_permanent_impact(
        self, asset_id: AssetId, per_share_signed: Decimal
    ) -> None:
        """Accumulate signed permanent impact for an asset.

        per_share_signed is a SIGNED dollar amount per share. Buys produce
        positive values (lifts subsequent reads); sells produce negative
        values (lowers subsequent reads). The matcher computes the sign
        upstream; the decorator does not infer direction from a side flag.

        Idempotent at zero: apply_permanent_impact(asset_id, Decimal("0"))
        is a no-op that does NOT allocate a register entry. This means
        the register is NOT a faithful "ever-touched" audit trail; a
        never-traded asset and a round-tripped-to-zero asset are
        indistinguishable via cumulative_for (both return Decimal("0"))
        BUT they differ in the underlying register membership. Callers
        that need "every (asset, fill) the matcher saw" must track that
        separately. The choice is a memory optimization; reversing it
        (allocating an entry for every apply call regardless of value)
        is a single-character change and would not affect any v1
        observable behavior.
        """
        if per_share_signed == Decimal("0"):
            return
        current = self._cumulative_per_share.get(asset_id, Decimal("0"))
        self._cumulative_per_share[asset_id] = current + per_share_signed

    def adjust_price(self, asset_id: AssetId, raw_price: Decimal) -> Decimal:
        """Return raw_price + cumulative_for(asset_id).

        For an asset that has never been traded, the register has no entry
        and the raw price is returned unchanged. For volume reads, the
        BarLoop and matcher MUST bypass the decorator by reading directly
        from the raw source (this method's contract is price-only).
        """
        cumulative = self._cumulative_per_share.get(asset_id, Decimal("0"))
        return raw_price + cumulative

    def cumulative_for(self, asset_id: AssetId) -> Decimal:
        """Read-only accessor for the per-asset cumulative impact.

        Returns Decimal("0") for assets never traded. Used by tests and
        by the BarLoop's snapshot-time mark-to-market when verifying the
        impact-aware valuation path.
        """
        return self._cumulative_per_share.get(asset_id, Decimal("0"))

    def reset(self) -> None:
        """Zero the register.

        Called by Backtest.__init__ at the start of each backtest run so
        the decorator does not carry state across runs. Tests construct a
        fresh ImpactedPriceSource per test; reset() is the alternative
        for callers that want to reuse the decorator across runs.
        """
        self._cumulative_per_share.clear()
