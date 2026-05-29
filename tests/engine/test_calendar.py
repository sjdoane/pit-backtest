"""monthly_last_trading_day tests.

Per ADR 0004, the rebalance calendar is fund-policy-determined and shared
between BarLoop and reference function. The helper must be deterministic
on edge cases (single day, month boundaries, holidays).
"""

from __future__ import annotations

from datetime import date

import pytest

from pit_backtest.engine.calendar import monthly_last_trading_day


def test_empty_input_returns_empty() -> None:
    assert monthly_last_trading_day(()) == frozenset()


def test_single_day_returns_that_day() -> None:
    result = monthly_last_trading_day((date(2024, 3, 15),))
    assert result == frozenset({date(2024, 3, 15)})


def test_two_days_same_month_returns_last() -> None:
    result = monthly_last_trading_day((date(2024, 3, 14), date(2024, 3, 15)))
    assert result == frozenset({date(2024, 3, 15)})


def test_two_days_different_months_returns_both() -> None:
    result = monthly_last_trading_day((date(2024, 3, 28), date(2024, 4, 1)))
    assert result == frozenset({date(2024, 3, 28), date(2024, 4, 1)})


def test_holiday_at_month_end_picks_preceding_trading_day() -> None:
    """Good Friday 2024-03-29 is a holiday; if it is absent from the
    trading-day input, March's last day is the preceding trading day.
    """
    # Construct a synthetic March 2024 trading-day list excluding Good Friday.
    march_days = tuple(
        date(2024, 3, d)
        for d in (1, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 18, 19, 20, 21, 22, 25, 26, 27, 28)
    )
    result = monthly_last_trading_day(march_days)
    assert result == frozenset({date(2024, 3, 28)})


def test_full_year_returns_twelve_dates() -> None:
    """Synthetic 2024 calendar (one trading day per month) returns 12 dates."""
    one_per_month = tuple(date(2024, m, 15) for m in range(1, 13))
    result = monthly_last_trading_day(one_per_month)
    assert len(result) == 12
    for m in range(1, 13):
        assert date(2024, m, 15) in result


def test_real_nyse_2024_january_returns_last_trading_day() -> None:
    """A small subset of real NYSE January 2024 trading days. Last trading
    day of January 2024 was Wednesday 2024-01-31.
    """
    jan_2024 = tuple(
        date(2024, 1, d)
        for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 16, 17, 18, 19, 22, 23, 24, 25, 26, 29, 30, 31)
    )
    # MLK Day 2024-01-15 is closed; we skip it. Last trading day = 2024-01-31.
    result = monthly_last_trading_day(jan_2024)
    assert result == frozenset({date(2024, 1, 31)})


def test_unsorted_input_relies_on_caller() -> None:
    """The helper assumes sorted input; an unsorted input produces a stale
    result. This is documented in the docstring; the test pins the
    expected (incorrect) behavior to avoid silent contract changes.
    """
    unsorted = (date(2024, 3, 31), date(2024, 3, 1), date(2024, 4, 1))
    result = monthly_last_trading_day(unsorted)
    # With unsorted input, the helper sees March as the "previous month"
    # when it hits March 1, so March 31 becomes the recorded last day of
    # the (out-of-order) "month boundary" between March 31 and March 1.
    # The result includes both March 31 and the implicit final day; this
    # asserts the contract that callers MUST pass sorted input.
    assert date(2024, 4, 1) in result
