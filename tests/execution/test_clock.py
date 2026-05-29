"""TestClock tests.

NYSE 2024 holidays used as fixtures: New Year's Day (Mon 2024-01-01),
MLK Day (Mon 2024-01-15), Good Friday (Fri 2024-03-29), Memorial Day
(Mon 2024-05-27), Juneteenth (Wed 2024-06-19), Independence Day (Thu
2024-07-04), Labor Day (Mon 2024-09-02), Thanksgiving (Thu 2024-11-28),
Christmas (Wed 2024-12-25).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from pit_backtest.execution.clock import TestClock
from pit_backtest.utils.timezones import NEW_YORK


def test_clock_initial_position_aligns_to_first_trading_day() -> None:
    """Constructing with start_dt on a non-trading day (Saturday) snaps to
    the next trading day (Monday).
    """
    clock = TestClock(start_dt=date(2024, 1, 6), end_dt=date(2024, 1, 31))  # Sat
    # First trading day on or after 2024-01-06 is Mon 2024-01-08 (Jan 1
    # holiday, Jan 6 Sat, Jan 7 Sun).
    expected = datetime(2024, 1, 8, 16, 0, tzinfo=NEW_YORK)
    assert clock.now() == expected


def test_advance_to_known_trading_day() -> None:
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    clock.advance_to(date(2024, 3, 15))
    assert clock.now() == datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK)


def test_advance_to_weekend_snaps_forward() -> None:
    """advance_to on a Saturday lands on the following Monday's close."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    clock.advance_to(date(2024, 3, 16))  # Sat
    assert clock.now() == datetime(2024, 3, 18, 16, 0, tzinfo=NEW_YORK)


def test_advance_to_holiday_snaps_to_next_trading_day() -> None:
    """Good Friday 2024-03-29 is closed; advancing to it lands on Mon 2024-04-01."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 4, 5))
    clock.advance_to(date(2024, 3, 29))
    assert clock.now() == datetime(2024, 4, 1, 16, 0, tzinfo=NEW_YORK)


def test_is_market_open_trading_day() -> None:
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    assert clock.is_market_open(datetime(2024, 3, 15, 12, 0, tzinfo=NEW_YORK))


def test_is_market_open_weekend() -> None:
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    # Saturday 2024-03-16
    assert not clock.is_market_open(datetime(2024, 3, 16, 12, 0, tzinfo=NEW_YORK))


def test_is_market_open_known_holiday() -> None:
    """Good Friday 2024-03-29: NYSE closed."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 4, 5))
    assert not clock.is_market_open(datetime(2024, 3, 29, 12, 0, tzinfo=NEW_YORK))


def test_is_market_open_independence_day() -> None:
    """Independence Day 2024-07-04 (Thursday): NYSE closed."""
    clock = TestClock(start_dt=date(2024, 7, 1), end_dt=date(2024, 7, 10))
    assert not clock.is_market_open(datetime(2024, 7, 4, 12, 0, tzinfo=NEW_YORK))
    # Day before and day after are open.
    assert clock.is_market_open(datetime(2024, 7, 3, 12, 0, tzinfo=NEW_YORK))
    assert clock.is_market_open(datetime(2024, 7, 5, 12, 0, tzinfo=NEW_YORK))


def test_next_bar_skips_weekend() -> None:
    """next_bar from Friday's close jumps to Monday's close."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    friday_close = datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK)
    assert clock.next_bar(friday_close) == datetime(2024, 3, 18, 16, 0, tzinfo=NEW_YORK)


def test_next_bar_skips_holiday() -> None:
    """next_bar from Thursday before Good Friday jumps to following Monday."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 4, 5))
    thursday_close = datetime(2024, 3, 28, 16, 0, tzinfo=NEW_YORK)
    # Friday 3-29 closed; Sat-Sun closed; next trading day = Mon 4-1.
    assert clock.next_bar(thursday_close) == datetime(2024, 4, 1, 16, 0, tzinfo=NEW_YORK)


def test_next_bar_strictly_after() -> None:
    """next_bar from any time on day T returns the close of day T+1 trading."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    # Mid-day Friday: next bar is Monday's close.
    mid_friday = datetime(2024, 3, 15, 9, 30, tzinfo=NEW_YORK)
    assert clock.next_bar(mid_friday) == datetime(2024, 3, 18, 16, 0, tzinfo=NEW_YORK)


def test_naive_datetime_assumed_new_york() -> None:
    """Naive datetimes are interpreted as already in America/New_York."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    naive = datetime(2024, 3, 15, 16, 0)  # naive
    aware = datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK)
    assert clock.is_market_open(naive) == clock.is_market_open(aware)


def test_utc_datetime_converted_to_ny() -> None:
    """A UTC datetime is converted to America/New_York before lookup."""
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    # 2024-03-15 23:00 UTC = 2024-03-15 19:00 ET (still 2024-03-15 in NY).
    utc = datetime(2024, 3, 15, 23, 0, tzinfo=ZoneInfo("UTC"))
    assert clock.is_market_open(utc)


def test_trading_days_returned_sorted_and_immutable() -> None:
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))
    days = clock.trading_days()
    assert isinstance(days, tuple)
    assert days == tuple(sorted(days))
    # March 2024 has 21 trading days (no holidays).
    march_2024 = [d for d in days if d.month == 3 and d.year == 2024]
    assert len(march_2024) == 20  # Good Friday 3-29 closed; March 2024 has 20 trading days


def test_invalid_window_raises() -> None:
    with pytest.raises(ValueError, match="precedes start_dt"):
        TestClock(start_dt=date(2024, 3, 31), end_dt=date(2024, 3, 1))


def test_next_bar_past_end_raises() -> None:
    """next_bar at the cache window's far edge raises with an actionable
    diagnostic.
    """
    clock = TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 5))
    # Reach the last trading day in the cache, then ask for the next bar
    # past the padded window.
    last_day = clock.trading_days()[-1]
    with pytest.raises(ValueError, match="no trading day after"):
        clock.next_bar(datetime.combine(last_day, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=23, minute=59))
