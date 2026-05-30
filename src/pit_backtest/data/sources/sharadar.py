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

M3 PR 1 (#24): read_tickers + read_sf1_arq low-level PIT readers shipped.
M3 PR 2 (this PR): per-row get_price + get_fundamental shipped. Both
consume the lazy `_resolver` cached_property to translate AssetId to
ticker; get_price returns Decimal at the locked boundary precision;
get_fundamental applies the PIT gate `datekey <= available_dt` as the
structural lookahead protection.

Still NotImplementedError as of M3 PR 2: get_corporate_actions and
get_cash_flows (PR 3; discriminated-union dispatch over splits +
dividends + delistings + spinoffs); members_at and get_delisting (PR 4;
alongside SharadarSP500Universe + the IsMemberAt demo).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import polars as pl

from pit_backtest.data.records import (
    AssetId,
    CashFlow,
    CashFlowType,
    CorporateAction,
    SplitAction,
)
from pit_backtest.data.resolver import (
    SharadarPermatickerResolver,
    TickerNotFoundError,
)
from pit_backtest.data.sources.base import PitDataSource
from pit_backtest.data.sources.manifest import (
    SnapshotBundleEntry,
    load_manifest,
    verify_bundle,
)
from pit_backtest.execution.cost.impact import to_boundary_decimal


_LOG = logging.getLogger(__name__)


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

# SF1 PIT (point-in-time) dimensions per ADR 0003 architecture sketch +
# docs/methodology/dataset_versioning.md. "ARQ" = as-reported quarterly,
# "ART" = as-reported trailing-twelve-month, "ARY" = as-reported yearly.
# Sharadar's restated counterparts MRQ / MRT / MRY are explicitly rejected
# at the read_sf1_arq boundary; the dimension column may also be uppercase
# variants ("Arq", "arq") in vendor exports, so the reader normalizes input
# to uppercase before membership check.
_PIT_SF1_DIMENSIONS: frozenset[str] = frozenset({"ARQ", "ART", "ARY"})


def _normalize_pit_dimension(dimension: str) -> str:
    """Uppercase + PIT membership check (Plan-reviewer Low 10 on M3 PR 2).

    Single source of truth so `read_sf1_arq` and `get_fundamental` share
    the same validation. Returns the normalized uppercase dimension.

    Raises:
        ValueError: when the normalized dimension is not in
            `_PIT_SF1_DIMENSIONS`. Sharadar's restated counterparts
            MRQ / MRT / MRY are rejected here.
    """
    dimension_norm = dimension.upper()
    if dimension_norm not in _PIT_SF1_DIMENSIONS:
        raise ValueError(
            f"SF1 dimension {dimension!r} is not PIT; accepted: "
            f"{sorted(_PIT_SF1_DIMENSIONS)}. "
            f"Restated dimensions (MRQ / MRT / MRY) are rejected at load "
            f"per docs/methodology/dataset_versioning.md."
        )
    return dimension_norm


# SEP price fields the engine reads. Mirrors the Literal in the
# PitDataSource Protocol. Per Plan-reviewer Medium 9 a defensive runtime
# check in get_price guards against `cast(PriceField, untrusted)` misuse.
_SEP_PRICE_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume"}
)


# ----- M3 PR 3: Sharadar ACTIONS dispatch (corporate actions + cash flows) -----
#
# Discriminated-union dispatch from Sharadar's ACTIONS string column to the
# typed records in `data.records` per ADR 0003 decision 2 (CashFlow split
# from CorporateAction as two streams) and ADR 0001 decision 8 (v1 covers
# splits, cash dividends, delistings with cash proceeds, spin-offs as cash
# equivalent).
#
# Enumeration source: Sharadar's documented ACTIONS action codes per the
# vendor schema. Real bundles in this repo (verified against
# data/snapshots/sharadar_2026-05-29/actions.parquet on 2026-05-30) carry
# at minimum `dividend`, `listed`, `initiated`; broader pulls include
# `split`, `spinoff`, `delisted`, `transfer`, `tradinghaltresumed`,
# `acquisitionbystock`, `acquisitionbycash`, `acquisitionunknown`,
# `bankruptcyliquidation`, `bankruptcyreorganization`.
#
# Dispatch policy:
# - `_SHARADAR_DISPATCHED_ACTIONS`: v1 portfolio-impacting events that
#   produce SplitAction or CashFlow records.
# - `_SHARADAR_SKIPPED_ACTIONS`: announce-only (`listed`, `initiated`,
#   `delisted`, `tradinghaltresumed`) plus events handled via the TICKERS
#   -derived delisting path (`acquisitionby*`, `bankruptcy*`, `transfer`).
#   Per ADR 0002 decision 16, cash + stock acquisitions and Chapter 11
#   reorganizations are routed through `get_delisting` (which reads SEP
#   `closeunadj` at `lastpricedate` per `docs/methodology/dataset_versioning.md:25`
#   as the cash-flow source-of-truth), not through this ACTIONS dispatch.
# - Anything else: log a warning and skip. Vendor adding a new code mid-2027
#   must NOT crash existing backtests; the warning gives operators
#   visibility, and the dispatch table can be extended in a follow-up.
_SHARADAR_DISPATCHED_ACTIONS: frozenset[str] = frozenset(
    {"dividend", "split", "spinoff"}
)

_SHARADAR_SKIPPED_ACTIONS: frozenset[str] = frozenset(
    {
        # Announce-only (not portfolio-impacting at v1):
        "listed",
        "initiated",
        "delisted",
        "transfer",
        "tradinghaltresumed",
        # Routed via TICKERS-derived delisting path per ADR 0002 dec 16:
        "acquisitionbystock",
        "acquisitionbycash",
        "acquisitionunknown",
        "bankruptcyliquidation",
        "bankruptcyreorganization",
    }
)

# Explicit ordinal for CashFlow sort within `get_cash_flows` so a v1.1
# addition (e.g., `borrow_fee`) does not silently reorder via alphabetical
# tie-breaking. Per ADR 0003 decision 13 the engine applies dividends at
# ex-date and delisting cash at the open of T+1; same-day sorting puts
# dividends BEFORE delisting cash so the bar at T includes the dividend
# and the delisting cash hits the next bar in BarLoop's flow application.
_CASH_FLOW_SORT_ORDINAL: dict[CashFlowType, int] = {
    "cash_dividend": 0,
    "spinoff_cash_equivalent": 1,
    "delisting_cash_proceeds": 2,
}


def _row_date_to_datetime(row_date: date) -> datetime:
    """Promote a Sharadar row's `date` column value to `datetime`.

    Per ADR 0002 decision 11 every Sharadar date is interpreted as the
    end-of-day America/New_York close (16:00 ET). Since the engine's
    timezone convention is naive-datetimes-treated-as-ET throughout
    (`docs/methodology/determinism.md` + `execution.cost.impact._et_date`),
    no tzinfo is attached. The promotion ensures `SplitAction.ex_date`
    and `CashFlow.dt` (both typed `datetime` per `data/records.py`)
    receive consistent values from the cast-before-filter ACTIONS reader.
    """
    return datetime.combine(row_date, time(16, 0))


def _dispatch_action_row(
    row: dict[str, Any], asset_id: AssetId
) -> CashFlow | SplitAction | None:
    """Discriminated dispatch from a Sharadar ACTIONS row to a typed record.

    Returns:
      - `CashFlow` for `dividend` (cash_dividend) and `spinoff`
        (spinoff_cash_equivalent; bias note per ADR 0002 decision 14
        cites Cusatis-Miles-Woolridge 1993 and McConnell-Ovtchinnikov
        2004; v1 ships the cash-equivalent approximation and v1.1 will
        ship share-distribution semantics).
      - `SplitAction` for `split`. `ratio=2.0` is a 2-for-1 forward split;
        `ratio=0.5` is a 1-for-2 reverse split per `data/records.py:74-86`.
      - `None` for skipped action codes (announce-only or TICKERS-routed
        per ADR 0002 dec 16).

    For unknown action codes the helper LOGS A WARNING and returns None
    (does NOT raise). This preserves backward compatibility against
    vendor schema additions; operators see the warning and can extend
    the dispatch tables in a follow-up. The strict-raise alternative
    was rejected per Plan-reviewer's Counter on Choice 1 (a vendor-
    added code should not crash production backtests).
    """
    action: str = row["action"]
    if action in _SHARADAR_DISPATCHED_ACTIONS:
        dt = _row_date_to_datetime(row["date"])
        amount_or_ratio = to_boundary_decimal(float(row["value"]))
        if action == "dividend":
            return CashFlow(
                asset_id=asset_id,
                dt=dt,
                flow_type="cash_dividend",
                amount=amount_or_ratio,
            )
        if action == "split":
            return SplitAction(asset_id=asset_id, ex_date=dt, ratio=amount_or_ratio)
        # action == "spinoff" by elimination
        return CashFlow(
            asset_id=asset_id,
            dt=dt,
            flow_type="spinoff_cash_equivalent",
            amount=amount_or_ratio,
        )
    if action in _SHARADAR_SKIPPED_ACTIONS:
        return None
    _LOG.warning(
        "Sharadar ACTIONS row has unknown action %r (asset_id=%d, date=%s); "
        "skipping. Extend _SHARADAR_DISPATCHED_ACTIONS or "
        "_SHARADAR_SKIPPED_ACTIONS in pit_backtest.data.sources.sharadar "
        "to dispatch or document this code explicitly.",
        action,
        int(asset_id),
        row["date"],
    )
    return None


class DelistingDataQualityError(ValueError):
    """Raised when a TICKERS-reported delisting lacks a recoverable SEP price.

    Per ADR 0002 decision 16 the v1 delisting cash proceeds come from
    SEP `closeunadj` at `lastpricedate` (per
    `docs/methodology/dataset_versioning.md:25` as the cash-flow
    reconstruction source-of-truth). Two failure modes are vendor data
    quality bugs that we refuse to silently substitute:

    1. TICKERS reports `isdelisted=='Y'` AND `lastpricedate is not None`
       but the SEP table has no row at that date for the resolved ticker.
    2. SEP has a row at `lastpricedate` but the `closeunadj` cell is
       NULL. Ambiguous between Chapter-11-at-zero and a missing-data
       bug; refuse rather than guess.

    Note on Chapter 11: ADR 0002 decision 16 commits the v1 baseline
    "Chapter 11 reorgs treated as a delisting at zero with a documented
    bias note". The closeunadj-at-lastpricedate path captures the actual
    last-traded price for bankruptcy-route delistings, which typically
    overstates the realized proceeds (the bias). Operators who need the
    explicit-zero baseline can dispatch off the `bankruptcyreorganization`
    ACTIONS code in v1.1; v1 ships the SEP-based approximation.
    """


class PriceNotFoundError(KeyError):
    """Raised when get_price has no SEP row at the requested (asset, dt).

    Per Plan-reviewer Low 11 on M3 PR 2 placed directly above the
    SharadarDataSource class for grep-ability. Symmetric with
    TickerNotFoundError; inherits from KeyError so a caller that broad-
    catches `KeyError` will catch both. Canonical failure modes:
    weekend / holiday, pre-IPO, post-delisting (when post-delisting also
    falls outside the resolver's interval, TickerNotFoundError fires
    first; PriceNotFoundError applies when the asset is in the resolver
    index but the SEP table has no bar at the requested date).
    """


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

    @cached_property
    def _resolver(self) -> SharadarPermatickerResolver:
        """Lazy-built permaticker resolver; constructed on first per-row call.

        Per Plan-reviewer's Ratified Choice 1 on M3 PR 2: lazy via
        `cached_property` so the M1 demos (`read_sep_prices`,
        `read_actions_dividends`) that never touch per-row paths pay
        nothing for the resolver. Users who call `get_price` or
        `get_fundamental` pay the index build once (one pass over the
        TICKERS LazyFrame, ~25k rows on real Sharadar) and amortize
        across every subsequent per-row call. External callers who want
        to share a resolver across data sources construct one themselves
        via `SharadarPermatickerResolver(source)`; this cached property
        exists only to make `get_price` / `get_fundamental` self-contained.
        """
        return SharadarPermatickerResolver(self)

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

    def read_actions(
        self,
        *,
        ticker: str | None = None,
        start_dt: date | datetime | None = None,
        end_dt: date | datetime | None = None,
        action_filter: frozenset[str] | None = None,
    ) -> pl.DataFrame:
        """M3 PR 3: general ACTIONS reader (corp events + cash flows).

        Generalizes the M1 `read_actions_dividends` so the per-row
        PitDataSource methods (`get_corporate_actions`, `get_cash_flows`)
        can dispatch over the full Sharadar action set rather than the
        dividend subset. The M1 reader stays for backward compatibility
        with the SPY TR reconstruction demo.

        Args:
          ticker: optional ticker filter.
          start_dt: lower bound on ACTIONS `date` (inclusive).
          end_dt: upper bound on ACTIONS `date` (inclusive).
          action_filter: optional allowlist of action codes; rows whose
            `action` is not in this set are dropped at the reader.
            When None, all action codes pass through.

        Returns a Polars frame with columns: ticker, date, action, value.
        Cast-before-filter on `date` per project rule 12 (the M1 hotfix
        contract; see `read_sep_prices` for the full rationale).
        Sorted by (ticker, date, action) for determinism.
        """
        lf = self.get_table("actions").with_columns(
            pl.col("date").cast(pl.Date)
        )
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if start_dt is not None:
            lf = lf.filter(pl.col("date") >= _to_date(start_dt))
        if end_dt is not None:
            lf = lf.filter(pl.col("date") <= _to_date(end_dt))
        if action_filter is not None:
            lf = lf.filter(pl.col("action").is_in(list(action_filter)))

        df = lf.select(
            pl.col("ticker"),
            pl.col("date"),
            pl.col("action"),
            pl.col("value").cast(pl.Float64),
        ).sort(["ticker", "date", "action"]).collect()

        return df

    def read_tickers(
        self,
        *,
        ticker: str | None = None,
        permaticker: int | None = None,
        active_at: date | datetime | None = None,
    ) -> pl.DataFrame:
        """M3 PR 1: load Sharadar TICKERS rows for resolver / universe wiring.

        Returns a Polars frame with the documented column subset:
        permaticker, ticker, name, exchange, isdelisted, firstpricedate,
        lastpricedate, firstquarter, lastquarter, cusip. The four date
        columns are cast to pl.Date BEFORE filtering per project rule 12
        (the M1 hotfix at fix/adapter-date-filter-and-pandas-pin).

        Args:
          ticker: optional ticker filter.
          permaticker: optional permaticker filter (the AssetId carrier).
          active_at: when set, returns only rows whose
            [firstpricedate, lastpricedate] interval contains active_at,
            treating null lastpricedate as right-unbounded (active through
            now). Matches the resolver interval convention.

        Sorted by (permaticker, firstpricedate) for determinism.
        """
        lf = self.get_table("tickers").with_columns(
            pl.col("firstpricedate").cast(pl.Date),
            pl.col("lastpricedate").cast(pl.Date),
            pl.col("firstquarter").cast(pl.Date),
            pl.col("lastquarter").cast(pl.Date),
        )
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if permaticker is not None:
            lf = lf.filter(pl.col("permaticker") == permaticker)
        if active_at is not None:
            active_at_date = _to_date(active_at)
            lf = lf.filter(
                (pl.col("firstpricedate") <= active_at_date)
                & (
                    pl.col("lastpricedate").is_null()
                    | (pl.col("lastpricedate") >= active_at_date)
                )
            )

        df = lf.select(
            pl.col("permaticker").cast(pl.Int64),
            pl.col("ticker"),
            pl.col("name"),
            pl.col("exchange"),
            pl.col("isdelisted"),
            pl.col("firstpricedate"),
            pl.col("lastpricedate"),
            pl.col("firstquarter"),
            pl.col("lastquarter"),
            pl.col("cusip"),
        ).sort(["permaticker", "firstpricedate"]).collect()

        return df

    def read_sf1_arq(
        self,
        *,
        ticker: str | None = None,
        datekey_start: date | datetime | None = None,
        datekey_end: date | datetime | None = None,
        dimension: str = "ARQ",
    ) -> pl.DataFrame:
        """M3 PR 1: load Sharadar SF1 fundamentals filtered to a PIT dimension.

        Per docs/methodology/dataset_versioning.md and ADR 0003 architecture
        sketch, only the as-reported dimensions ARQ / ART / ARY are PIT.
        Sharadar's restated counterparts MRQ / MRT / MRY are explicitly
        rejected at this boundary; the engine never reads them.

        Args:
          ticker: optional ticker filter (string at M3 PR 1; per-asset
            wiring lands when get_fundamental does in a subsequent M3 PR).
          datekey_start: lower bound on datekey (SEC submission date;
            this is the available_dt for SF1 records).
          datekey_end: upper bound on datekey.
          dimension: PIT flavor; case-insensitive input is normalized to
            uppercase before membership check. Defaults to ARQ.

        Returns the full SF1 column set unchanged after the dimension and
        date filters. Per-field columns (revenue, netinc, eps, sharesbas,
        etc.) flow through unchanged; per-row Decimal coercion happens
        when get_fundamental wires in.

        Sorted by (ticker, datekey, calendardate) for determinism. The
        column-order of the returned frame is the vendor parquet order;
        callers that need a specific column order should select explicitly.

        Raises:
          ValueError: when dimension (after uppercase normalization) is
            not in _PIT_SF1_DIMENSIONS.
        """
        dimension_norm = _normalize_pit_dimension(dimension)

        lf = self.get_table("sf1").with_columns(
            pl.col("calendardate").cast(pl.Date),
            pl.col("datekey").cast(pl.Date),
            pl.col("reportperiod").cast(pl.Date),
        ).filter(pl.col("dimension") == dimension_norm)
        if ticker is not None:
            lf = lf.filter(pl.col("ticker") == ticker)
        if datekey_start is not None:
            lf = lf.filter(pl.col("datekey") >= _to_date(datekey_start))
        if datekey_end is not None:
            lf = lf.filter(pl.col("datekey") <= _to_date(datekey_end))

        df = lf.sort(["ticker", "datekey", "calendardate"]).collect()
        return df

    # ----- Per-row PitDataSource protocol -----
    # M3 PR 2 shipped: get_price + get_fundamental real implementations
    # consuming the lazy `_resolver` cached_property and the PR 1 readers.
    # M3 PR 3+ remaining: get_corporate_actions + get_cash_flows
    # (discriminated-union dispatch); members_at + get_delisting (alongside
    # SharadarSP500Universe + IsMemberAt demo). M1 demos continue to drive
    # the data via the M1 convenience methods (read_sep_prices,
    # read_actions_dividends) which bypass the per-row path.

    def get_price(
        self,
        asset_id: AssetId,
        dt: datetime,
        field: Literal["open", "high", "low", "close", "volume"],
    ) -> Decimal:
        """Per-row SEP price read for the BarLoop's per-asset-per-bar dispatch.

        Resolves asset_id -> ticker at dt via the lazy resolver, then reads
        the SEP row at exact dt for the requested field. Returns Decimal at
        the locked boundary precision per `pydantic_polars_boundary.md`.

        Raises:
            TickerNotFoundError: asset_id is not in the resolver index at
                dt, or dt is outside the asset's
                [firstpricedate, lastpricedate] interval (pre-IPO,
                post-delisting).
            PriceNotFoundError: asset_id is in the resolver index but the
                SEP table has no bar at the requested date (weekend,
                holiday, vendor gap), or the requested field is NULL on
                the row.
            ValueError: field is not in the SEP price field set
                (defensive runtime guard against `cast(PriceField, ...)`
                misuse; mypy strict catches static typos).
            ValueError: SEP returned more than one row for the
                (ticker, date) pair (vendor data-quality bug).
        """
        if field not in _SEP_PRICE_FIELDS:
            raise ValueError(
                f"SEP field {field!r} is not a price field; accepted: "
                f"{sorted(_SEP_PRICE_FIELDS)}"
            )
        ticker = self._resolver.get_ticker(asset_id, dt)
        lookup_date = _to_date(dt)
        df = (
            self.get_table("sep")
            .with_columns(pl.col("date").cast(pl.Date))
            .filter(pl.col("ticker") == ticker)
            .filter(pl.col("date") == lookup_date)
            .collect()
        )
        if df.height == 0:
            raise PriceNotFoundError(
                f"no SEP row for asset_id={int(asset_id)} ticker={ticker!r} "
                f"at dt={lookup_date.isoformat()}"
            )
        if df.height > 1:
            raise ValueError(
                f"SEP returned {df.height} rows for asset_id={int(asset_id)} "
                f"ticker={ticker!r} at dt={lookup_date.isoformat()}; "
                f"expected exactly 1. Vendor data-quality bug; refuse to "
                f"silently pick one."
            )
        value = df[field][0]
        if value is None:
            raise PriceNotFoundError(
                f"SEP row for asset_id={int(asset_id)} ticker={ticker!r} "
                f"at dt={lookup_date.isoformat()} has NULL {field!r}"
            )
        if field == "volume":
            # Volume is Int64 in SEP. Decimal-from-int is exact at any
            # magnitude (no float intermediate; no 2**53 ceiling per
            # Plan-reviewer Medium 8). The cast asserts the runtime type.
            return Decimal(int(value))
        return to_boundary_decimal(float(value))

    def get_fundamental(
        self,
        asset_id: AssetId,
        available_dt: datetime,
        field: str,
        flavor: Literal["ARQ", "ART", "ARY"],
    ) -> Decimal | None:
        """Per-row SF1 fundamental read with PIT discipline.

        Returns the most recent SF1 row observable as of `available_dt`
        (the strict PIT filter `datekey <= available_dt` enforces the
        dual-timestamp contract; no leak possible). The
        (datekey DESC, calendardate DESC) tiebreaker picks the more
        current as-reported snapshot when two rows share datekey per
        Plan-reviewer's Ratified Choice 3.

        Returns None when:
            - The asset has no SF1 row at this flavor whose datekey is
              <= available_dt (pre-filing, or the asset is new and has
              no history yet).
            - The most recent observable row has NULL in the requested
              field (vendor reported the row but did not populate the
              field for this asset).

        Raises:
            TickerNotFoundError: asset_id is not in the resolver index
                at available_dt.
            ValueError: flavor (after uppercase normalization) is not in
                `_PIT_SF1_DIMENSIONS` (rejects MRQ / MRT / MRY).
            ValueError: field is not a column in the SF1 table.
        """
        flavor_norm = _normalize_pit_dimension(flavor)
        ticker = self._resolver.get_ticker(asset_id, available_dt)
        available_date = _to_date(available_dt)
        lf = (
            self.get_table("sf1")
            .with_columns(
                pl.col("calendardate").cast(pl.Date),
                pl.col("datekey").cast(pl.Date),
                pl.col("reportperiod").cast(pl.Date),
            )
            .filter(pl.col("ticker") == ticker)
            .filter(pl.col("dimension") == flavor_norm)
            .filter(pl.col("datekey") <= available_date)
        )
        available_columns = lf.collect_schema().names()
        if field not in available_columns:
            raise ValueError(
                f"SF1 field {field!r} is not a column in the bundle's sf1 "
                f"table; available columns: {sorted(available_columns)}"
            )
        df = (
            lf.sort(["datekey", "calendardate"], descending=True)
            .head(1)
            .collect()
        )
        if df.height == 0:
            return None
        value = df[field][0]
        if value is None:
            return None
        return to_boundary_decimal(float(value))

    def get_corporate_actions(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CorporateAction]:
        """Per-row ACTIONS read returning v1 corporate actions in range.

        At v1 the only `CorporateAction` v1 produces from Sharadar is
        `SplitAction` (per ADR 0001 dec 8 + ADR 0002 dec 16). Cash
        dividends and spinoff-as-cash flow through `get_cash_flows`;
        stock-for-stock acquisitions are cash-equivalent at v1 per
        ADR 0002 dec 16 (they route through `get_delisting`).

        For symmetry with the Protocol return type the implementation
        also checks `get_delisting` for a `CorporateAction` result and
        includes it when `ex_date` falls in `[start_dt, end_dt]`. At v1
        this arm is unreachable (the v1 delisting path always returns
        `CashFlow`); v1.1's `DelistingStockAcquisitionAction` dispatch
        will activate this branch without contract churn.

        Range semantics: `[start_dt, end_dt]` inclusive on both ends.
        Sorted by `ex_date` ascending (Determinism Requirement 3).
        """
        ticker = self._resolver.get_ticker(asset_id, start_dt)
        # NO action_filter at the reader: unknown codes must reach
        # _dispatch_action_row so the WARN-and-skip path surfaces vendor
        # schema additions (Plan-reviewer Counter on Choice 1). A filter
        # at this level would silently lose visibility into new codes.
        df = self.read_actions(
            ticker=ticker, start_dt=start_dt, end_dt=end_dt
        )
        actions: list[CorporateAction] = []
        for row in df.iter_rows(named=True):
            dispatched = _dispatch_action_row(row, asset_id)
            if isinstance(dispatched, SplitAction):
                actions.append(dispatched)
        # Symmetric stock-acquisition arm; unreachable at v1, primed for v1.1.
        delisting = self.get_delisting(asset_id)
        if (
            delisting is not None
            and not isinstance(delisting, CashFlow)
            and _to_date(start_dt) <= delisting.ex_date.date() <= _to_date(end_dt)
        ):
            actions.append(delisting)
        return sorted(actions, key=lambda a: a.ex_date)

    def get_cash_flows(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CashFlow]:
        """Per-row ACTIONS read returning v1 cash flows in range.

        Dispatches dividend (-> cash_dividend CashFlow) and spinoff
        (-> spinoff_cash_equivalent CashFlow with the CMW1993 + MO2004
        bias note per ADR 0002 dec 14). The TICKERS-derived delisting
        record is appended when `lastpricedate in [start_dt, end_dt]`.

        Range semantics: `[start_dt, end_dt]` inclusive on both ends.
        Sorted by `(dt, _CASH_FLOW_SORT_ORDINAL[flow_type])`. The
        explicit ordinal (cash_dividend < spinoff_cash_equivalent <
        delisting_cash_proceeds) honors ADR 0003 decision 13's "dividends
        applied at ex-date T, delisting cash credits open of T+1"
        ordering even when the engine groups same-day flows.
        """
        ticker = self._resolver.get_ticker(asset_id, start_dt)
        # NO action_filter at the reader: unknown codes must reach
        # _dispatch_action_row so the WARN-and-skip path surfaces vendor
        # schema additions (Plan-reviewer Counter on Choice 1). A filter
        # at this level would silently lose visibility into new codes.
        df = self.read_actions(
            ticker=ticker, start_dt=start_dt, end_dt=end_dt
        )
        flows: list[CashFlow] = []
        for row in df.iter_rows(named=True):
            dispatched = _dispatch_action_row(row, asset_id)
            if isinstance(dispatched, CashFlow):
                flows.append(dispatched)
        delisting = self.get_delisting(asset_id)
        if isinstance(delisting, CashFlow):
            if _to_date(start_dt) <= delisting.dt.date() <= _to_date(end_dt):
                flows.append(delisting)
        return sorted(
            flows,
            key=lambda f: (f.dt, _CASH_FLOW_SORT_ORDINAL[f.flow_type]),
        )

    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        raise NotImplementedError("M3 deliverable")

    def get_delisting(
        self, asset_id: AssetId
    ) -> CashFlow | CorporateAction | None:
        """TICKERS-derived delisting record per ADR 0002 decision 16.

        Returns:
          - `CashFlow(flow_type="delisting_cash_proceeds")` when TICKERS
            reports `isdelisted=='Y'` and `lastpricedate is not None`,
            with `amount = closeunadj at lastpricedate` per
            `docs/methodology/dataset_versioning.md:25` (the v1
            cash-flow reconstruction source-of-truth).
          - `None` when the asset is still active (`isdelisted=='N'` or
            `lastpricedate is None`).

        At v1 stock-for-stock acquisitions are routed through this path
        as cash-equivalent at the announced deal price (which Sharadar
        records as the SEP `closeunadj` at `lastpricedate`); the
        explicit `DelistingStockAcquisitionAction` shape is deferred to
        v1.1 per ADR 0002 dec 16.

        Raises:
          - `TickerNotFoundError` when `asset_id` is not in the resolver
            index (no TICKERS rows for the permaticker at all).
          - `DelistingDataQualityError` when TICKERS says delisted but
            SEP has no row at `lastpricedate`, or SEP has the row but
            `closeunadj` is NULL. Both are vendor data-quality bugs;
            refuse to silently substitute.
          - `ValueError` when SEP returns multiple rows for
            (ticker, lastpricedate); vendor uniqueness invariant
            violated.
        """
        if not self._resolver.contains(asset_id):
            raise TickerNotFoundError(
                f"asset_id {int(asset_id)} not in resolver index"
            )
        tickers_df = self.read_tickers(permaticker=int(asset_id))
        if tickers_df.height == 0:
            raise TickerNotFoundError(
                f"asset_id {int(asset_id)} has no TICKERS row"
            )
        # Multi-row case: an asset with ticker history has multiple rows.
        # Sort lastpricedate ascending with NULLS LAST (still-active rows
        # last); the final row carries the canonical delisting metadata.
        last_row = (
            tickers_df.sort(
                ["lastpricedate"], descending=False, nulls_last=True
            )
            .tail(1)
            .row(0, named=True)
        )
        if last_row["isdelisted"] != "Y" or last_row["lastpricedate"] is None:
            return None
        lastpricedate = last_row["lastpricedate"]
        ticker = last_row["ticker"]
        sep_df = (
            self.get_table("sep")
            .with_columns(pl.col("date").cast(pl.Date))
            .filter(pl.col("ticker") == ticker)
            .filter(pl.col("date") == lastpricedate)
            .collect()
        )
        if sep_df.height == 0:
            raise DelistingDataQualityError(
                f"asset_id {int(asset_id)} (ticker={ticker!r}) delisted on "
                f"{lastpricedate.isoformat()} per TICKERS but SEP has no row "
                f"at that date; vendor data-quality bug, refuse to silently "
                f"substitute the previous trading-day close."
            )
        if sep_df.height > 1:
            raise ValueError(
                f"SEP returned {sep_df.height} rows for ticker={ticker!r} at "
                f"{lastpricedate.isoformat()}; expected exactly 1. Vendor "
                f"data-quality bug; refuse to silently pick one."
            )
        closeunadj = sep_df["closeunadj"][0]
        if closeunadj is None:
            raise DelistingDataQualityError(
                f"asset_id {int(asset_id)} (ticker={ticker!r}) delisted on "
                f"{lastpricedate.isoformat()} has NULL closeunadj; ambiguous "
                f"between Chapter-11-at-zero (v1.1 dispatch) and vendor bug; "
                f"refuse to silently substitute zero."
            )
        amount = to_boundary_decimal(float(closeunadj))
        return CashFlow(
            asset_id=asset_id,
            dt=_row_date_to_datetime(lastpricedate),
            flow_type="delisting_cash_proceeds",
            amount=amount,
        )


def _to_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value
