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
    NoDuplicateTickerDatekeyInSf1Contract,
    NoSepBarsAfterDelistingContract,
    Sf1DatekeyNonNullAfter1990Contract,
    Sp500EventsResolveToUniqueTickersRowContract,
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
    permaticker + ticker + firstpricedate surfaced.
    """
    tickers = _tickers_frame_with_one_row(
        permaticker=42, ticker="GHOST", firstpricedate=date(2024, 4, 2)
    )
    sep = _empty_sep_frame()
    with pytest.raises(DataQualityError) as exc_info:
        FirstPriceWithinFiveDaysContract().check({"tickers": tickers, "sep": sep})
    message = str(exc_info.value)
    assert "tickers_first_price_within_five_days" in message
    assert "GHOST" in message
    assert "42" in message
    # `.to_dicts()` surfaces dates via their `repr()` (`datetime.date(2024, 4, 2)`)
    # rather than ISO format; assert against the literal repr.
    assert "datetime.date(2024, 4, 2)" in message


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


# ----- Contract 5: Sp500EventsResolveToUniqueTickersRowContract -----


def test_sp500_events_resolve_to_unique_tickers_row_passes_on_clean_bundle() -> None:
    """The shared fixture's SP500 events all resolve cleanly: SPY 1995-09-19
    is inside SPY's TICKERS interval (1993-01-22, open); AGG 2010-06-15
    and AGG 2015-12-31 are inside AGG's TICKERS interval (2003-09-22, open).
    """
    frames = {
        "sp500": pl.DataFrame(_SP500_ROWS),
        "tickers": pl.DataFrame(_TICKERS_ROWS),
    }
    Sp500EventsResolveToUniqueTickersRowContract().check(frames)


def test_sp500_events_resolve_to_unique_tickers_row_fails_when_event_ticker_missing_from_tickers() -> None:
    """The SP500-coverage bug class: an event-log ticker with no TICKERS
    row at all (or none whose interval contains the event date) raises.
    """
    sp500 = pl.DataFrame(
        [{"ticker": "ABSENT", "date": date(2010, 1, 1), "action": "added"}]
    )
    tickers = pl.DataFrame(_TICKERS_ROWS)
    with pytest.raises(DataQualityError) as exc_info:
        Sp500EventsResolveToUniqueTickersRowContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    message = str(exc_info.value)
    assert "sp500_events_resolve_to_unique_tickers_row" in message
    assert "ABSENT" in message


def test_sp500_events_resolve_to_unique_tickers_row_fails_on_ticker_reuse_multi_match() -> None:
    """The Plan-reviewer High 4 reframing: a ticker string with two
    TICKERS rows whose intervals both contain the event date is a
    vendor bug (we cannot resolve which permaticker the event references).
    """
    sp500 = pl.DataFrame(
        [{"ticker": "REUSE", "date": date(2010, 6, 15), "action": "added"}]
    )
    tickers = pl.DataFrame(
        [
            {
                "permaticker": 800, "ticker": "REUSE",
                "name": "First Issue", "exchange": "NYSE", "isdelisted": "N",
                "firstpricedate": date(2005, 1, 1), "lastpricedate": None,
                "firstquarter": date(2005, 3, 31), "lastquarter": None,
                "cusip": "REUSE0001",
            },
            {
                "permaticker": 801, "ticker": "REUSE",
                "name": "Second Issue", "exchange": "NASDAQ", "isdelisted": "N",
                "firstpricedate": date(2009, 1, 1), "lastpricedate": None,
                "firstquarter": date(2009, 3, 31), "lastquarter": None,
                "cusip": "REUSE0002",
            },
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500EventsResolveToUniqueTickersRowContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    message = str(exc_info.value)
    assert "REUSE" in message
    assert "match_count" in message


def test_sp500_events_resolve_to_unique_tickers_row_event_outside_interval_fails() -> None:
    """An SP500 event date outside any TICKERS row's
    [firstpricedate, lastpricedate] interval is a no-match case.
    """
    sp500 = pl.DataFrame(
        [{"ticker": "EARLY", "date": date(2000, 1, 1), "action": "added"}]
    )
    tickers = pl.DataFrame(
        [
            {
                "permaticker": 900, "ticker": "EARLY",
                "name": "Early Co", "exchange": "NYSE", "isdelisted": "N",
                "firstpricedate": date(2010, 1, 4), "lastpricedate": None,
                "firstquarter": date(2010, 3, 31), "lastquarter": None,
                "cusip": "EARLY0001",
            }
        ]
    )
    with pytest.raises(DataQualityError) as exc_info:
        Sp500EventsResolveToUniqueTickersRowContract().check(
            {"sp500": sp500, "tickers": tickers}
        )
    assert "EARLY" in str(exc_info.value)


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
        "all 5 data quality contracts passed" in record.message
        for record in caplog.records
    )


def test_runner_one_failing_contract_raises_with_name(
    tmp_path: Path,
) -> None:
    """An SP500 row referencing a ticker that has no TICKERS coverage at
    the event date trips Contract 5 only; the other 4 pass; the
    aggregated error names the failing contract.
    """
    # Build an inline M3 bundle where SP500 references a ticker absent
    # from TICKERS so Contract 5 fails.
    bundle_name = "sharadar_runner_one_fail"
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    bad_sp500 = [
        # Valid SPY 1995-09-19 (matches shared fixture).
        {"ticker": "SPY", "date": date(1995, 9, 19), "action": "added"},
        # Invalid: ABSENT has no TICKERS row.
        {"ticker": "ABSENT", "date": date(2010, 1, 1), "action": "added"},
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
    assert "sp500_events_resolve_to_unique_tickers_row" in message
    assert "ABSENT" in message
    # The other 4 contracts passed; their names must NOT be in the
    # aggregated message.
    assert "tickers_first_price_within_five_days" not in message
    assert "no_sep_bars_after_delisting" not in message
    assert "sf1_datekey_non_null_after_1990" not in message
    assert "no_duplicate_ticker_datekey_in_sf1" not in message


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
    """Default _write_synthetic_bundle ships SEP + ACTIONS only. All five
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
    # All 5 contracts skipped.
    assert any("tickers_first_price_within_five_days" in m for m in skipped_names)
    assert any("no_sep_bars_after_delisting" in m for m in skipped_names)
    assert any("sf1_datekey_non_null_after_1990" in m for m in skipped_names)
    assert any("no_duplicate_ticker_datekey_in_sf1" in m for m in skipped_names)
    assert any(
        "sp500_events_resolve_to_unique_tickers_row" in m
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
        "all 5 data quality contracts passed" in record.message
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
    assert len(skipped) == 5


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
