"""SharadarSP500Universe tests via from_lazy_frame (M3 PR 4).

Tests use `SharadarSP500Universe.from_lazy_frame(sp500_lf, resolver)`
so each test builds a synthetic in-process LazyFrame without the parquet
+ manifest dance. Production callers construct from a SharadarDataSource
so the snapshot SHA256 commitment is the vintage gate; tests opt out of
that contract explicitly per the M3 PR 1 resolver test pattern.
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.resolver import SharadarPermatickerResolver
from pit_backtest.data.universe import (
    SharadarSP500Universe,
    UniverseValidationError,
)


def _sp500_lf(rows: list[dict[str, object]]) -> pl.LazyFrame:
    return pl.LazyFrame(
        rows, schema={"ticker": pl.String, "date": pl.Date, "action": pl.String}
    )


def _resolver(rows: list[dict[str, object]]) -> SharadarPermatickerResolver:
    tickers_lf = pl.LazyFrame(
        rows,
        schema={
            "permaticker": pl.Int64,
            "ticker": pl.String,
            "firstpricedate": pl.Date,
            "lastpricedate": pl.Date,
        },
    )
    return SharadarPermatickerResolver.from_lazy_frame(tickers_lf)


def test_single_open_ended_interval_for_still_member() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(1990, 1, 1), "action": "added"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    assert universe.is_member(AssetId(100), datetime(2024, 1, 1, 16, 0)) is True
    assert universe.membership_spells(AssetId(100)) == [
        (datetime(1990, 1, 1, 16, 0), None)
    ]


def test_paired_added_removed_interval() -> None:
    sp500 = _sp500_lf([
        {"ticker": "AGG", "date": date(2010, 6, 15), "action": "added"},
        {"ticker": "AGG", "date": date(2015, 12, 31), "action": "removed"},
    ])
    resolver = _resolver([
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    assert universe.is_member(AssetId(200), datetime(2012, 1, 1, 16, 0)) is True
    assert universe.is_member(AssetId(200), datetime(2016, 1, 1, 16, 0)) is False
    spells = universe.membership_spells(AssetId(200))
    assert spells == [
        (datetime(2010, 6, 15, 16, 0), datetime(2015, 12, 31, 16, 0)),
    ]


def test_add_remove_add_produces_two_intervals() -> None:
    """Multi-interval test on a fictional ticker (Plan-reviewer Critical 1).
    MULTI is added 2010 removed 2015 added 2018 (open-ended); the
    SharadarSP500Universe builds two intervals for the same AssetId.
    """
    sp500 = _sp500_lf([
        {"ticker": "MULTI", "date": date(2010, 6, 15), "action": "added"},
        {"ticker": "MULTI", "date": date(2015, 12, 31), "action": "removed"},
        {"ticker": "MULTI", "date": date(2018, 3, 15), "action": "added"},
    ])
    resolver = _resolver([
        {
            "permaticker": 500,
            "ticker": "MULTI",
            "firstpricedate": date(2010, 1, 1),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    assert universe.is_member(AssetId(500), datetime(2012, 1, 1, 16, 0)) is True
    assert universe.is_member(AssetId(500), datetime(2017, 1, 1, 16, 0)) is False
    assert universe.is_member(AssetId(500), datetime(2020, 1, 1, 16, 0)) is True
    spells = universe.membership_spells(AssetId(500))
    assert spells == [
        (datetime(2010, 6, 15, 16, 0), datetime(2015, 12, 31, 16, 0)),
        (datetime(2018, 3, 15, 16, 0), None),
    ]


def test_double_add_raises_universe_validation_error() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 1, 1), "action": "added"},
        {"ticker": "SPY", "date": date(2015, 1, 1), "action": "added"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(sp500, resolver)
    message = str(exc_info.value)
    assert "double-add" in message
    assert "SPY" in message
    assert "2015-01-01" in message
    assert "2010-01-01" in message


def test_remove_without_add_raises_universe_validation_error() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 1, 1), "action": "removed"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(sp500, resolver)
    assert "remove-without-add" in str(exc_info.value)


def test_unknown_action_raises_universe_validation_error() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 1, 1), "action": "transferred"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(sp500, resolver)
    message = str(exc_info.value)
    assert "unknown action" in message
    assert "transferred" in message


def test_resolver_unknown_ticker_at_event_date_raises_universe_validation_error() -> None:
    """The resolver does not know ticker XYZ at the event date; chained
    via `raise ... from exc` per Plan-reviewer Counter on Choice 3.
    """
    sp500 = _sp500_lf([
        {"ticker": "XYZ", "date": date(2010, 1, 1), "action": "added"},
    ])
    # Resolver has SPY but not XYZ.
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(sp500, resolver)
    message = str(exc_info.value)
    assert "XYZ" in message
    assert "resolver has no AssetId" in message
    # Per High 3 the message names bundle name + hint at staleness.
    assert "bundle" in message
    # Cause chain is preserved.
    assert exc_info.value.__cause__ is not None


def test_same_date_added_removed_pair_produces_one_day_interval() -> None:
    """Plan-reviewer Medium 5: same-date add+remove is a documented
    one-day membership (not a no-op).
    """
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 1, 1), "action": "added"},
        {"ticker": "SPY", "date": date(2010, 1, 1), "action": "removed"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    assert universe.is_member(AssetId(100), datetime(2010, 1, 1, 16, 0)) is True
    assert universe.is_member(AssetId(100), datetime(2010, 1, 2, 16, 0)) is False
    spells = universe.membership_spells(AssetId(100))
    assert spells == [
        (datetime(2010, 1, 1, 16, 0), datetime(2010, 1, 1, 16, 0)),
    ]


def test_members_at_sorted_by_int_value() -> None:
    """Plan-reviewer Low 10: explicit sort-by-int regression."""
    sp500 = _sp500_lf([
        # Insert in non-numeric order to verify the sort is by int, not insertion.
        {"ticker": "AGG", "date": date(1990, 1, 1), "action": "added"},
        {"ticker": "SPY", "date": date(1990, 1, 1), "action": "added"},
    ])
    resolver = _resolver([
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    members = universe.members_at(datetime(2024, 1, 1, 16, 0))
    assert members == [AssetId(100), AssetId(200)]
    assert members == sorted(members, key=int)


def test_pit_discipline_future_added_excluded() -> None:
    """Structural PIT regression per project rule 2D: an "added" event
    in the future must NOT affect membership at a past lookup_date.
    """
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(1990, 1, 1), "action": "added"},
        {"ticker": "AGG", "date": date(2026, 1, 1), "action": "added"},
    ])
    resolver = _resolver([
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, resolver)

    members_2010 = universe.members_at(datetime(2010, 6, 15, 16, 0))
    assert members_2010 == [AssetId(100)]
    assert universe.is_member(AssetId(200), datetime(2010, 6, 15, 16, 0)) is False
    # AGG IS a member after its 2026 add.
    assert universe.is_member(AssetId(200), datetime(2026, 6, 15, 16, 0)) is True


def test_construction_is_deterministic_across_two_instances() -> None:
    """Per Determinism Requirement 3: same input -> same interval shape."""
    rows = [
        {"ticker": "SPY", "date": date(1990, 1, 1), "action": "added"},
        {"ticker": "AGG", "date": date(2010, 6, 15), "action": "added"},
        {"ticker": "AGG", "date": date(2015, 12, 31), "action": "removed"},
    ]
    ticker_rows = [
        {
            "permaticker": 100,
            "ticker": "SPY",
            "firstpricedate": date(1990, 1, 1),
            "lastpricedate": None,
        },
        {
            "permaticker": 200,
            "ticker": "AGG",
            "firstpricedate": date(2003, 9, 22),
            "lastpricedate": None,
        },
    ]
    u1 = SharadarSP500Universe.from_lazy_frame(
        _sp500_lf(rows), _resolver(ticker_rows)
    )
    u2 = SharadarSP500Universe.from_lazy_frame(
        _sp500_lf(rows), _resolver(ticker_rows)
    )
    for dt in (datetime(2012, 1, 1, 16, 0), datetime(2016, 1, 1, 16, 0)):
        assert u1.members_at(dt) == u2.members_at(dt)
    assert u1.membership_spells(AssetId(100)) == u2.membership_spells(AssetId(100))
    assert u1.membership_spells(AssetId(200)) == u2.membership_spells(AssetId(200))
