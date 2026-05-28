"""Universe protocol and v1 SharadarSP500Universe.

Per ADR 0001 decision 9 and ADR 0003 architecture sketch: typed PIT
membership API with is_member, members_at, and membership_spells. Backed
at v1 by the Sharadar SP500 event log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pit_backtest.data.records import AssetId


class Universe(Protocol):
    """Point-in-time universe membership API."""

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        """True if asset_id was a member of this universe at dt."""
        ...

    def members_at(self, dt: datetime) -> list[AssetId]:
        """Every asset_id that was a member at dt. Sorted for determinism."""
        ...

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime]]:
        """Every (start_dt, end_dt) interval during which asset_id was a member."""
        ...


class SharadarSP500Universe:
    """v1 Universe backed by the Sharadar SP500 event log.

    Validates at backtest construction that every asset has either a
    delisting record or an active-status confirmation across each
    membership spell. Gaps raise UniverseValidationError per ADR 0002
    decision 12.
    """

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        raise NotImplementedError("M3 deliverable")

    def members_at(self, dt: datetime) -> list[AssetId]:
        raise NotImplementedError("M3 deliverable")

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime]]:
        raise NotImplementedError("M3 deliverable")


class UniverseValidationError(ValueError):
    """Raised when a Universe instance fails its construction-time checks."""
