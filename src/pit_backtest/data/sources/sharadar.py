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
        # Cast `date` to pl.Date BEFORE filtering. nasdaq-data-link returns
        # pandas datetime64[ns]; with the project's correct pinned
        # pandas==2.2.3, pl.from_pandas surfaces this as
        # Datetime(time_unit='ns'). Under Polars 1.41.1 a Python
        # `date(2999, 12, 31)` literal OVERFLOWS Datetime[ns]'s i64-ns-since-
        # epoch representable range (~1677-2262); the literal silently
        # saturates and `pl.col('date') <= date(2999, 12, 31)` returns zero
        # rows, NOT an error. The wide-open coverage probe in
        # reconcile_spy_trailing uses exactly that upper-bound literal, so
        # the pre-hotfix cast-after-filter pattern produced "bundle has no
        # SPY rows" on a parquet that contained 7126 SPY rows.
        #
        # A prior transient `pandas==3.0.3` in uv.lock accidentally returned
        # a nullable-date dtype which polars surfaced as pl.Date directly,
        # which made the cast-after-filter pattern silently work. Pinning to
        # the correct 2.2.3 restored the standard Datetime[ns] shape and
        # exposed the latent bug. The fix here normalizes the column dtype
        # FIRST so the filter operates on pl.Date (which has a wider
        # representable range and accepts the wide-open literal).
        #
        # Regression test:
        # tests/data/test_sharadar_adapter.py::test_date_range_filter_works_on_datetime_typed_input
        # uses an explicit pl.Datetime(time_unit="ns") override (bare
        # pl.Datetime defaults to "us" which would not overflow at 2999-12-31)
        # and asserts the on-disk dtype is ns. The previous regression test
        # asserted only the RETURN dtype (pl.Date), not that the FILTER
        # produced non-empty rows.
        lf = self.get_table("sep").with_columns(
            pl.col("date").cast(pl.Date)
        )
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if start_dt is not None:
            lf = lf.filter(pl.col("date") >= _to_date(start_dt))
        if end_dt is not None:
            lf = lf.filter(pl.col("date") <= _to_date(end_dt))

        df = lf.select(
            pl.col("date").alias("dt"),
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
        # Same cast-before-filter contract as read_sep_prices; see comment
        # there for the pandas-pin / Datetime-vs-Date silent-empty rationale.
        lf = (
            self.get_table("actions")
            .with_columns(pl.col("date").cast(pl.Date))
            .filter(pl.col("action") == "dividend")
        )
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if start_dt is not None:
            lf = lf.filter(pl.col("date") >= _to_date(start_dt))
        if end_dt is not None:
            lf = lf.filter(pl.col("date") <= _to_date(end_dt))

        df = lf.select(
            pl.col("date").alias("ex_date"),
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
