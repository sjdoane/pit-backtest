"""Rebalance calendar helpers.

Per ADR 0004 (rebalance calendar independence): rebalance dates are fund-
policy-determined functions of the NYSE trading calendar, computed once
at backtest construction and shared as a frozenset[date] between the
Policy and any reference function or test fixture.

monthly_last_trading_day is the v1 helper for "last NYSE trading day of
each calendar month observed in the input." Quarterly, weekly, and
custom-cadence helpers can land alongside as needed; the API shape is
the same: take a sorted tuple of trading days, return a frozenset of
rebalance dates.
"""

from __future__ import annotations

from datetime import date


def monthly_last_trading_day(trading_days: tuple[date, ...]) -> frozenset[date]:
    """Return the last NYSE trading day of each calendar month in the input.

    Input must be sorted ascending; output is a frozenset[date]. Empty
    input returns an empty frozenset.

    The function does NOT bracket the result to any window; the caller
    decides which subset of trading_days to pass. Per ADR 0004, the
    Backtest computes the tuple from TestClock.trading_days() over a
    window WIDER than the backtest, then trims at consumption time.

    Edge cases:
    - Month with only one trading day: that day is the last-of-month.
    - First or last calendar day of a month falling on a weekend: the
      preceding/following trading day inside the month is returned.
    - Empty input: empty frozenset (not a raise; callers may pass empty
      tuples during validation passes).
    """
    if not trading_days:
        return frozenset()
    result: list[date] = []
    prev_year_month: tuple[int, int] | None = None
    prev_day: date | None = None
    for d in trading_days:
        ym = (d.year, d.month)
        if prev_year_month is not None and ym != prev_year_month:
            if prev_day is None:
                raise AssertionError("prev_day is None despite prev_year_month being set")
            result.append(prev_day)
        prev_year_month = ym
        prev_day = d
    # Final month's last day
    if prev_day is not None:
        result.append(prev_day)
    return frozenset(result)
