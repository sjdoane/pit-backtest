"""Sharadar adapter: SEP + SF1 + TICKERS + SP500.

The M1 deliverable is SEP (prices + dividends + delistings). SF1 + TICKERS
+ SP500 land in M3.

Per docs/methodology/dataset_versioning.md, the adapter reads from a
SHA256-verified snapshot bundle; the manifest is consulted at construction
to refuse loading if any file has been modified since the manifest was
last updated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import polars as pl

from pit_backtest.data.records import AssetId, CashFlow, CorporateAction
from pit_backtest.data.sources.base import PitDataSource


class SharadarDataSource(PitDataSource):
    """v1 implementation of PitDataSource backed by Sharadar parquet snapshots."""

    def __init__(self, snapshot_bundle: str, snapshots_root: Path) -> None:
        raise NotImplementedError("M1 deliverable")

    def get_price(
        self,
        asset_id: AssetId,
        dt: datetime,
        field: Literal["open", "high", "low", "close", "volume"],
    ) -> Decimal:
        raise NotImplementedError("M1 deliverable")

    def get_fundamental(
        self,
        asset_id: AssetId,
        available_dt: datetime,
        field: str,
        flavor: Literal["ARQ", "ART", "ARY"],
    ) -> Decimal | None:
        raise NotImplementedError("M3 deliverable")

    def get_corporate_actions(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CorporateAction]:
        raise NotImplementedError("M3 deliverable")

    def get_cash_flows(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CashFlow]:
        raise NotImplementedError("M1 deliverable (SPY dividends required for reconciliation)")

    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        raise NotImplementedError("M3 deliverable")

    def get_delisting(
        self, asset_id: AssetId
    ) -> CashFlow | CorporateAction | None:
        raise NotImplementedError("M3 deliverable")

    def get_table(self, table_name: str) -> pl.LazyFrame:
        raise NotImplementedError("M1 deliverable")
