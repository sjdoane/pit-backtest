"""ExpenseRatioSchedule tests (ADR 0006).

Tests both the scalar `rate_for` lookup and the join_asof path inside
reconstruct_total_return. Boundary semantics are the key invariant:
`dt == effective_from` picks up the new rate (Polars
join_asof(strategy='backward') with effective_from on the right).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from pit_backtest.data.adjustments import (
    TRADING_DAYS_PER_YEAR,
    ExpenseRatioSchedule,
    ExpenseRatioStep,
    reconstruct_total_return,
)


# Two-step SPY schedule per ADR 0006. The boundary day 2003-11-01 is a
# Saturday; the first NYSE trading day on or after is 2003-11-03 (Mon).
SPY_TWO_STEP_SCHEDULE = ExpenseRatioSchedule(
    rows=(
        ExpenseRatioStep(effective_from=date(1993, 1, 22), rate=Decimal("0.0012")),
        ExpenseRatioStep(effective_from=date(2003, 11, 1), rate=Decimal("0.000945")),
    )
)


def test_construction_rejects_empty_rows() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        ExpenseRatioSchedule(rows=())


def test_construction_rejects_unsorted_rows() -> None:
    with pytest.raises(ValueError, match="sorted ascending"):
        ExpenseRatioSchedule(
            rows=(
                ExpenseRatioStep(effective_from=date(2003, 11, 1), rate=Decimal("0.000945")),
                ExpenseRatioStep(effective_from=date(1993, 1, 22), rate=Decimal("0.0012")),
            )
        )


def test_rate_for_returns_last_step_at_or_before_date() -> None:
    """Standard step-table coverage."""
    assert SPY_TWO_STEP_SCHEDULE.rate_for(date(1993, 1, 22)) == Decimal("0.0012")
    assert SPY_TWO_STEP_SCHEDULE.rate_for(date(1995, 6, 30)) == Decimal("0.0012")
    assert SPY_TWO_STEP_SCHEDULE.rate_for(date(2003, 10, 31)) == Decimal("0.0012")
    # Boundary: dt == effective_from picks up the new rate.
    assert SPY_TWO_STEP_SCHEDULE.rate_for(date(2003, 11, 1)) == Decimal("0.000945")
    assert SPY_TWO_STEP_SCHEDULE.rate_for(date(2026, 5, 29)) == Decimal("0.000945")


def test_rate_for_before_schedule_start_raises() -> None:
    """A date earlier than the schedule's first effective_from has no rate."""
    with pytest.raises(ValueError, match="precedes the schedule's first"):
        SPY_TWO_STEP_SCHEDULE.rate_for(date(1990, 1, 1))


def test_to_drag_frame_returns_polars_date_columns() -> None:
    """The drag frame must use pl.Date for join_asof compatibility."""
    frame = SPY_TWO_STEP_SCHEDULE.to_drag_frame()
    assert frame.columns == ["effective_from", "daily_drag"]
    assert frame.schema["effective_from"] == pl.Date
    assert frame.schema["daily_drag"] == pl.Float64
    assert frame.height == 2
    # Pre-step drag: 0.0012 / 252
    assert frame["daily_drag"][0] == pytest.approx(
        0.0012 / TRADING_DAYS_PER_YEAR, abs=1e-15
    )
    # Post-step drag: 0.000945 / 252
    assert frame["daily_drag"][1] == pytest.approx(
        0.000945 / TRADING_DAYS_PER_YEAR, abs=1e-15
    )


def test_rate_for_agrees_with_drag_frame_at_boundary_days() -> None:
    """The scalar rate_for and the drag-frame asof lookup must agree at
    every boundary day. Polars asof drift in a future version would fail
    this first.
    """
    drag_frame = SPY_TWO_STEP_SCHEDULE.to_drag_frame()
    boundary_days = (
        date(2003, 10, 31),  # last day of old rate
        date(2003, 11, 1),   # first day of new rate
        date(2003, 11, 3),   # first NYSE trading day with new rate
    )
    for d in boundary_days:
        scalar_rate = SPY_TWO_STEP_SCHEDULE.rate_for(d)
        scalar_drag = float(scalar_rate) / TRADING_DAYS_PER_YEAR
        asof_result = drag_frame.sort("effective_from").join_asof(
            pl.DataFrame({"dt": [d]}, schema={"dt": pl.Date}).sort("dt"),
            left_on="effective_from",
            right_on="dt",
            strategy="forward",
        )
        # Use the reverse direction: build a tiny query frame and asof
        # against the drag frame the same way reconstruct_total_return does.
        query = pl.DataFrame({"dt": [d]}, schema={"dt": pl.Date}).sort("dt")
        joined = query.join_asof(
            drag_frame.sort("effective_from"),
            left_on="dt",
            right_on="effective_from",
            strategy="backward",
        )
        join_drag = joined["daily_drag"][0]
        assert join_drag == pytest.approx(scalar_drag, abs=1e-15), (
            f"rate_for vs join_asof disagree at boundary day {d}: "
            f"scalar={scalar_drag}, join={join_drag}"
        )


def test_reconstruct_total_return_with_schedule_applies_step_at_boundary() -> None:
    """5-trading-day fixture spanning 2003-10-29..2003-11-04 verifies the
    boundary semantics: 10-30 and 10-31 use the 0.12% rate, 11-03 and
    11-04 use the 0.0945% rate (per ADR 0006 Author response item 3).

    Synthetic constant 0.03% daily return; no dividends.
    """
    prices = pl.DataFrame(
        {
            "dt": [
                date(2003, 10, 29),
                date(2003, 10, 30),
                date(2003, 10, 31),
                date(2003, 11, 3),
                date(2003, 11, 4),
            ],
            "close": [
                100.0,
                100.0 * 1.0003,
                100.0 * 1.0003 ** 2,
                100.0 * 1.0003 ** 3,
                100.0 * 1.0003 ** 4,
            ],
        }
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )

    tr_series = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2003, 10, 29),
        end_dt=date(2003, 11, 4),
        expense_ratio_annual=SPY_TWO_STEP_SCHEDULE,
    )

    pre_step_drag = 0.0012 / TRADING_DAYS_PER_YEAR
    post_step_drag = 0.000945 / TRADING_DAYS_PER_YEAR
    tr = tr_series["tr"].to_list()

    # Day 0 anchor.
    assert tr[0] == pytest.approx(1.0, abs=1e-12)
    # Day 1 (2003-10-30): 1.0003 * (1 - pre_step_drag)
    expected_mult_pre = 1.0003 * (1.0 - pre_step_drag)
    assert tr[1] == pytest.approx(expected_mult_pre, abs=1e-12)
    # Day 2 (2003-10-31): same multiplier again.
    assert tr[2] == pytest.approx(expected_mult_pre ** 2, abs=1e-12)
    # Day 3 (2003-11-03): post-step rate (Polars asof backward maps
    # 2003-11-03 to effective_from=2003-11-01).
    expected_mult_post = 1.0003 * (1.0 - post_step_drag)
    assert tr[3] == pytest.approx(
        expected_mult_pre ** 2 * expected_mult_post, abs=1e-12
    )
    # Day 4 (2003-11-04): post-step rate.
    assert tr[4] == pytest.approx(
        expected_mult_pre ** 2 * expected_mult_post ** 2, abs=1e-12
    )


def test_schedule_path_byte_for_byte_matches_scalar_for_single_step() -> None:
    """A single-step schedule with a date predating the window must
    produce TR values identical to the Decimal-scalar path (back-compat
    invariant; the equivalence holds because the scalar branch and the
    schedule branch do the same per-row arithmetic).
    """
    prices = pl.DataFrame(
        {
            "dt": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.00, 101.00, 102.00],
        }
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )
    scalar_rate = Decimal("0.000945")
    schedule = ExpenseRatioSchedule(
        rows=(ExpenseRatioStep(effective_from=date(1900, 1, 1), rate=scalar_rate),)
    )

    scalar_tr = reconstruct_total_return(
        prices, dividends, date(2024, 1, 2), date(2024, 1, 4), scalar_rate
    )
    schedule_tr = reconstruct_total_return(
        prices, dividends, date(2024, 1, 2), date(2024, 1, 4), schedule
    )
    for i in range(scalar_tr.height):
        assert scalar_tr["tr"][i] == pytest.approx(
            schedule_tr["tr"][i], abs=1e-12
        ), f"row {i} diverges: scalar={scalar_tr['tr'][i]}, schedule={schedule_tr['tr'][i]}"


def test_schedule_path_rejects_window_starting_before_first_step() -> None:
    """If the window's first trading day is before the schedule's first
    effective_from, the join would silently produce null daily_drag and
    the multiplier would become NaN. The function raises instead.
    """
    prices = pl.DataFrame(
        {"dt": [date(1990, 1, 2), date(1990, 1, 3)], "close": [100.0, 101.0]}
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )
    with pytest.raises(ValueError, match="first effective_from"):
        reconstruct_total_return(
            prices,
            dividends,
            date(1990, 1, 2),
            date(1990, 1, 3),
            SPY_TWO_STEP_SCHEDULE,
        )
