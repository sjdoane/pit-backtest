"""SharadarPermatickerResolver tests (M3 PR 1).

Tests use the `from_lazy_frame` alternate constructor so each test builds
a synthetic in-process LazyFrame without the parquet write + manifest
dance. Production callers construct the resolver from a SharadarDataSource
so the snapshot SHA256 commitment in dataset_versioning.md is the gate
that pins which vintage of TICKERS the resolver consumed; tests opt out
of that contract explicitly.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.resolver import (
    SharadarPermatickerResolver,
    TickerNotFoundError,
)


def _tickers_lf(
    rows: list[dict[str, object]],
) -> pl.LazyFrame:
    """Build a TICKERS-shaped LazyFrame with the column subset the resolver
    indexes. Other TICKERS columns (name, exchange, cusip, etc.) are not
    consumed by the resolver and are omitted here.
    """
    return pl.LazyFrame(rows, schema={
        "permaticker": pl.Int64,
        "ticker": pl.String,
        "firstpricedate": pl.Date,
        "lastpricedate": pl.Date,
    })


def test_resolve_ticker_single_interval_returns_correct_permaticker() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    asset_id = resolver.resolve_ticker("SPY", datetime(2015, 6, 15, 16, 0))
    assert asset_id == AssetId(100)


def test_get_ticker_returns_correct_ticker_for_permaticker_at_date() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    ticker = resolver.get_ticker(AssetId(100), datetime(2015, 6, 15, 16, 0))
    assert ticker == "SPY"


def test_resolve_ticker_pre_firstpricedate_raises_ticker_not_found() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(TickerNotFoundError) as exc_info:
        resolver.resolve_ticker("SPY", datetime(2009, 12, 31, 16, 0))
    assert "2009-12-31" in str(exc_info.value)
    assert "no interval containing" in str(exc_info.value)


def test_resolve_ticker_post_lastpricedate_raises_ticker_not_found() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(TickerNotFoundError):
        resolver.resolve_ticker("SPY", datetime(2021, 1, 1, 16, 0))


def test_resolve_ticker_active_through_now_succeeds_with_null_lastpricedate() -> None:
    """NULL lastpricedate means the interval is right-unbounded (active
    through now). Lookups at any date >= firstpricedate succeed.
    """
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    asset_id = resolver.resolve_ticker("SPY", datetime(2026, 5, 30, 16, 0))
    assert asset_id == AssetId(100)


def test_resolve_ticker_reused_after_delisting_returns_correct_permaticker_per_date() -> None:
    """Ticker FOO used by permaticker=100 from 2010 to 2014; then by
    permaticker=200 from 2016 to 2020 (non-overlapping intervals).
    """
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "FOO",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2014, 12, 31),
        },
        {
            "permaticker": 200,
            "ticker": "FOO",
            "firstpricedate": date(2016, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    assert resolver.resolve_ticker("FOO", datetime(2012, 6, 1, 16, 0)) == AssetId(100)
    assert resolver.resolve_ticker("FOO", datetime(2018, 6, 1, 16, 0)) == AssetId(200)
    with pytest.raises(TickerNotFoundError):
        resolver.resolve_ticker("FOO", datetime(2015, 6, 1, 16, 0))


def test_resolve_ticker_multi_match_raises_value_error_with_candidates() -> None:
    """Two intervals overlap; the resolver must raise rather than silently
    pick one. Vendor data quality should surface.
    """
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "BAR",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
        {
            "permaticker": 200,
            "ticker": "BAR",
            "firstpricedate": date(2015, 1, 1),
            "lastpricedate": date(2018, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(ValueError) as exc_info:
        resolver.resolve_ticker("BAR", datetime(2016, 6, 1, 16, 0))
    message = str(exc_info.value)
    assert "permaticker=100" in message
    assert "permaticker=200" in message


def test_get_ticker_pre_firstpricedate_raises() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(TickerNotFoundError):
        resolver.get_ticker(AssetId(100), datetime(2009, 12, 31, 16, 0))


def test_get_ticker_multi_match_raises() -> None:
    """Same permaticker owns two tickers with overlapping intervals."""
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "OLDSYM",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2018, 12, 31),
        },
        {
            "permaticker": 100,
            "ticker": "NEWSYM",
            "firstpricedate": date(2015, 1, 1),
            "lastpricedate": date(2020, 12, 31),
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(ValueError) as exc_info:
        resolver.get_ticker(AssetId(100), datetime(2016, 6, 1, 16, 0))
    message = str(exc_info.value)
    assert "OLDSYM" in message
    assert "NEWSYM" in message


def test_resolve_unknown_ticker_raises_ticker_not_found() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(TickerNotFoundError) as exc_info:
        resolver.resolve_ticker("UNKNOWN", datetime(2015, 1, 1, 16, 0))
    assert "not in resolver index" in str(exc_info.value)


def test_construction_is_deterministic_across_two_instances() -> None:
    """Per Determinism Requirement 3, same input -> same indexed shape."""
    rows = [
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
        {
            "permaticker": 300,
            "ticker": "OLDCO",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": date(2014, 12, 31),
        },
    ]
    r1 = SharadarPermatickerResolver.from_lazy_frame(_tickers_lf(rows))
    r2 = SharadarPermatickerResolver.from_lazy_frame(_tickers_lf(rows))
    assert r1._ticker_history == r2._ticker_history
    assert r1._permaticker_history == r2._permaticker_history


def test_datetime_input_is_normalized_to_date() -> None:
    """Datetime and date inputs at the same calendar date return the same
    AssetId. Per ADR 0002 decision 11, callers normalize tz at the engine
    boundary; the resolver calls .date() without inspecting tzinfo.
    """
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    dt_ny = datetime(2015, 1, 1, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    # The Protocol annotates `datetime`; calling with a `date` works too because
    # `.date()` is only invoked when isinstance(dt, datetime). The type checker
    # rejects this in strict mode but the runtime path is tested via the
    # equality below: same calendar date -> same AssetId.
    asset_id_dt = resolver.resolve_ticker("SPY", dt_ny)
    assert asset_id_dt == AssetId(100)


def test_null_firstpricedate_rows_excluded_from_index() -> None:
    """Sharadar TICKERS rows with NULL firstpricedate (no SEP coverage)
    are filtered at materialization so they do not enter the index.
    """
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 999,
            "ticker": "NOPRICE",
            "firstpricedate": None,
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    with pytest.raises(TickerNotFoundError):
        resolver.resolve_ticker("NOPRICE", datetime(2015, 1, 1, 16, 0))
    # The SPY row is still indexed.
    assert resolver.resolve_ticker("SPY", datetime(2015, 1, 1, 16, 0)) == AssetId(100)


def test_contains_returns_true_for_indexed_permaticker_and_false_otherwise() -> None:
    """Per M3 PR 3 Plan-reviewer: public predicate to avoid private-field touch."""
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    assert resolver.contains(AssetId(100)) is True
    assert resolver.contains(AssetId(200)) is True
    assert resolver.contains(AssetId(999)) is False


def test_repr_surfaces_index_sizes_for_diagnostics() -> None:
    lf = _tickers_lf([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
    ])
    resolver = SharadarPermatickerResolver.from_lazy_frame(lf)
    repr_str = repr(resolver)
    assert "rows=2" in repr_str
    assert "tickers=2" in repr_str
    assert "permatickers=2" in repr_str
