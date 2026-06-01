"""Lookahead gate tests (M3 PR 1) + data quality contracts (M3 PR 5a).

Per project rule 2D, PIT data work must ship lookahead-leak tests. The
first block (lines 30-110) covers the LookaheadLeakError +
assert_not_lookahead helper.

The M3 PR 5a block covers:
- Five concrete DataQualityContract impls (success + failure + skip-on-
  missing-table for each).
- The `_nth_trading_day_after` helper's strict-after semantics
  (Thanksgiving-week pin per Plan-reviewer High 2).
- run_data_quality_contracts aggregation semantics (collect-all, sorted-
  by-name, INFO on skip / pass, ERROR on fail).
- check_snapshot_freshness threshold tiers + clock injection.
- SharadarDataSource.__init__ integration smoke (full + M1 two-table +
  stale bundle).
- One lookahead-smoke regression that the PR 5a wiring does not
  inadvertently bypass the per-row PIT gate (PR 2's
  test_get_fundamental_does_not_return_row_with_datekey_in_future is the
  canonical assertion; this file references the pattern but does not
  duplicate the assertion logic).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import polars as pl
import pytest

from pit_backtest.data.contracts import (
    DataQualityError,
    FirstPriceWithinFiveDaysContract,
    LookaheadLeakError,
    NoDuplicateSp500EventsContract,
    NoDuplicateTickerDatekeyInSf1Contract,
    NoSepBarsAfterDelistingContract,
    Sf1DatekeyNonNullAfter1990Contract,
    Sp500AddedRemovedCrossCheckContract,
    Sp500SnapshotMembersResolveContract,
    _nth_trading_day_after,
    assert_not_lookahead,
    check_snapshot_freshness,
    run_data_quality_contracts,
)
from pit_backtest.data.sources.manifest import (
    SnapshotBundleEntry,
    SnapshotFileEntry,
)
from pit_backtest.data.sources.sharadar import SharadarDataSource

# Cross-module import: the canonical synthetic bundle helper lives in
# test_sharadar_adapter.py; reusing it here avoids duplicating the
# manifest-construction dance. `tests/__init__.py` plus
# `tests/data/__init__.py` make this a regular package import; pytest
# collection handles it.
from tests.data.test_sharadar_adapter import (
    _IPO_WINDOW_SEP_ROWS,
    _M3_TABLES,
    _SEP_ROWS,
    _SF1_ROWS,
    _SP500_ROWS,
    _TICKERS_ROWS,
    _write_synthetic_bundle,
)


def test_assert_not_lookahead_allows_equal_dates() -> None:
    """available_dt == simulation_dt is the borderline allowed case.

    Per ADR 0001 decision 9 the gate is `available_dt <= simulation_dt`;
    equal dates pass (the record became observable at the same moment
    the simulation is asking for it).
    """
    dt = datetime(2024, 3, 15, 16, 0)
    assert_not_lookahead(dt, dt, context="test_equal")


def test_assert_not_lookahead_allows_past_available_dt() -> None:
    """available_dt strictly in the past returns None (no raise)."""
    available = datetime(2024, 3, 14, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    assert_not_lookahead(available, simulation, context="test_past")


def test_assert_not_lookahead_raises_on_future_available_dt() -> None:
    """available_dt strictly in the future raises LookaheadLeakError."""
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(available, simulation, context="test_future")
    message = str(exc_info.value)
    assert "lookahead leak" in message
    assert "2024-03-16T16:00:00" in message
    assert "2024-03-15T16:00:00" in message
    assert "test_future" in message


def test_lookahead_leak_error_is_value_error() -> None:
    """Callers can broad-catch ValueError when wrapping a pit_view read."""
    assert issubclass(LookaheadLeakError, ValueError)


def test_assert_not_lookahead_message_contains_context_for_diagnostics() -> None:
    """The context string surfaces verbatim so a debug session has the call
    site without a stack trace.
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    context = "SharadarDataSource.get_fundamental(asset=42, field='revenue')"
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(available, simulation, context=context)
    assert context in str(exc_info.value)


def test_assert_not_lookahead_message_includes_period_end_dt_when_provided() -> None:
    """Per ADR 0001 decision 9 the dual-timestamp pair is
    (period_end_dt, available_dt). When the caller provides period_end_dt
    the helper surfaces it so a future debug session has both halves
    without re-reading the source frame.
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    period_end = datetime(2023, 12, 31, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(
            available,
            simulation,
            context="test_with_period_end",
            period_end_dt=period_end,
        )
    message = str(exc_info.value)
    assert "period_end_dt=2023-12-31T16:00:00" in message


def test_assert_not_lookahead_period_end_dt_optional_omits_when_absent() -> None:
    """When period_end_dt is None the message does not mention it; this
    preserves the M3 PR 1 standalone-helper contract for callers that
    do not have period_end_dt at the call site (e.g., get_price reads).
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(
            available, simulation, context="test_no_period_end"
        )
    assert "period_end_dt" not in str(exc_info.value)


# ============================================================================
# M3 PR 5a: data quality contracts + runner + freshness check
# ============================================================================


# ----- NYSE trading-day helper -----


def test_nth_trading_day_after_normal_week_returns_fifth_trading_day() -> None:
    """A Tuesday firstpricedate with no holidays nearby: n=5 lands on the
    following Tuesday (5 trading days strictly after the anchor).

    Anchor 2024-04-02 (Tuesday): Wed Apr 3, Thu Apr 4, Fri Apr 5, Mon
    Apr 8, Tue Apr 9. Expected cutoff = Tue Apr 9.
    """
    cutoff = _nth_trading_day_after(date(2024, 4, 2), n=5)
    assert cutoff == date(2024, 4, 9)


def test_nth_trading_day_after_thanksgiving_pins_strict_after_semantics() -> None:
    """Plan-reviewer High 2 regression: a Wednesday before Thanksgiving
    must not include Thanksgiving Thursday or the post-Thanksgiving
    half-day Friday closure logic in the count.

    Anchor 2024-11-27 (Wed before Thanksgiving). Strictly after:
    Fri Nov 29 (half-day but still a trading day per NYSE), Mon Dec 2,
    Tue Dec 3, Wed Dec 4, Thu Dec 5. Expected cutoff = Thu Dec 5.

    A naive `valid_days(anchor, anchor + 14d)[4]` would return Dec 4
    (off-by-one); this test pins that the strict-after implementation
    returns Dec 5.
    """
    cutoff = _nth_trading_day_after(date(2024, 11, 27), n=5)
    assert cutoff == date(2024, 12, 5)


def test_nth_trading_day_after_when_anchor_is_holiday_skips_anchor() -> None:
    """When the anchor is itself a holiday (e.g., 2025-01-01 New Year's
    Day), the strict-after window starts at Jan 2 and the n=5 cutoff
    lands at Jan 8 (Jan 2 Thu, Jan 3 Fri, Jan 6 Mon, Jan 7 Tue, Jan 8 Wed).
    """
    cutoff = _nth_trading_day_after(date(2025, 1, 1), n=5)
    assert cutoff == date(2025, 1, 8)


def test_nth_trading_day_after_rejects_zero_and_negative_n() -> None:
    """n < 1 has no meaning; the helper raises ValueError rather than
    returning a stale index.
    """
    with pytest.raises(ValueError, match="n must be >= 1"):
        _nth_trading_day_after(date(2024, 4, 2), n=0)
    with pytest.raises(ValueError, match="n must be >= 1"):
        _nth_trading_day_after(date(2024, 4, 2), n=-3)


# ----- helpers for the per-contract tests -----


def _empty_sep_frame() -> pl.DataFrame:
    """Empty SEP-shaped frame; used by failure tests that want to assert
    the contract surfaces zero in-window matches without rebuilding the
    bundle on disk.
    """
    return pl.DataFrame(
        schema={
            "ticker": pl.String,
            "date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "closeunadj": pl.Float64,
            "volume": pl.Int64,
        }
    )


def _tickers_frame_with_one_row(
    *,
    permaticker: int = 100,
    ticker: str = "SPY",
    isdelisted: str = "N",
    firstpricedate: date | None = date(2024, 4, 2),
    lastpricedate: date | None = None,
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "permaticker": permaticker,
                "ticker": ticker,
                "name": f"{ticker} Co",
                "exchange": "NYSEARCA",
                "isdelisted": isdelisted,
                "firstpricedate": firstpricedate,
                "lastpricedate": lastpricedate,
                "firstquarter": (
                    firstpricedate if firstpricedate is not None else None
                ),
                "lastquarter": lastpricedate,
                "cusip": f"{ticker}00001",
            }
        ]
    )


# ----- Contract 1: FirstPriceWithinFiveDaysContract -----


def test_first_price_within_five_days_passes_on_clean_bundle() -> None:
    """The shared synthetic fixture's SEP table now carries an IPO-window
    row for every TICKERS firstpricedate; contract returns None on
    success and never raises.
    """
    frames = {
        "tickers": pl.DataFrame(_TICKERS_ROWS),
        "sep": pl.DataFrame(_SEP_ROWS),
    }
    FirstPriceWithinFiveDaysContract().check(frames)


def test_first_price_within_five_days_fails_when_sep_missing_first_bar() -> None:
    """A TICKERS row whose ticker is absent from SEP within the
    [firstpricedate, +5 trading days] window raises with the offending
    permaticker + ticker + firstpricedate surfaced. The SEP frame carries a
    bar for a DIFFERENT ticker on GHOST's firstpricedate so the coverage
    window is non-empty (firstpricedate >= min(SEP date)) and GHOST is a
    candidate; GHOST itself has no in-window bar.
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=42, ticker="GHOST", firstpricedate=date(2024, 4, 2)
    )
    sep = pl.DataFrame(
        [
            {
                "ticker": "OTHER",
                "date": date(2024, 4, 2),  # establishes the SEP coverage window
                "open": 10.0, "high": 10.0, "low": 10.0,
                "close": 10.0, "closeunadj": 10.0, "volume": 100,
            }
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        FirstPriceWithinFiveDaysContract().check({"tickers": tickers, "sep": sep})
    message = str(exc_info.value)
    assert "tickers_first_price_within_five_days" in message
    assert "GHOST" in message
    assert "42" in message
    # `.to_dicts()` surfaces dates via their `repr()` (`datetime.date(2024, 4, 2)`)
    # rather than ISO format; assert against the literal repr.
    assert "datetime.date(2024, 4, 2)" in message


def test_first_price_within_five_days_skips_firstpricedate_before_sep_coverage() -> None:
    """Coverage-window refinement (M5 data-quality WIP): a TICKERS row whose
    firstpricedate precedes the earliest SEP bar began trading before the
    pulled window, so the five-day coverage check is not applicable and the
    row is skipped rather than flagged. Real Sharadar bundles carry SP500
    history far deeper than the SEP price feed; only synthetic fixtures had
    the two coincide.
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=42, ticker="DEEP", firstpricedate=date(1990, 1, 2)
    )
    sep = pl.DataFrame(
        [
            {
                "ticker": "OTHER",
                "date": date(2004, 1, 2),  # SEP coverage starts well after 1990
                "open": 10.0, "high": 10.0, "low": 10.0,
                "close": 10.0, "closeunadj": 10.0, "volume": 100,
            }
        ]
    )
    # No raise: DEEP's firstpricedate is outside (before) SEP coverage.
    FirstPriceWithinFiveDaysContract().check({"tickers": tickers, "sep": sep})


def test_first_price_within_five_days_empty_sep_is_skipped() -> None:
    """An empty SEP table has no coverage window to validate against, so the
    contract returns without raising (the bundle would fail other contracts
    first; this guards the min(SEP)-is-None branch)."""
    tickers = _tickers_frame_with_one_row(
        permaticker=42, ticker="GHOST", firstpricedate=date(2024, 4, 2)
    )
    FirstPriceWithinFiveDaysContract().check(
        {"tickers": tickers, "sep": _empty_sep_frame()}
    )


def test_first_price_within_five_days_with_sep_row_inside_window_passes() -> None:
    """SEP bar within the [firstpricedate, firstpricedate + 5 trading days]
    window satisfies the contract; this pins that the cutoff is inclusive
    on the right edge (NOT strict-less-than).
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=42, ticker="EDGE", firstpricedate=date(2024, 4, 2)
    )
    sep = pl.DataFrame(
        [
            {
                "ticker": "EDGE",
                "date": date(2024, 4, 9),  # 5 trading days after anchor
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.0,
                "closeunadj": 10.0,
                "volume": 100,
            }
        ]
    )
    FirstPriceWithinFiveDaysContract().check({"tickers": tickers, "sep": sep})


def test_first_price_within_five_days_null_firstpricedate_skipped() -> None:
    """TICKERS rows with NULL firstpricedate are never-traded shells; the
    contract skips them rather than treating them as violations.
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=99, ticker="SHELL", firstpricedate=None
    )
    sep = _empty_sep_frame()
    FirstPriceWithinFiveDaysContract().check({"tickers": tickers, "sep": sep})


# ----- Contract 2: NoSepBarsAfterDelistingContract -----


def test_no_sep_bars_after_delisting_passes_on_clean_bundle() -> None:
    """The shared fixture's OLDCO row (delisted 2014-12-31) has zero SEP
    rows after that date; DLST has rows AT lastpricedate (2018-06-30)
    but none AFTER; the contract passes.
    """
    frames = {
        "tickers": pl.DataFrame(_TICKERS_ROWS),
        "sep": pl.DataFrame(_SEP_ROWS),
    }
    NoSepBarsAfterDelistingContract().check(frames)


def test_no_sep_bars_after_delisting_fails_on_phantom_post_delisting_bar() -> None:
    """A SEP row whose date is strictly after a delisted TICKERS row's
    lastpricedate surfaces in the failure message.
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=300,
        ticker="GHOST",
        isdelisted="Y",
        firstpricedate=date(2010, 1, 4),
        lastpricedate=date(2014, 12, 31),
    )
    sep = pl.DataFrame(
        [
            # Valid IPO-window bar.
            {
                "ticker": "GHOST", "date": date(2010, 1, 4),
                "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
                "closeunadj": 10.0, "volume": 100,
            },
            # Phantom post-delisting bar (the contract's target).
            {
                "ticker": "GHOST", "date": date(2015, 1, 5),
                "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0,
                "closeunadj": 5.0, "volume": 100,
            },
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        NoSepBarsAfterDelistingContract().check(
            {"tickers": tickers, "sep": sep}
        )
    message = str(exc_info.value)
    assert "no_sep_bars_after_delisting" in message
    assert "GHOST" in message
    # `.to_dicts()` surfaces dates via their `repr()` so assert against
    # the literal repr of the offending SEP date.
    assert "datetime.date(2015, 1, 5)" in message


def test_no_sep_bars_after_delisting_passes_when_lastpricedate_is_null() -> None:
    """A TICKERS row marked delisted='Y' but with NULL lastpricedate is
    skipped by the contract (the vendor never reported the delisting
    date; a separate data-quality concern not in this contract's scope).
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=300,
        ticker="UNKNOWN",
        isdelisted="Y",
        firstpricedate=date(2010, 1, 4),
        lastpricedate=None,
    )
    sep = pl.DataFrame(
        [
            {
                "ticker": "UNKNOWN", "date": date(2010, 1, 4),
                "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
                "closeunadj": 10.0, "volume": 100,
            },
        ]
    )
    NoSepBarsAfterDelistingContract().check({"tickers": tickers, "sep": sep})


# ----- Contract 3: Sf1DatekeyNonNullAfter1990Contract -----


def test_sf1_datekey_non_null_after_1990_passes_on_clean_bundle() -> None:
    """The shared SF1 fixture has datekey populated on every ARQ row;
    contract returns None.
    """
    SuC = Sf1DatekeyNonNullAfter1990Contract()
    SuC.check({"sf1": pl.DataFrame(_SF1_ROWS)})


def test_sf1_datekey_non_null_after_1990_fails_on_null_datekey_arq_row() -> None:
    """An ARQ row with calendardate >= 1991-01-01 and NULL datekey is the
    target bug class.
    """
    sf1 = pl.DataFrame(
        [
            {
                "ticker": "BUG",
                "dimension": "ARQ",
                "calendardate": date(2020, 3, 31),
                "datekey": None,
                "reportperiod": date(2020, 3, 31),
                "lastupdated": date(2020, 4, 16),
                "revenue": 100.0,
                "netinc": 10.0,
            }
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sf1DatekeyNonNullAfter1990Contract().check({"sf1": sf1})
    message = str(exc_info.value)
    assert "sf1_datekey_non_null_after_1990" in message
    assert "BUG" in message


def test_sf1_datekey_non_null_after_1990_ignores_pre_1991_rows() -> None:
    """Pre-1991 calendardate rows with NULL datekey are sparse legacy data
    explicitly out of scope per the "after 1990" qualifier; the contract
    skips them.
    """
    sf1 = pl.DataFrame(
        [
            {
                "ticker": "OLD",
                "dimension": "ARQ",
                "calendardate": date(1985, 3, 31),
                "datekey": None,
                "reportperiod": date(1985, 3, 31),
                "lastupdated": date(1985, 4, 16),
                "revenue": 50.0,
                "netinc": 5.0,
            }
        ]
    )
    Sf1DatekeyNonNullAfter1990Contract().check({"sf1": sf1})


def test_sf1_datekey_non_null_after_1990_ignores_non_arq_dimensions() -> None:
    """The contract is text-locked to ARQ; an ART row with NULL datekey
    passes (ART has its own enforcement path via the read_sf1_arq
    dimension reader's normalization, not this contract).
    """
    sf1 = pl.DataFrame(
        [
            {
                "ticker": "X",
                "dimension": "ART",
                "calendardate": date(2020, 3, 31),
                "datekey": None,
                "reportperiod": date(2020, 3, 31),
                "lastupdated": date(2020, 4, 16),
                "revenue": 100.0,
                "netinc": 10.0,
            }
        ]
    )
    Sf1DatekeyNonNullAfter1990Contract().check({"sf1": sf1})


# ----- Contract 4: NoDuplicateTickerDatekeyInSf1Contract -----


def test_no_duplicate_ticker_datekey_in_sf1_passes_on_clean_bundle() -> None:
    NoDuplicateTickerDatekeyInSf1Contract().check(
        {"sf1": pl.DataFrame(_SF1_ROWS)}
    )


def test_no_duplicate_ticker_datekey_in_sf1_fails_on_duplicate_triple() -> None:
    """Two rows with identical (ticker, datekey, dimension) triple is the
    target bug class. Vendor restatement is in-place so duplicates
    should never exist.
    """
    base = {
        "ticker": "DUP",
        "dimension": "ARQ",
        "calendardate": date(2020, 3, 31),
        "datekey": date(2020, 4, 15),
        "reportperiod": date(2020, 3, 31),
        "lastupdated": date(2020, 4, 16),
        "revenue": 100.0,
        "netinc": 10.0,
    }
    sf1 = pl.DataFrame([base, {**base, "revenue": 200.0}])
    with pytest.raises(DataQualityError) as exc_info:
        NoDuplicateTickerDatekeyInSf1Contract().check({"sf1": sf1})
    message = str(exc_info.value)
    assert "no_duplicate_ticker_datekey_in_sf1" in message
    assert "DUP" in message


def test_no_duplicate_ticker_datekey_in_sf1_dimension_discriminates_duplicates() -> None:
    """ARQ + ART rows that legitimately share a (ticker, datekey) pair
    must NOT register as duplicates (they describe different facts at
    the same filing moment).
    """
    base = {
        "ticker": "OK",
        "calendardate": date(2020, 3, 31),
        "datekey": date(2020, 4, 15),
        "reportperiod": date(2020, 3, 31),
        "lastupdated": date(2020, 4, 16),
        "revenue": 100.0,
        "netinc": 10.0,
    }
    sf1 = pl.DataFrame(
        [
            {**base, "dimension": "ARQ"},
            {**base, "dimension": "ART", "revenue": 400.0},
        ]
    )
    NoDuplicateTickerDatekeyInSf1Contract().check({"sf1": sf1})


# ----- Contract 5: Sp500SnapshotMembersResolveContract (ADR 0017) -----


def test_sp500_snapshot_members_resolve_passes_on_clean_bundle() -> None:
    """The shared fixture's snapshot members (SPY, AGG) each resolve to
    exactly one TICKERS permaticker."""
    frames = {
        "sp500": pl.DataFrame(_SP500_ROWS),
        "tickers": pl.DataFrame(_TICKERS_ROWS),
    }
    Sp500SnapshotMembersResolveContract().check(frames)


def test_sp500_snapshot_member_absent_from_tickers_fails() -> None:
    """A snapshot member ticker with no TICKERS row (n_permatickers == 0)."""
    sp500 = pl.DataFrame(
        [{"ticker": "ABSENT", "date": date(2010, 12, 31), "action": "historical"}]
    )
    tickers = pl.DataFrame(_TICKERS_ROWS)
    with pytest.raises(DataQualityError) as exc_info:
        Sp500SnapshotMembersResolveContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    message = str(exc_info.value)
    assert "sp500_snapshot_members_resolve_to_unique_ticker" in message
    assert "ABSENT" in message


def test_sp500_snapshot_member_ticker_reuse_fails() -> None:
    """A snapshot member ticker mapping to two distinct permatickers
    (n_permatickers > 1) is the ticker-reuse bug the universe cannot
    silently disambiguate."""
    sp500 = pl.DataFrame(
        [{"ticker": "REUSE", "date": date(2010, 12, 31), "action": "historical"}]
    )
    tickers = pl.DataFrame(
        {
            "ticker": ["REUSE", "REUSE"],
            "permaticker": [800, 801],
            "firstpricedate": [date(2000, 1, 3), date(2009, 1, 5)],
        }
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500SnapshotMembersResolveContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    message = str(exc_info.value)
    assert "REUSE" in message
    assert "n_permatickers" in message


def test_sp500_snapshot_member_with_null_firstpricedate_fails() -> None:
    """A snapshot member whose only TICKERS row has a NULL firstpricedate is
    dropped from the resolver index, so the universe would raise at
    members_at. The contract matches that filter (post-impl review Medium 1):
    such a member counts as n_permatickers == 0 and fails the gate rather
    than passing and deferring the failure to first use."""
    sp500 = pl.DataFrame(
        [{"ticker": "SHELL", "date": date(2010, 12, 31), "action": "historical"}]
    )
    tickers = pl.DataFrame(
        {
            "ticker": ["SHELL"],
            "permaticker": [950],
            "firstpricedate": [None],
        },
        schema={
            "ticker": pl.String,
            "permaticker": pl.Int64,
            "firstpricedate": pl.Date,
        },
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500SnapshotMembersResolveContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    assert "SHELL" in str(exc_info.value)


def test_sp500_snapshot_member_boundary_date_outside_price_interval_passes() -> None:
    """ADR 0017 no-masking-but-tolerant regression: a snapshot whose date
    sits a few days outside the member's price interval (a spin-off listed
    just before its first regular-way bar, mirroring TDC 2007-09-30 vs
    firstprice 2007-10-02) still PASSES, because resolution is by ticker
    string and the member maps to exactly one permaticker. Only added/removed
    EVENT-vs-snapshot consistency uses dates (the cross-check contract)."""
    sp500 = pl.DataFrame(
        [{"ticker": "TDC", "date": date(2007, 9, 30), "action": "historical"}]
    )
    tickers = pl.DataFrame(
        {
            "ticker": ["TDC"],
            "permaticker": [700],
            # firstpricedate AFTER the snapshot date; would fail a
            # date-interval-contains check, passes ticker-string resolution.
            "firstpricedate": [date(2007, 10, 2)],
            "lastpricedate": [None],
        }
    )
    # No raise.
    Sp500SnapshotMembersResolveContract().check({"sp500": sp500, "tickers": tickers})


# ----- Contract 7: Sp500AddedRemovedCrossCheckContract (ADR 0017) -----


def _xcheck_sep(dates: list[date]) -> pl.DataFrame:
    """Minimal SEP frame (the cross-check reads only `date` for the window)."""
    return pl.DataFrame({"date": dates})


def test_sp500_added_removed_cross_check_passes_on_clean_bundle() -> None:
    """The shared fixture's add/drop events reconcile with its snapshots."""
    frames = {
        "sp500": pl.DataFrame(_SP500_ROWS),
        "sep": _xcheck_sep([date(1994, 1, 3), date(2024, 3, 18)]),
    }
    Sp500AddedRemovedCrossCheckContract().check(frames)


def test_sp500_added_removed_cross_check_fails_on_removed_still_present() -> None:
    """A `removed` event whose ticker is still in the next snapshot, with no
    re-add, is a real disagreement between the two representations."""
    sp500 = pl.DataFrame(
        [
            {"ticker": "FOO", "date": date(2010, 3, 31), "action": "historical"},
            {"ticker": "FOO", "date": date(2010, 6, 30), "action": "historical"},
            {"ticker": "FOO", "date": date(2010, 5, 1), "action": "removed"},
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500AddedRemovedCrossCheckContract().check(
            {"sp500": sp500, "sep": _xcheck_sep([date(2010, 1, 1), date(2010, 12, 31)])}
        )
    message = str(exc_info.value)
    assert "sp500_added_removed_consistent_with_snapshots" in message
    assert "FOO" in message


def test_sp500_added_removed_cross_check_fails_on_added_absent() -> None:
    """An `added` event whose ticker is absent from the next snapshot, with
    no offsetting removal, is a real disagreement."""
    sp500 = pl.DataFrame(
        [
            {"ticker": "FOO", "date": date(2010, 3, 31), "action": "historical"},
            {"ticker": "FOO", "date": date(2010, 6, 30), "action": "historical"},
            {"ticker": "BAR", "date": date(2010, 5, 1), "action": "added"},
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500AddedRemovedCrossCheckContract().check(
            {"sp500": sp500, "sep": _xcheck_sep([date(2010, 1, 1), date(2010, 12, 31)])}
        )
    assert "BAR" in str(exc_info.value)


def test_sp500_added_removed_cross_check_within_quarter_churn_passes() -> None:
    """A ticker added then removed inside one inter-snapshot window (so
    absent from both bracketing snapshots) is exempted, not a violation
    (the SOLS/BMS intra-quarter-churn pattern from the real bundle)."""
    sp500 = pl.DataFrame(
        [
            {"ticker": "FOO", "date": date(2010, 3, 31), "action": "historical"},
            {"ticker": "FOO", "date": date(2010, 6, 30), "action": "historical"},
            {"ticker": "BAR", "date": date(2010, 4, 15), "action": "added"},
            {"ticker": "BAR", "date": date(2010, 5, 20), "action": "removed"},
        ]
    )
    # No raise: the added BAR is offset by the removed BAR within the window.
    Sp500AddedRemovedCrossCheckContract().check(
        {"sp500": sp500, "sep": _xcheck_sep([date(2010, 1, 1), date(2010, 12, 31)])}
    )


def test_sp500_added_removed_cross_check_events_outside_sep_window_skipped() -> None:
    """Events outside the SEP price window (e.g. pre-price-era adds) are not
    reconciled and never raise."""
    sp500 = pl.DataFrame(
        [
            {"ticker": "FOO", "date": date(2010, 3, 31), "action": "historical"},
            # Pre-window add for a ticker never in a snapshot; out of scope.
            {"ticker": "OLD", "date": date(1990, 1, 1), "action": "added"},
        ]
    )
    Sp500AddedRemovedCrossCheckContract().check(
        {"sp500": sp500, "sep": _xcheck_sep([date(2004, 1, 2), date(2024, 3, 18)])}
    )


# ----- Runner aggregation -----


def test_runner_all_pass_logs_pass_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Full M3 superset bundle: all 5 contracts run and pass; the runner
    emits one INFO "all N contracts passed" message.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    with caplog.at_level(logging.INFO, logger="pit_backtest.data.contracts"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    assert any(
        "all 7 data quality contracts passed" in record.message
        for record in caplog.records
    )


def test_runner_one_failing_contract_raises_with_name(
    tmp_path: Path,
) -> None:
    """An SP500 snapshot listing a ticker that has no TICKERS row trips the
    snapshot-resolve contract only; the other 6 pass; the aggregated error
    names the failing contract.
    """
    # Build an inline M3 bundle where an SP500 snapshot references a ticker
    # absent from TICKERS so the snapshot-resolve contract fails. No
    # added/removed events, so the cross-check has nothing to reconcile.
    bundle_name = "sharadar_runner_one_fail"
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    bad_sp500 = [
        # Valid SPY snapshot member (resolves to AssetId 100).
        {"ticker": "SPY", "date": date(2010, 12, 31), "action": "historical"},
        # Invalid: ABSENT has no TICKERS row.
        {"ticker": "ABSENT", "date": date(2010, 12, 31), "action": "historical"},
    ]

    tables: dict[str, list[dict[str, object]]] = {
        "sep": _SEP_ROWS,
        "tickers": _TICKERS_ROWS,
        "sf1": _SF1_ROWS,
        "sp500": bad_sp500,
    }
    file_lines: list[str] = []
    for table, rows in tables.items():
        df = pl.DataFrame(rows)
        path = bundle_dir / f"{table}.parquet"
        df.write_parquet(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        file_lines.append(
            f'"{table}.parquet" = {{ sha256 = "{sha}", '
            f"size_bytes = {size}, row_count = {len(rows)} }}"
        )
    files_block = "\n".join(file_lines)
    manifest_content = f"""
[snapshots.{bundle_name}]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.{bundle_name}.files]
{files_block}
"""
    (snapshots_root / "manifest.toml").write_text(
        manifest_content, encoding="utf-8"
    )
    with pytest.raises(DataQualityError) as exc_info:
        SharadarDataSource(bundle_name, snapshots_root)
    message = str(exc_info.value)
    assert "sp500_snapshot_members_resolve_to_unique_ticker" in message
    assert "ABSENT" in message
    # The other 6 contracts passed; their names must NOT be in the
    # aggregated message.
    assert "tickers_first_price_within_five_days" not in message
    assert "no_sep_bars_after_delisting" not in message
    assert "sf1_datekey_non_null_after_1990" not in message
    assert "no_duplicate_ticker_datekey_in_sf1" not in message
    assert "no_duplicate_sp500_events" not in message
    assert "sp500_added_removed_consistent_with_snapshots" not in message


def test_runner_aggregated_message_sorts_failing_contracts_by_name() -> None:
    """Plan-reviewer Choice B addendum: the aggregated message lists
    failing contracts in alphabetical order so log-pattern alerts are
    stable across runs.
    """

    # Construct a stub source-like object so we exercise the runner
    # without the SharadarDataSource construction path.
    class _StubSource:
        bundle_name = "stub"
        available_tables = frozenset({"sf1"})

        def get_table(self, name: str) -> pl.LazyFrame:
            # Two duplicates so Contract 4 fails; same row has NULL
            # datekey so Contract 3 fails too.
            return pl.LazyFrame(
                [
                    {
                        "ticker": "DUP",
                        "dimension": "ARQ",
                        "calendardate": date(2020, 3, 31),
                        "datekey": None,
                        "reportperiod": date(2020, 3, 31),
                        "lastupdated": date(2020, 4, 16),
                        "revenue": 100.0,
                        "netinc": 10.0,
                    },
                    {
                        "ticker": "DUP",
                        "dimension": "ARQ",
                        "calendardate": date(2020, 3, 31),
                        "datekey": None,
                        "reportperiod": date(2020, 3, 31),
                        "lastupdated": date(2020, 4, 16),
                        "revenue": 200.0,
                        "netinc": 20.0,
                    },
                ]
            )

    with pytest.raises(DataQualityError) as exc_info:
        run_data_quality_contracts(_StubSource())  # type: ignore[arg-type]
    message = str(exc_info.value)
    # Both names present.
    assert "no_duplicate_ticker_datekey_in_sf1" in message
    assert "sf1_datekey_non_null_after_1990" in message
    # Alphabetical order: 'no_duplicate...' < 'sf1_...'
    idx_dup = message.index("no_duplicate_ticker_datekey_in_sf1")
    idx_null = message.index("sf1_datekey_non_null_after_1990")
    assert idx_dup < idx_null


def test_runner_skips_contracts_whose_required_tables_are_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default _write_synthetic_bundle ships SEP + ACTIONS only. All seven
    contracts require at least one table beyond that pair; the runner
    skips each with an INFO log; construction succeeds.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path)
    with caplog.at_level(logging.INFO, logger="pit_backtest.data.contracts"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    skipped_names = [
        record.message
        for record in caplog.records
        if "skipping data quality contract" in record.message
    ]
    # All 7 contracts skipped.
    assert any("tickers_first_price_within_five_days" in m for m in skipped_names)
    assert any("no_sep_bars_after_delisting" in m for m in skipped_names)
    assert any("sf1_datekey_non_null_after_1990" in m for m in skipped_names)
    assert any("no_duplicate_ticker_datekey_in_sf1" in m for m in skipped_names)
    assert any(
        "sp500_snapshot_members_resolve_to_unique_ticker" in m
        for m in skipped_names
    )
    assert any("no_duplicate_sp500_events" in m for m in skipped_names)
    assert any(
        "sp500_added_removed_consistent_with_snapshots" in m
        for m in skipped_names
    )


# ----- Freshness check -----


def _bundle_entry_with_pull_date(pull_date: date) -> SnapshotBundleEntry:
    return SnapshotBundleEntry(
        source="sharadar",
        pull_date=pull_date,
        files={
            "sep.parquet": SnapshotFileEntry(
                sha256="a" * 64, size_bytes=1, row_count=1
            ),
        },
        notes="test",
    )


def _frozen_now(target: datetime) -> Callable[[], datetime]:
    return lambda: target


def test_freshness_under_30_days_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle_entry = _bundle_entry_with_pull_date(date(2026, 5, 15))
    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.contracts"):
        check_snapshot_freshness(
            bundle_entry, now=_frozen_now(datetime(2026, 5, 30, 16, 0))
        )
    assert all(
        "snapshot is" not in record.message for record in caplog.records
    )


def test_freshness_30_to_89_days_emits_normal_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 2026-03-15 to 2026-05-30 = 76 days (in the 30-89 tier).
    bundle_entry = _bundle_entry_with_pull_date(date(2026, 3, 15))
    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.contracts"):
        check_snapshot_freshness(
            bundle_entry, now=_frozen_now(datetime(2026, 5, 30, 16, 0))
        )
    relevant = [
        r for r in caplog.records if "snapshot is" in r.message
    ]
    assert len(relevant) == 1
    assert "consider refreshing" in relevant[0].message
    assert "STALE" not in relevant[0].message


def test_freshness_at_or_over_90_days_emits_stale_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle_entry = _bundle_entry_with_pull_date(date(2026, 2, 25))
    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.contracts"):
        check_snapshot_freshness(
            bundle_entry, now=_frozen_now(datetime(2026, 5, 30, 16, 0))
        )
    relevant = [
        r for r in caplog.records if "snapshot is" in r.message
    ]
    assert len(relevant) == 1
    assert "STALE" in relevant[0].message


def test_freshness_pull_date_in_future_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clock skew (pull_date > now) is anomalous but not a freshness
    failure; the helper returns silently rather than raising.
    """
    bundle_entry = _bundle_entry_with_pull_date(date(2030, 1, 1))
    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.contracts"):
        check_snapshot_freshness(
            bundle_entry, now=_frozen_now(datetime(2026, 5, 30, 16, 0))
        )
    assert all(
        "snapshot is" not in record.message for record in caplog.records
    )


def test_freshness_rejects_aware_datetime_from_now_callable() -> None:
    """A future caller attaching tzinfo to one side would silently break
    the (current_dt - pull_dt).days subtraction; the helper asserts both
    sides naive per ADR 0002 dec 11.
    """
    import zoneinfo

    bundle_entry = _bundle_entry_with_pull_date(date(2026, 5, 15))
    aware_now = datetime(
        2026, 5, 30, 16, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York")
    )
    with pytest.raises(ValueError, match="naive datetime"):
        check_snapshot_freshness(bundle_entry, now=_frozen_now(aware_now))


# ----- SharadarDataSource integration smoke -----


def test_sharadar_data_source_init_runs_contracts_on_full_m3_bundle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Constructing the adapter against the full M3 superset succeeds and
    emits the "all contracts passed" INFO message.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    with caplog.at_level(logging.INFO, logger="pit_backtest.data.contracts"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    assert any(
        "all 7 data quality contracts passed" in record.message
        for record in caplog.records
    )


def test_sharadar_data_source_init_skips_contracts_on_m1_two_table_bundle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """M1 SPY-only bundle (SEP + ACTIONS only) succeeds with 5 skip-INFO
    logs; construction does not fail despite missing TICKERS / SF1 /
    SP500. This is the M1-backcompat guarantee.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path)
    with caplog.at_level(logging.INFO, logger="pit_backtest.data.contracts"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    skipped = [
        record.message
        for record in caplog.records
        if "skipping data quality contract" in record.message
    ]
    assert len(skipped) == 7


def test_sharadar_data_source_init_freshness_warns_loudly_on_stale_bundle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Build a bundle whose manifest pull_date is 100 days in the past
    relative to the test's current wall-clock. The freshness check
    fires at __init__ and the STALE warning is captured.

    The integration smoke test exercises the wiring; threshold-level
    determinism lives in the direct check_snapshot_freshness tests
    above (now-injection).
    """
    bundle_name = "sharadar_stale"
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    stale_pull_date = (datetime.now().date() - timedelta(days=100)).isoformat()

    # Write SEP + ACTIONS only so the contracts skip; the test isolates
    # the freshness branch.
    sep_df = pl.DataFrame(_SEP_ROWS)
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)
    actions_df = pl.DataFrame(
        [
            {
                "ticker": "SPY",
                "date": date(2024, 3, 15),
                "action": "dividend",
                "value": 1.7715,
            }
        ]
    )
    actions_path = bundle_dir / "actions.parquet"
    actions_df.write_parquet(actions_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.{bundle_name}]
source = "sharadar"
pull_date = {stale_pull_date}

[snapshots.{bundle_name}.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = {len(_SEP_ROWS)} }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_path.stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.contracts"):
        SharadarDataSource(bundle_name, snapshots_root)
    stale_messages = [
        record.message
        for record in caplog.records
        if "STALE" in record.message
    ]
    assert len(stale_messages) == 1


def test_init_wiring_does_not_break_existing_lookahead_gate_on_get_fundamental(
    tmp_path: Path,
) -> None:
    """Project rule 2D smoke: PR 5a wires contracts at __init__ but must
    not bypass the per-row PIT gate that PR 2 ships. After construction,
    `get_fundamental` with available_dt before any SF1 datekey returns
    None (the canonical PIT-gate behavior; not a leak).

    Full assertion logic lives in
    test_get_fundamental_available_dt_before_any_row_returns_none in
    test_sharadar_adapter.py; this test pins the wiring did not regress
    the gate.
    """
    from pit_backtest.data.records import AssetId

    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    # 2023-01-01 is before any SPY SF1 datekey in the fixture
    # (earliest datekey = 2024-01-15).
    result = adapter.get_fundamental(
        AssetId(100),
        available_dt=datetime(2023, 1, 1, 16, 0),
        field="revenue",
        flavor="ARQ",
    )
    assert result is None


# ============================================================================
# M3 PR 5b: NoDuplicateSp500EventsContract (Contract 6)
# ============================================================================


def test_no_duplicate_sp500_events_passes_on_clean_bundle() -> None:
    """Shared _SP500_ROWS has no duplicate (ticker, date, action) triples;
    Contract 6 returns None on success.
    """
    NoDuplicateSp500EventsContract().check(
        {"sp500": pl.DataFrame(_SP500_ROWS)}
    )


def test_no_duplicate_sp500_events_fails_on_duplicate_triple() -> None:
    """Two identical (SPY, 1995-09-19, 'added') rows is the target bug
    class. The error surfaces the count column so the operator sees
    the duplication factor.
    """
    sp500 = pl.DataFrame(
        [
            {"ticker": "SPY", "date": date(1995, 9, 19), "action": "added"},
            {"ticker": "SPY", "date": date(1995, 9, 19), "action": "added"},
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        NoDuplicateSp500EventsContract().check({"sp500": sp500})
    message = str(exc_info.value)
    assert "no_duplicate_sp500_events" in message
    assert "SPY" in message
    # Post-impl Low 3: assert the literal column key from to_dicts()
    # output so the assertion does not accidentally match the prose
    # "triple(s) in SP500" (which contains "count" as a substring via
    # the docstring of `_format_violation_message`'s "found N").
    assert "'count':" in message


def test_no_duplicate_sp500_events_dispatched_via_runner_when_sp500_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Full M3 superset bundle runs Contract 6 as part of _DEFAULT_CONTRACTS;
    the per-contract pass log includes the contract name.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    with caplog.at_level(logging.INFO, logger="pit_backtest.data.contracts"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    passed_messages = [
        record.message
        for record in caplog.records
        if "passed" in record.message and "no_duplicate_sp500_events" in record.message
    ]
    assert len(passed_messages) == 1


def test_aggregated_message_sorts_duplicate_event_before_resolution_failure() -> None:
    """When both the snapshot-resolve contract and the dedup contract fail,
    alphabetical sort in the runner places no_duplicate_sp500_events
    BEFORE sp500_snapshot_members_resolve_to_unique_ticker in the message.
    This is the user-message ordering that Plan-reviewer Medium 7 pinned
    as the intended PR 5b semantic (ADR 0017 keeps the ordering).
    """

    class _StubSource:
        bundle_name = "stub"
        available_tables = frozenset({"sp500", "tickers"})

        def get_table(self, name: str) -> pl.LazyFrame:
            if name == "sp500":
                # Duplicate snapshot row + unknown snapshot member so BOTH
                # SP500 contracts (dedup + snapshot-resolve) fail.
                return pl.LazyFrame(
                    [
                        {"ticker": "DUP", "date": date(2020, 3, 31), "action": "historical"},
                        {"ticker": "DUP", "date": date(2020, 3, 31), "action": "historical"},
                        {"ticker": "ABSENT", "date": date(2020, 6, 30), "action": "historical"},
                    ]
                )
            # Empty tickers so the snapshot members fail to resolve.
            return pl.LazyFrame(
                schema={
                    "ticker": pl.String, "permaticker": pl.Int64,
                    "firstpricedate": pl.Date, "lastpricedate": pl.Date,
                },
            )

    with pytest.raises(DataQualityError) as exc_info:
        run_data_quality_contracts(_StubSource())  # type: ignore[arg-type]
    message = str(exc_info.value)
    assert "no_duplicate_sp500_events" in message
    assert "sp500_snapshot_members_resolve_to_unique_ticker" in message
    idx_dup = message.index("no_duplicate_sp500_events")
    idx_resolve = message.index("sp500_snapshot_members_resolve_to_unique_ticker")
    assert idx_dup < idx_resolve
