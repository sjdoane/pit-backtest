"""Identifier resolution: ticker history to persistent AssetId.

Per ADR 0003 decision 8, IdentifierResolver is a separate protocol so the
AssetId NewType is not locked to Sharadar permatickers. v2 can add other
resolvers (CRSP PERMNO, FactSet PermID) without changing AssetId itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pit_backtest.data.records import AssetId


class IdentifierResolver(Protocol):
    """Resolve between ticker-at-date and persistent AssetId."""

    def resolve_ticker(self, ticker: str, dt: datetime) -> AssetId:
        """Return the AssetId that owned this ticker at dt.

        Raises TickerNotFoundError if no asset owned the ticker at that
        date. Ticker reuse after delisting is handled by returning the
        asset that owned the ticker as of dt.
        """
        ...

    def get_ticker(self, asset_id: AssetId, dt: datetime) -> str:
        """Return the ticker an asset was trading under at dt.

        Raises TickerNotFoundError if the asset had no listed ticker on dt
        (pre-IPO or post-delisting).
        """
        ...


class SharadarPermatickerResolver:
    """v1 resolver backed by the Sharadar TICKERS table.

    Sharadar's permaticker is the AssetId carrier. The ticker history
    table records (permaticker, ticker, firstpricedate, lastpricedate)
    triples; the resolver indexes them for both directions of lookup.
    """

    def resolve_ticker(self, ticker: str, dt: datetime) -> AssetId:
        raise NotImplementedError("M3 deliverable")

    def get_ticker(self, asset_id: AssetId, dt: datetime) -> str:
        raise NotImplementedError("M3 deliverable")


class TickerNotFoundError(KeyError):
    """Raised when a ticker-date or asset-date lookup has no result."""
