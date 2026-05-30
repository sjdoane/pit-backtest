"""Identifier resolution: ticker history to persistent AssetId.

Per ADR 0003 decision 8, IdentifierResolver is a separate protocol so the
AssetId NewType is not locked to Sharadar permatickers. v2 can add other
resolvers (CRSP PERMNO, FactSet PermID) without changing AssetId itself.

The v1 implementation, SharadarPermatickerResolver, indexes the Sharadar
TICKERS table at construction. Production callers construct it from a
SharadarDataSource so the snapshot SHA256 commitment in
docs/methodology/dataset_versioning.md remains the gate that pins which
vintage of TICKERS the resolver consumed. Test callers can use the
from_lazy_frame alternate constructor, accepting the responsibility for
vintage by themselves.

The resolver indexes are constructed once and never mutated thereafter.
They satisfy Determinism Requirement 3 (sorted iteration order from the
sorted source LazyFrame) and Requirement 4 (no set iteration in the
resolver). The resolver does NOT consume the LookaheadLeakError +
assert_not_lookahead helper from data.contracts: identifier resolution
is asof lookup (which AssetId owned this ticker at this date), not PIT
fundamental read; available_dt is not part of the resolver's contract.

Timezone convention per ADR 0002 decision 11: callers pass datetimes
already normalized to America/New_York. The resolver calls `.date()` on
datetime inputs without inspecting tzinfo; a tz-mismatched datetime
would silently drop to the wrong calendar date. Callers are responsible
for the precondition; the engine's bar loop normalizes upstream.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

import polars as pl

from pit_backtest.data.records import AssetId

if TYPE_CHECKING:
    from pit_backtest.data.sources.sharadar import SharadarDataSource


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


class TickerNotFoundError(KeyError):
    """Raised when a ticker-date or asset-date lookup has no result."""


class SharadarPermatickerResolver:
    """v1 resolver backed by the Sharadar TICKERS table.

    Sharadar's permaticker is the AssetId carrier (per ADR 0003 decision 8;
    AssetId is a NewType over int). The ticker history table records
    (permaticker, ticker, firstpricedate, lastpricedate) intervals; the
    resolver indexes them for both directions of lookup.

    Multi-match policy (raise vs pick-most-recent vs pick-oldest): the
    resolver RAISES ValueError when two intervals contain the same lookup
    date. Per ADR 0001 decision 2 spirit (surface ambiguity, do not paper
    over it), a vendor-data overlap is a data-quality bug the engine should
    fail on, not silently route around. Real Sharadar TICKERS data does not
    produce overlap by construction (a permaticker owns disjoint price-date
    intervals); the only realistic overlap source is upstream identifier
    reassignment during corporate restructuring, which is exactly the case
    that should surface.

    NULL firstpricedate handling: rows with NULL firstpricedate represent
    TICKERS entries without SEP price coverage (rare; some OTC pinks). The
    constructor filters these out at materialization so they do not enter
    the index. NULL lastpricedate means the interval is right-unbounded
    (still actively listed); the resolver treats it as `<= +infinity` in
    the interval check.

    Lookup interval convention: `[firstpricedate, lastpricedate]`
    right-closed when lastpricedate is non-null, and `[firstpricedate, +infinity)`
    right-unbounded when lastpricedate is null. Lookups are
    `firstpricedate <= lookup_date <= lastpricedate` (or
    `firstpricedate <= lookup_date` when lastpricedate is null).
    """

    def __init__(self, source: "SharadarDataSource") -> None:
        """Construct from a SharadarDataSource so the snapshot SHA256
        commitment in dataset_versioning.md is the vintage gate.

        For tests that do not want the parquet write + manifest dance,
        use `SharadarPermatickerResolver.from_lazy_frame(...)`.
        """
        tickers_lf = source.get_table("tickers")
        self._build_indexes(tickers_lf)

    @classmethod
    def from_lazy_frame(
        cls, tickers_lf: pl.LazyFrame
    ) -> "SharadarPermatickerResolver":
        """Alternate constructor for tests.

        The caller accepts responsibility for vintage (no SHA256 gate);
        production callers should construct from a SharadarDataSource so
        the manifest verification at the source's __init__ is the gate.
        """
        instance = cls.__new__(cls)
        instance._build_indexes(tickers_lf)
        return instance

    def _build_indexes(self, tickers_lf: pl.LazyFrame) -> None:
        # Cast date columns to pl.Date BEFORE filtering (project rule 12;
        # the M1 hotfix at fix/adapter-date-filter-and-pandas-pin locked
        # this in for SEP and ACTIONS; the same Datetime[ns] overflow class
        # at date(2999, 12, 31) applies to TICKERS firstpricedate /
        # lastpricedate if the parquet was written with the
        # nasdaq-data-link pandas datetime64[ns] dtype).
        materialized = (
            tickers_lf.with_columns(
                pl.col("firstpricedate").cast(pl.Date),
                pl.col("lastpricedate").cast(pl.Date),
            )
            .filter(pl.col("firstpricedate").is_not_null())
            .select(
                pl.col("permaticker"),
                pl.col("ticker"),
                pl.col("firstpricedate"),
                pl.col("lastpricedate"),
            )
            .collect()
        )

        # Two passes so each per-key list ends up in firstpricedate order.
        # A single sort like (permaticker, firstpricedate, ticker) would
        # not give the per-ticker lists firstpricedate order without a
        # re-sort inside each group.
        self._ticker_history: dict[str, list[tuple[date, date | None, AssetId]]] = {}
        by_ticker = materialized.sort(["ticker", "firstpricedate"])
        for row in by_ticker.iter_rows(named=True):
            ticker = row["ticker"]
            first = row["firstpricedate"]
            last = row["lastpricedate"]
            permaticker = AssetId(int(row["permaticker"]))
            self._ticker_history.setdefault(ticker, []).append((first, last, permaticker))

        self._permaticker_history: dict[AssetId, list[tuple[date, date | None, str]]] = {}
        by_permaticker = materialized.sort(["permaticker", "firstpricedate"])
        for row in by_permaticker.iter_rows(named=True):
            permaticker_asset_id = AssetId(int(row["permaticker"]))
            first = row["firstpricedate"]
            last = row["lastpricedate"]
            ticker = row["ticker"]
            self._permaticker_history.setdefault(permaticker_asset_id, []).append(
                (first, last, ticker)
            )

        self._construction_row_count = materialized.height

    def __repr__(self) -> str:
        return (
            f"SharadarPermatickerResolver(rows={self._construction_row_count}, "
            f"tickers={len(self._ticker_history)}, "
            f"permatickers={len(self._permaticker_history)})"
        )

    def resolve_ticker(self, ticker: str, dt: datetime) -> AssetId:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        histories = self._ticker_history.get(ticker)
        if histories is None:
            raise TickerNotFoundError(
                f"ticker {ticker!r} not in resolver index "
                f"(date={lookup_date.isoformat()})"
            )
        matches = [
            (first, last, permaticker)
            for first, last, permaticker in histories
            if first <= lookup_date and (last is None or lookup_date <= last)
        ]
        if not matches:
            candidates = ", ".join(
                f"[{first.isoformat()}, {last.isoformat() if last is not None else 'open'}] "
                f"permaticker={permaticker}"
                for first, last, permaticker in histories
            )
            raise TickerNotFoundError(
                f"ticker {ticker!r} has no interval containing "
                f"{lookup_date.isoformat()}; candidates: {candidates}"
            )
        if len(matches) > 1:
            candidates = ", ".join(
                f"permaticker={permaticker} "
                f"[{first.isoformat()}, {last.isoformat() if last is not None else 'open'}]"
                for first, last, permaticker in matches
            )
            raise ValueError(
                f"ticker {ticker!r} matches multiple permatickers at "
                f"{lookup_date.isoformat()}: {candidates}. "
                f"Vendor data quality issue; refuse to silently pick one."
            )
        return matches[0][2]

    def get_ticker(self, asset_id: AssetId, dt: datetime) -> str:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        histories = self._permaticker_history.get(asset_id)
        if histories is None:
            raise TickerNotFoundError(
                f"asset_id {int(asset_id)} not in resolver index "
                f"(date={lookup_date.isoformat()})"
            )
        matches = [
            (first, last, ticker)
            for first, last, ticker in histories
            if first <= lookup_date and (last is None or lookup_date <= last)
        ]
        if not matches:
            candidates = ", ".join(
                f"ticker={ticker!r} "
                f"[{first.isoformat()}, {last.isoformat() if last is not None else 'open'}]"
                for first, last, ticker in histories
            )
            raise TickerNotFoundError(
                f"asset_id {int(asset_id)} has no ticker at "
                f"{lookup_date.isoformat()}; candidates: {candidates}"
            )
        if len(matches) > 1:
            candidates = ", ".join(
                f"ticker={ticker!r} "
                f"[{first.isoformat()}, {last.isoformat() if last is not None else 'open'}]"
                for first, last, ticker in matches
            )
            raise ValueError(
                f"asset_id {int(asset_id)} matches multiple tickers at "
                f"{lookup_date.isoformat()}: {candidates}. "
                f"Vendor data quality issue; refuse to silently pick one."
            )
        return matches[0][2]
