"""SharadarSP500Universe tests via from_lazy_frame (ADR 0017 snapshot model).

Tests use `SharadarSP500Universe.from_lazy_frame(sp500_lf, resolver)` so
each test builds a synthetic in-process LazyFrame without the parquet +
manifest dance. Production callers construct from a SharadarDataSource so
the snapshot SHA256 commitment is the vintage gate; tests opt out of that
contract explicitly per the M3 PR 1 resolver test pattern.

The universe reads membership from the `historical`/`current` SNAPSHOT
rows (ADR 0017); `added`/`removed` rows, if present, are ignored by the
universe (they feed the separate cross-check contract). `members_at(t)`
returns the most-recent snapshot on or before t; spell boundaries are
quarter-end snapshot dates.
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


_SPY_TICKER = {
    "permaticker": 100,
    "ticker": "SPY",
    "firstpricedate": date(1993, 1, 22),
    "lastpricedate": None,
}
_AGG_TICKER = {
    "permaticker": 200,
    "ticker": "AGG",
    "firstpricedate": date(2003, 9, 22),
    "lastpricedate": None,
}


# ----- members_at as-of semantics -----


def test_single_snapshot_membership_resolves() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.members_at(datetime(2010, 6, 1, 16, 0)) == [
        AssetId(100),
        AssetId(200),
    ]


def test_members_at_asof_returns_most_recent_snapshot_on_or_before() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2010, 6, 30), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    # Between snapshots: the earlier one applies.
    assert universe.members_at(datetime(2010, 5, 1, 16, 0)) == [
        AssetId(100),
        AssetId(200),
    ]
    # On the snapshot date itself: inclusive.
    assert universe.members_at(datetime(2010, 3, 31, 16, 0)) == [
        AssetId(100),
        AssetId(200),
    ]
    # After the later snapshot: AGG has dropped.
    assert universe.members_at(datetime(2010, 7, 1, 16, 0)) == [AssetId(100)]


def test_members_at_before_first_snapshot_is_empty() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, _resolver([_SPY_TICKER]))

    assert universe.members_at(datetime(2009, 12, 31, 16, 0)) == []
    assert universe.is_member(AssetId(100), datetime(2009, 12, 31, 16, 0)) is False


def test_current_snapshot_folds_in_as_latest() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2026, 5, 30), "action": "current"},
        {"ticker": "AGG", "date": date(2026, 5, 30), "action": "current"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    # Before the current snapshot: only the 2010 historical roster.
    assert universe.members_at(datetime(2011, 1, 1, 16, 0)) == [AssetId(100)]
    # After the current snapshot: the current roster.
    assert universe.members_at(datetime(2026, 6, 1, 16, 0)) == [
        AssetId(100),
        AssetId(200),
    ]


def test_members_at_sorted_by_int_value() -> None:
    sp500 = _sp500_lf([
        # Non-numeric insertion order to verify the sort is by int.
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_AGG_TICKER, _SPY_TICKER])
    )

    members = universe.members_at(datetime(2010, 6, 1, 16, 0))
    assert members == [AssetId(100), AssetId(200)]
    assert members == sorted(members, key=int)


def test_pit_discipline_future_snapshot_excluded() -> None:
    """A future-dated snapshot must not affect a past members_at."""
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2026, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2026, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.members_at(datetime(2012, 6, 15, 16, 0)) == [AssetId(100)]
    assert universe.is_member(AssetId(200), datetime(2012, 6, 15, 16, 0)) is False
    assert universe.is_member(AssetId(200), datetime(2026, 6, 15, 16, 0)) is True


def test_added_removed_rows_are_ignored_by_the_universe() -> None:
    """Only `historical`/`current` rows define membership; `added`/`removed`
    event rows are ignored by the universe (ADR 0017)."""
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        # These event rows must not create membership.
        {"ticker": "AGG", "date": date(2005, 1, 1), "action": "added"},
        {"ticker": "AGG", "date": date(2009, 1, 1), "action": "removed"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.members_at(datetime(2010, 6, 1, 16, 0)) == [AssetId(100)]
    assert universe.is_member(AssetId(200), datetime(2007, 1, 1, 16, 0)) is False


# ----- membership_spells -----


def test_membership_spells_contiguous_run_closes_at_last_present_snapshot() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2010, 6, 30), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 6, 30), "action": "historical"},
        {"ticker": "SPY", "date": date(2010, 9, 30), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 9, 30), "action": "historical"},
        # AGG keeps the final snapshot non-empty; SPY has dropped.
        {"ticker": "AGG", "date": date(2011, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.membership_spells(AssetId(100)) == [
        (datetime(2010, 3, 31, 16, 0), datetime(2010, 9, 30, 16, 0)),
    ]


def test_membership_spells_gap_produces_two_spells() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 6, 30), "action": "historical"},  # gap
        {"ticker": "SPY", "date": date(2010, 9, 30), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 12, 31), "action": "historical"},  # drop
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.membership_spells(AssetId(100)) == [
        (datetime(2010, 3, 31, 16, 0), datetime(2010, 3, 31, 16, 0)),
        (datetime(2010, 9, 30, 16, 0), datetime(2010, 9, 30, 16, 0)),
    ]


def test_membership_spells_open_ended_through_latest_snapshot() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2020, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, _resolver([_SPY_TICKER]))

    assert universe.membership_spells(AssetId(100)) == [
        (datetime(2010, 3, 31, 16, 0), None),
    ]


def test_membership_spells_single_snapshot_is_degenerate_same_date() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 6, 30), "action": "historical"},  # SPY gone
    ])
    universe = SharadarSP500Universe.from_lazy_frame(
        sp500, _resolver([_SPY_TICKER, _AGG_TICKER])
    )

    assert universe.membership_spells(AssetId(100)) == [
        (datetime(2010, 3, 31, 16, 0), datetime(2010, 3, 31, 16, 0)),
    ]


def test_membership_spells_unknown_asset_returns_empty() -> None:
    sp500 = _sp500_lf([
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
    ])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, _resolver([_SPY_TICKER]))

    assert universe.membership_spells(AssetId(999)) == []


# ----- boundary resolution + validation -----


def test_boundary_snapshot_member_resolves_despite_date_outside_price_interval() -> None:
    """ADR 0017 boundary case: a spin-off listed at the quarter-end snapshot
    one or more days BEFORE its first regular-way price bar. Date-agnostic
    ticker-string resolution succeeds where date-gated resolution would
    raise, so the member is present (mirrors TDC 2007-09-30 vs firstprice
    2007-10-02 on the real bundle)."""
    sp500 = _sp500_lf([
        {"ticker": "TDC", "date": date(2007, 9, 30), "action": "historical"},
    ])
    tdc_ticker = {
        "permaticker": 700,
        "ticker": "TDC",
        "firstpricedate": date(2007, 10, 2),  # AFTER the snapshot date
        "lastpricedate": None,
    }
    universe = SharadarSP500Universe.from_lazy_frame(sp500, _resolver([tdc_ticker]))

    assert universe.members_at(datetime(2007, 10, 15, 16, 0)) == [AssetId(700)]
    assert universe.is_member(AssetId(700), datetime(2007, 9, 30, 16, 0)) is True


def test_snapshot_member_ticker_absent_from_tickers_raises() -> None:
    sp500 = _sp500_lf([
        {"ticker": "XYZ", "date": date(2010, 3, 31), "action": "historical"},
    ])
    # Resolver knows SPY but not XYZ.
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(
            sp500, _resolver([_SPY_TICKER]), bundle_name="test_bundle"
        )
    message = str(exc_info.value)
    assert "XYZ" in message
    assert "no AssetId" in message
    assert "test_bundle" in message
    assert exc_info.value.__cause__ is not None


def test_snapshot_member_ticker_reuse_raises() -> None:
    sp500 = _sp500_lf([
        {"ticker": "REUSE", "date": date(2010, 3, 31), "action": "historical"},
    ])
    # REUSE maps to two distinct permatickers (ticker reuse).
    reuse_rows = [
        {
            "permaticker": 800,
            "ticker": "REUSE",
            "firstpricedate": date(2000, 1, 3),
            "lastpricedate": date(2008, 1, 1),
        },
        {
            "permaticker": 801,
            "ticker": "REUSE",
            "firstpricedate": date(2009, 1, 5),
            "lastpricedate": None,
        },
    ]
    with pytest.raises(UniverseValidationError) as exc_info:
        SharadarSP500Universe.from_lazy_frame(
            sp500, _resolver(reuse_rows), bundle_name="test_bundle"
        )
    message = str(exc_info.value)
    assert "REUSE" in message
    assert "multiple permatickers" in message
    assert exc_info.value.__cause__ is not None


def test_empty_bundle_returns_empty_members_without_raising() -> None:
    """A bundle with no snapshot rows builds an empty universe; querying it
    returns [] for any date and never raises."""
    sp500 = _sp500_lf([])
    universe = SharadarSP500Universe.from_lazy_frame(sp500, _resolver([_SPY_TICKER]))

    assert universe.members_at(datetime(2010, 1, 1, 16, 0)) == []
    assert universe.is_member(AssetId(100), datetime(2010, 1, 1, 16, 0)) is False
    assert universe.membership_spells(AssetId(100)) == []


def test_construction_is_deterministic_across_two_instances() -> None:
    """Per Determinism Requirement 3: same input -> same snapshot shape."""
    rows = [
        {"ticker": "SPY", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "AGG", "date": date(2010, 3, 31), "action": "historical"},
        {"ticker": "SPY", "date": date(2010, 6, 30), "action": "historical"},
    ]
    ticker_rows = [_SPY_TICKER, _AGG_TICKER]
    u1 = SharadarSP500Universe.from_lazy_frame(_sp500_lf(rows), _resolver(ticker_rows))
    u2 = SharadarSP500Universe.from_lazy_frame(_sp500_lf(rows), _resolver(ticker_rows))

    for dt in (datetime(2010, 5, 1, 16, 0), datetime(2010, 7, 1, 16, 0)):
        assert u1.members_at(dt) == u2.members_at(dt)
    assert u1.membership_spells(AssetId(100)) == u2.membership_spells(AssetId(100))
    assert u1.membership_spells(AssetId(200)) == u2.membership_spells(AssetId(200))
