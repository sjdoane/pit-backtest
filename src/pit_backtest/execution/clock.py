"""Clock protocol, TestClock, LiveClock.

Per ADR 0003 decision 7: Clock includes now, is_market_open, next_bar.
Calendar and Clock collapse into Clock; the pandas-market-calendars NYSE
backing is hidden behind the interface.

Per ADR 0001 decision 5: the backtest and any future live execution share
the same engine kernel; only the Clock implementation differs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol

import pandas_market_calendars as mcal  # type: ignore[import-untyped]

from pit_backtest.utils.timezones import ensure_ny, to_nyse_close


# pandas-market-calendars valid_days is expensive to recompute; the TestClock
# caches its trading-day index at construction. The cache window extends
# CACHE_PADDING_DAYS days beyond end_dt so next_bar() lookups near the end
# of the window still find a successor day.
_CACHE_PADDING_DAYS = 14


class Clock(Protocol):
    """Time source injected at engine construction."""

    def now(self) -> datetime:
        """Current simulation time (TestClock) or wall-clock (LiveClock)."""
        ...

    def is_market_open(self, dt: datetime) -> bool:
        """True if NYSE is open at dt (regular session, not pre/post)."""
        ...

    def next_bar(self, dt: datetime) -> datetime:
        """The next bar boundary after dt; for daily bars, the next NYSE
        trading day's close in America/New_York.
        """
        ...


class TestClock:
    """Simulated clock controlled by the BarLoop driver.

    NYSE trading-day calendar is loaded once at construction over
    [start_dt - padding, end_dt + padding] and cached as a sorted tuple of
    `date` values. is_market_open and next_bar are O(log N) binary searches
    against the cache.

    For daily bars, the convention is to align `now()` to 16:00 America/New_York
    on the current trading day. advance_to(dt) coerces dt to America/New_York
    and re-aligns to the next valid trading-day close if dt falls on a
    non-trading day (the safe default; raises if the caller's intent is
    ambiguous).
    """

    # Tell pytest not to collect this class as a test class. The leading
    # "Test" is part of the production API name (Clock + Test variant);
    # pytest's class-collection heuristic is overzealous here.
    __test__ = False

    def __init__(self, start_dt: date | datetime, end_dt: date | datetime) -> None:
        start_d = _to_date(start_dt)
        end_d = _to_date(end_dt)
        if end_d < start_d:
            raise ValueError(f"end_dt {end_d} precedes start_dt {start_d}")

        cache_start = start_d - timedelta(days=_CACHE_PADDING_DAYS)
        cache_end = end_d + timedelta(days=_CACHE_PADDING_DAYS)
        nyse = mcal.get_calendar("NYSE")
        valid = nyse.valid_days(start_date=cache_start, end_date=cache_end)
        # valid_days returns a pandas DatetimeIndex of UTC-midnight Timestamps
        # where the date portion identifies the trading day (NYSE local). The
        # raw .date() on a UTC-midnight Timestamp returns the trading day
        # directly; converting to America/New_York first would underflow
        # by one calendar day (UTC midnight = 19:00 or 20:00 previous-day ET).
        self._trading_days: tuple[date, ...] = tuple(ts.date() for ts in valid)
        if not self._trading_days:
            raise ValueError(
                f"no NYSE trading days in window [{cache_start}, {cache_end}]"
            )

        # Initial position: align to the first trading day >= start_d.
        self._now: datetime = to_nyse_close(
            self._first_trading_day_on_or_after(start_d)
        )

    def advance_to(self, dt: date | datetime) -> None:
        """Set the current simulation time to the 16:00 ET close of dt.

        dt is coerced to America/New_York. If dt's date is not a trading
        day, the next trading day's close is used; this is the safe
        default for ambiguous calls (e.g., advancing to a Saturday lands
        on Monday's close). The caller is expected to drive advance_to
        with trading-day dates from the cached calendar.
        """
        if isinstance(dt, datetime):
            d = ensure_ny(dt).date()
        else:
            d = dt
        target = self._first_trading_day_on_or_after(d)
        self._now = to_nyse_close(target)

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, dt: datetime) -> bool:
        """True if dt's date is an NYSE trading day in the cached window.

        For daily-bar use the regular-session boundaries (09:30-16:00 ET)
        are not enforced; any time on a trading day returns True. The
        method exists for the v1.1 LiveClock to override with intraday
        hours.
        """
        return self._is_trading_day(ensure_ny(dt).date())

    def next_bar(self, dt: datetime) -> datetime:
        """Return the next NYSE trading day's 16:00 ET close strictly after dt."""
        current = ensure_ny(dt).date()
        idx = _bisect_right(self._trading_days, current)
        if idx >= len(self._trading_days):
            raise ValueError(
                f"no trading day after {current} in the cached window; "
                f"construct TestClock with a later end_dt"
            )
        return to_nyse_close(self._trading_days[idx])

    def trading_days(self) -> tuple[date, ...]:
        """All trading days in the cached window (sorted, immutable).

        Used by the BarLoop to drive its iteration. Returned as a tuple so
        callers cannot mutate the cache.
        """
        return self._trading_days

    def _is_trading_day(self, d: date) -> bool:
        idx = _bisect_left(self._trading_days, d)
        return idx < len(self._trading_days) and self._trading_days[idx] == d

    def _first_trading_day_on_or_after(self, d: date) -> date:
        idx = _bisect_left(self._trading_days, d)
        if idx >= len(self._trading_days):
            raise ValueError(
                f"no trading day on or after {d} in the cached window; "
                f"construct TestClock with a later end_dt"
            )
        return self._trading_days[idx]


class LiveClock:
    """Real wall-clock; not used at v1.

    Stub exists so the kernel-sharing pattern from ADR 0001 decision 5 is
    visible in the architecture. Per ADR 0003 decision 23, v1.2 work
    replaces the BarLoop with an event-driven loop driven by a
    LiveBrokerClient; LiveClock is the time-source half of that work.
    """

    def now(self) -> datetime:
        raise NotImplementedError("v1.1")

    def is_market_open(self, dt: datetime) -> bool:
        raise NotImplementedError("v1.1")

    def next_bar(self, dt: datetime) -> datetime:
        raise NotImplementedError("v1.1")


def _to_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _bisect_left(seq: tuple[date, ...], target: date) -> int:
    """Stdlib bisect_left specialized for date tuples (avoids the bisect
    module's `key=` overhead and keeps the helper greppable).
    """
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(seq: tuple[date, ...], target: date) -> int:
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo
