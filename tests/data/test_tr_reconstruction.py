"""Total-return reconstruction tests.

Fixtures defined in docs/methodology/total_return_reconstruction.md
(Worked example A: toy three-day; Worked example B: SPY Q1 2024).

The toy fixture is exact-algebra and runs in CI. The SPY fixture is
gated on snapshot availability and is skipped in CI until the v1.1
snapshot-in-CI work lands.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import polars as pl
import pytest

from pit_backtest.data.adjustments import (
    annualized_return,
    reconstruct_total_return,
)


def test_toy_three_day_with_one_dividend() -> None:
    """Reproduces docs/methodology/total_return_reconstruction.md Worked
    example A. Day 2 has a 1.50 dividend; expense ratio is zero so the
    pure algebra is exercised.

    Expected TR series (from the methodology doc):
        Day 0 (2024-01-02): 1.000000
        Day 1 (2024-01-03): 1.010000  (price-only return)
        Day 2 (2024-01-04): 1.020000  (dividend reinvested)
        Day 3 (2024-01-05): 1.035224  (price move after reinvest)
    """
    prices = pl.DataFrame(
        {
            "dt": [
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
            "close": [100.00, 101.00, 100.50, 102.00],
        }
    )
    dividends = pl.DataFrame(
        {
            "ex_date": [date(2024, 1, 4)],
            "amount_per_share": [1.50],
        }
    )

    result = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 5),
        expense_ratio_annual=Decimal("0"),
    )

    tr = result["tr"].to_list()
    # Day 0: exact (reference).
    assert tr[0] == pytest.approx(1.000000, abs=1e-12)
    # Day 1: price-only return, exact rational.
    assert tr[1] == pytest.approx(1.010000, abs=1e-12)
    # Day 2: (101 * (102/101)) = 102 / 100, exact rational.
    assert tr[2] == pytest.approx(1.020000, abs=1e-12)
    # Day 3: 1.02 * (102 / 100.5) = 1.0352238805970149..., float-level match.
    assert tr[3] == pytest.approx(1.035223880597, abs=1e-10)

    daily_return = result["daily_return"].to_list()
    assert daily_return[0] == pytest.approx(0.0, abs=1e-12)
    assert daily_return[1] == pytest.approx(0.010000, abs=1e-12)
    # Day 2 multiplier is exactly 102/101.
    assert daily_return[2] == pytest.approx(102 / 101 - 1, abs=1e-12)
    assert daily_return[3] == pytest.approx(102 / 100.5 - 1, abs=1e-12)


def test_no_dividends_in_window() -> None:
    """Empty dividend frame produces pure price-return series."""
    prices = pl.DataFrame(
        {
            "dt": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.00, 101.00, 99.00],
        }
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )

    result = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 4),
        expense_ratio_annual=Decimal("0"),
    )

    tr = result["tr"].to_list()
    assert tr[0] == pytest.approx(1.0, abs=1e-12)
    assert tr[1] == pytest.approx(101.0 / 100.0, abs=1e-12)
    assert tr[2] == pytest.approx(99.0 / 100.0, abs=1e-12)


def test_dividend_outside_window_is_ignored() -> None:
    """Dividend ex_date outside [start_dt, end_dt] does not affect the TR
    inside the window.
    """
    prices = pl.DataFrame(
        {
            "dt": [date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.00, 101.00],
        }
    )
    dividends = pl.DataFrame(
        {
            "ex_date": [date(2024, 1, 2)],  # before window start
            "amount_per_share": [5.00],
        }
    )

    result = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2024, 1, 3),
        end_dt=date(2024, 1, 4),
        expense_ratio_annual=Decimal("0"),
    )

    assert result["tr"][0] == pytest.approx(1.0, abs=1e-12)
    assert result["tr"][1] == pytest.approx(101.0 / 100.0, abs=1e-12)


def test_expense_ratio_drag_applied_per_trading_day() -> None:
    """Expense-ratio drag deducted as (1 - er/252) on every multiplier
    except the reference row.
    """
    prices = pl.DataFrame(
        {
            "dt": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.00, 100.00, 100.00],  # flat price; pure drag
        }
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )

    er = Decimal("0.252")  # 25.2% annualized => exactly 0.001 per trading day for legibility
    result = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 1, 4),
        expense_ratio_annual=er,
    )

    # Day 0: reference, no drag.
    assert result["tr"][0] == pytest.approx(1.0, abs=1e-12)
    # Day 1: 1.0 * (1 - 0.001) = 0.999
    assert result["tr"][1] == pytest.approx(0.999, abs=1e-12)
    # Day 2: 0.999 * (1 - 0.001) = 0.998001
    assert result["tr"][2] == pytest.approx(0.998001, abs=1e-12)


def test_annualized_return_helper() -> None:
    """Annualized return uses the 252-trading-day convention from the
    methodology doc.
    """
    # 253 rows = 252 trading-day spans. TR doubles exactly. Annualized = 100%.
    n = 253
    start = date(2024, 1, 2)
    tr_series = pl.DataFrame(
        {
            "dt": [start + timedelta(days=i) for i in range(n)],
            "tr": [1.0 + i * (1.0 / 252.0) for i in range(n)],
            "daily_return": [0.0] * n,
        }
    )
    ann = annualized_return(tr_series)
    # TR[0]=1.0, TR[252]=2.0. (2.0/1.0)**(252/252) - 1 = 1.0
    assert ann == pytest.approx(1.0, abs=1e-10)


def test_missing_price_columns_raises() -> None:
    prices = pl.DataFrame({"date": [date(2024, 1, 2)], "px": [100.0]})
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )
    with pytest.raises(KeyError, match="prices frame missing columns"):
        reconstruct_total_return(
            prices, dividends, date(2024, 1, 2), date(2024, 1, 2), Decimal("0")
        )


def test_missing_dividend_columns_raises() -> None:
    prices = pl.DataFrame({"dt": [date(2024, 1, 2)], "close": [100.0]})
    dividends = pl.DataFrame({"date": [date(2024, 1, 2)], "amount": [1.0]})
    with pytest.raises(KeyError, match="dividends frame missing columns"):
        reconstruct_total_return(
            prices, dividends, date(2024, 1, 2), date(2024, 1, 2), Decimal("0")
        )


def test_empty_window_raises() -> None:
    prices = pl.DataFrame(
        {
            "dt": [date(2024, 1, 2), date(2024, 1, 3)],
            "close": [100.0, 101.0],
        }
    )
    dividends = pl.DataFrame(
        {"ex_date": [], "amount_per_share": []},
        schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
    )
    with pytest.raises(ValueError, match="No price rows in window"):
        reconstruct_total_return(
            prices, dividends, date(2025, 1, 2), date(2025, 1, 3), Decimal("0")
        )
