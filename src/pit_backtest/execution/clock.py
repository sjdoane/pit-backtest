"""Clock protocol, TestClock, LiveClock.

Per ADR 0003 decision 7: Clock includes now, is_market_open, next_bar.
Calendar and Clock collapse into Clock; the pandas-market-calendars NYSE
backing is hidden behind the interface.

Per ADR 0001 decision 5: the backtest and any future live execution share
the same engine kernel; only the Clock implementation differs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


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
    """Simulated clock controlled by the BarLoop driver."""

    def __init__(self, start_dt: datetime) -> None:
        raise NotImplementedError("M1 deliverable")

    def advance_to(self, dt: datetime) -> None:
        raise NotImplementedError("M1 deliverable")

    def now(self) -> datetime:
        raise NotImplementedError("M1 deliverable")

    def is_market_open(self, dt: datetime) -> bool:
        raise NotImplementedError("M1 deliverable")

    def next_bar(self, dt: datetime) -> datetime:
        raise NotImplementedError("M1 deliverable")


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
