"""Sharadar adapter: SEP + ACTIONS + SF1 + TICKERS + SP500.

The M1 deliverable is SEP (prices) and ACTIONS (dividends; full corp
events come in M3). SF1 + TICKERS + SP500 adapters land in M3.

Per docs/methodology/dataset_versioning.md, the adapter reads from a
SHA256-verified snapshot bundle; the manifest is consulted at construction
and refuses to load if any file has been modified since the manifest was
last updated.

M1 day 1 scope:
- __init__ verifies the bundle against the manifest and lazy-scans each
  parquet table.
- read_sep_prices and read_actions_dividends are vendor-specific
  convenience methods that drive the M1 SPY TR reconstruction. They
  return Polars frames keyed by ticker (string), bypassing the full
  AssetId resolution which lands in M3 with the TICKERS adapter.
- get_table is the forward-compatibility seam from ADR 0003 decision 9.
- The per-row PitDataSource methods (get_price, get_cash_flows,
  get_fundamental, members_at, get_delisting) remain NotImplementedError;
  M3 wires them when IdentifierResolver and the data quality contracts
  land.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import polars as pl

from pit_backtest.data.records import AssetId, CashFlow, CorporateAction
from pit_backtest.data.sources.base import PitDataSource
from pit_backtest.data.sources.manifest import (
    SnapshotBundleEntry,
    load_manifest,
    verify_bundle,
)


# Sharadar SEP column schema (subset used at M1). The vendor publishes lowercase
# column names; the adapter does not rename inside the LazyFrame. Translation
# to engine PriceRecord field names happens at the PydanticPriceRecord boundary
# in M3 (per docs/methodology/pydantic_polars_boundary.md).
_SEP_FILENAME = "sep.parquet"
_ACTIONS_FILENAME = "actions.parquet"
_SF1_FILENAME = "sf1.parquet"
_TICKERS_FILENAME = "tickers.parquet"
_SP500_FILENAME = "sp500.parquet"

_TABLE_FILENAMES = {
    "sep": _SEP_FILENAME,
    "actions": _ACTIONS_FILENAME,
    "sf1": _SF1_FILENAME,
    "tickers": _TICKERS_FILENAME,
    "sp500": _SP500_FILENAME,
}


class SharadarDataSource(PitDataSource):
    """v1 implementation of PitDataSource backed by Sharadar parquet snapshots."""

    def __init__(self, snapshot_bundle: str, snapshots_root: Path) -> None:
        self._bundle_name = snapshot_bundle
        self._snapshots_root = snapshots_root.resolve()
        manifest_path = self._snapshots_root / "manifest.toml"
        self._manifest = load_manifest(manifest_path)
        verify_bundle(snapshot_bundle, self._snapshots_root, self._manifest)
        self._bundle_dir = self._snapshots_root / snapshot_bundle
        self._lazy_cache: dict[str, pl.LazyFrame] = {}

    @property
    def bundle_name(self) -> str:
        return self._bundle_name

    @property
    def bundle_entry(self) -> SnapshotBundleEntry:
        return self._manifest[self._bundle_name]

    def get_table(self, table_name: str) -> pl.LazyFrame:
        """Return the LazyFrame for a named Sharadar table.

        table_name is one of: 'sep', 'actions', 'sf1', 'tickers', 'sp500'.
        The LazyFrame is cached per name; subsequent calls return the same
        instance (Polars LazyFrames are immutable so sharing is safe).

        Raises KeyError for unknown table names. Raises FileNotFoundError if
        the bundle does not contain the expected parquet file (the manifest
        verification at __init__ already catches this for files declared in
        the manifest; this fires for tables the bundle did not include).
        """
        if table_name not in _TABLE_FILENAMES:
            raise KeyError(
                f"unknown Sharadar table {table_name!r}; "
                f"available: {sorted(_TABLE_FILENAMES.keys())}"
            )

        filename = _TABLE_FILENAMES[table_name]
        if filename not in self._lazy_cache:
            path = self._bundle_dir / filename
            if not path.is_file():
                raise FileNotFoundError(
                    f"bundle {self._bundle_name!r} missing {filename} at {path}; "
                    f"the manifest should have caught this at construction"
                )
            self._lazy_cache[filename] = pl.scan_parquet(path)

        return self._lazy_cache[filename]

    def read_sep_prices(
        self,
        ticker: str | None = None,
        start_dt: date | datetime | None = None,
        end_dt: date | datetime | None = None,
    ) -> pl.DataFrame:
        """M1 convenience: load SEP prices for a ticker over a date range.

        Returns a Polars frame with columns: dt, open, high, low, close,
        closeunadj, volume. Sorted by dt for determinism (per
        docs/methodology/determinism.md Requirement 3).

        For M1 day 1 the adapter takes a ticker (string) rather than an
        AssetId because the IdentifierResolver lands in M3. The buy-and-hold
        SPY demo and the constant-weight SPY/AGG/GLD demo use this method
        directly; the full per-row PitDataSource.get_price path is M3 work.
        """
        lf = self.get_table("sep")
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if start_dt is not None:
            lf = lf.filter(pl.col("date") >= _to_date(start_dt))
        if end_dt is not None:
            lf = lf.filter(pl.col("date") <= _to_date(end_dt))

        df = lf.select(
            # Cast to pl.Date so downstream `iter_rows` yields python date
            # objects, not datetime. Nasdaq Data Link's SDK returns pandas
            # datetime64[ns] which polars converts to pl.Datetime; without
            # this cast the (asset_id, dt) price-index keys in the BarLoop
            # become (int, datetime) while lookups use date(), so every
            # lookup returns None and the constant-weight demo silently
            # never rebalances.
            pl.col("date").cast(pl.Date).alias("dt"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("closeunadj").cast(pl.Float64),
            pl.col("volume").cast(pl.Int64),
        ).sort("dt").collect()

        return df

    def read_actions_dividends(
        self,
        ticker: str | None = None,
        start_dt: date | datetime | None = None,
        end_dt: date | datetime | None = None,
    ) -> pl.DataFrame:
        """M1 convenience: load dividends for a ticker over a date range.

        Filters Sharadar ACTIONS to `action == "dividend"`. Returns a Polars
        frame with columns: ex_date, amount_per_share. Sorted by ex_date.

        ACTIONS row schema (vendor): ticker, date, action, value, ...
        For dividends, `date` is the ex-dividend date and `value` is the
        per-share cash amount. Spin-offs (action == "spinoff" with value
        as cash-equivalent), delisting cash, and stock-for-stock acquisitions
        flow through M3.
        """
        lf = self.get_table("actions").filter(pl.col("action") == "dividend")
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if start_dt is not None:
            lf = lf.filter(pl.col("date") >= _to_date(start_dt))
        if end_dt is not None:
            lf = lf.filter(pl.col("date") <= _to_date(end_dt))

        df = lf.select(
            # Same date-cast rationale as read_sep_prices: nasdaq-data-link
            # returns pl.Datetime via pandas; without the cast, ex_date keys
            # in the reference + engine dividend index lose their date-vs-
            # datetime equivalence and dividend credits silently never fire.
            pl.col("date").cast(pl.Date).alias("ex_date"),
            pl.col("value").cast(pl.Float64).alias("amount_per_share"),
        ).sort("ex_date").collect()

        return df

    # ----- Full PitDataSource protocol (M3 work) -----
    # The per-row methods below require IdentifierResolver (M3) for the
    # AssetId -> ticker lookup. M1 demos drive the data via the convenience
    # methods above.

    def get_price(
        self,
        asset_id: AssetId,
        dt: datetime,
        field: Literal["open", "high", "low", "close", "volume"],
    ) -> Decimal:
        raise NotImplementedError("M3 deliverable (needs IdentifierResolver)")

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
        raise NotImplementedError("M3 deliverable (needs IdentifierResolver)")

    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        raise NotImplementedError("M3 deliverable")

    def get_delisting(
        self, asset_id: AssetId
    ) -> CashFlow | CorporateAction | None:
        raise NotImplementedError("M3 deliverable")


def _to_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value
