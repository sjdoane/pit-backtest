"""Tests for analytics.drawdown (M4 PR 2 against ADR 0014 contract).

Acceptance fixture (22-bar hand-pinnable; floor bumped from the
original 6-bar fixture per the M4 PR 2 post-impl reviewer Medium 4
that raised _CALMAR_MIN_PERIODS from 5 to 21):
  dt   = 2024-01-01..22
  nav  = [100, 105, 110, 115,                  # rising; peak at 115 at idx 3
          110, 108, 105, 100, 105, 110,        # run A: 6 underwater bars (idx 4..9)
          115, 120,                            # full recovery + new peak at 120
          115, 110, 105, 100, 95, 90, 85, 80,  # run B begins
          75, 70]                              # run B ends at last bar (censored)

  running_peak path:
    idx  0..3:  100, 105, 110, 115
    idx  4..9:  115 (unchanged through dip)
    idx 10..11: 115, 120 (recovery + new peak)
    idx 12..21: 120 (unchanged through second dip)

  underwater runs:
    Run A: idx 4..9 (6 bars), bars 5..10 by 1-indexed dt
    Run B: idx 12..21 (10 bars), bars 13..22 by 1-indexed dt

  longest = Run B (10 bars). Tie-break is moot here.
  max_drawdown magnitude = max excursion = 50/120 = 5/12 = 0.41666666...
  longest underwater run = 10 bars
  is_censored_at_end = True (Run B's last bar idx=21 is the last bar)
  peak_dt = 2024-01-12 (idx 11; nav=120, the high-water mark before Run B)
  trough_dt = 2024-01-22 (idx 21; nav=70, the within-Run-B minimum)

  CAGR with periods_per_year=252, n_periods=21:
    CAGR = (70/100) ** (252/21) - 1 = 0.7**12 - 1
         = 0.013841287201 - 1
         = -0.986158712799
  Calmar = CAGR / max_drawdown
         = -0.986158712799 / (50/120)
         = -0.986158712799 * 2.4
         = -2.366780910718
"""

from __future__ import annotations

from datetime import date

import attrs
import polars as pl
import pytest

from pit_backtest.analytics.drawdown import (
    DrawdownDurationReport,
    calmar_ratio,
    drawdown_duration_report,
    max_drawdown,
)


def _acceptance_curve() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "dt": [date(2024, 1, i + 1) for i in range(22)],
            "nav": [
                100.0, 105.0, 110.0, 115.0,
                110.0, 108.0, 105.0, 100.0, 105.0, 110.0,
                115.0, 120.0,
                115.0, 110.0, 105.0, 100.0, 95.0, 90.0, 85.0, 80.0,
                75.0, 70.0,
            ],
        }
    )


def _flat_curve(n: int = 22) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "dt": [date(2024, 1, i + 1) for i in range(n)],
            "nav": [100.0] * n,
        }
    )


# ----- max_drawdown -----


def test_max_drawdown_acceptance_fixture_pins_50_over_120() -> None:
    """22-bar fixture; max_drawdown = 50/120 = 5/12 = 0.41666... (positive magnitude)."""
    result = max_drawdown(_acceptance_curve())
    assert result == pytest.approx(50.0 / 120.0, abs=1e-12)


def test_max_drawdown_flat_curve_returns_zero() -> None:
    """A flat equity curve has no drawdown; result is exactly 0.0."""
    assert max_drawdown(_flat_curve()) == 0.0


def test_max_drawdown_raises_on_missing_columns() -> None:
    """Loud-failure discipline per ADR 0013 dec 7."""
    bad = pl.DataFrame({"dt": [date(2024, 1, 1)], "value": [100.0]})
    with pytest.raises(ValueError, match="dt"):
        max_drawdown(bad)


def test_max_drawdown_raises_on_height_below_two() -> None:
    one_row = pl.DataFrame({"dt": [date(2024, 1, 1)], "nav": [100.0]})
    with pytest.raises(ValueError, match="height >= 2"):
        max_drawdown(one_row)


def test_max_drawdown_raises_on_non_positive_starting_equity() -> None:
    bad = pl.DataFrame(
        {
            "dt": [date(2024, 1, 1), date(2024, 1, 2)],
            "nav": [0.0, 100.0],
        }
    )
    with pytest.raises(ValueError, match="positive starting equity"):
        max_drawdown(bad)


def test_max_drawdown_returns_positive_magnitude_per_ldp_convention() -> None:
    """Plan-reviewer Choice B ratification: returned value is unsigned."""
    result = max_drawdown(_acceptance_curve())
    assert result > 0.0


# ----- drawdown_duration_report -----


def test_drawdown_duration_report_acceptance_fixture_pins_all_four_fields() -> None:
    """22-bar fixture: Run B (idx 12..21) is the longest at 10 bars.
    Censored at end (idx 21 is the last bar). Peak at idx 11 (2024-01-12,
    nav=120); trough at idx 21 (2024-01-22, nav=70).
    """
    report = drawdown_duration_report(_acceptance_curve())
    assert report.days == 10
    assert report.is_censored_at_end is True
    assert report.peak_dt == date(2024, 1, 12)
    assert report.trough_dt == date(2024, 1, 22)


def test_drawdown_duration_report_flat_curve_returns_days_zero_trough_none() -> None:
    """Flat curve: no underwater run; days=0; trough_dt=None."""
    report = drawdown_duration_report(_flat_curve())
    assert report.days == 0
    assert report.is_censored_at_end is False
    assert report.peak_dt == date(2024, 1, 1)
    assert report.trough_dt is None


def test_drawdown_duration_report_curve_recovers_to_peak_then_ends_flat() -> None:
    """Curve dips to 90 then recovers to 100 then ends flat. The
    longest underwater run completes inside the window; is_censored
    is False.
    """
    curve = pl.DataFrame(
        {
            "dt": [
                date(2024, 1, 1),
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
            "nav": [100.0, 90.0, 95.0, 100.0, 100.0],
        }
    )
    report = drawdown_duration_report(curve)
    assert report.days == 2  # bars 2 (90) + 3 (95) underwater
    assert report.is_censored_at_end is False
    assert report.trough_dt == date(2024, 1, 2)


def test_drawdown_duration_report_tie_broken_by_earliest_start() -> None:
    """Two equal-length runs; the earlier one wins."""
    curve = pl.DataFrame(
        {
            "dt": [
                date(2024, 1, 1),
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
                date(2024, 1, 6),
                date(2024, 1, 7),
            ],
            "nav": [100.0, 90.0, 100.0, 100.0, 90.0, 100.0, 100.0],
        }
    )
    report = drawdown_duration_report(curve)
    assert report.days == 1
    assert report.trough_dt == date(2024, 1, 2)


def test_drawdown_duration_report_two_equal_length_runs_second_at_end_of_curve() -> None:
    """Per the M4 PR 2 post-impl reviewer Medium 3: when two equal-length
    runs exist and the SECOND one ends at the last bar, the tie-break
    rule (earliest start wins) STILL picks the first run. So
    is_censored_at_end is False even though a censored equal-length
    run exists. This pins the tie-break determinism so a future refactor
    can't quietly flip the censoring flag.
    """
    curve = pl.DataFrame(
        {
            "dt": [date(2024, 1, i + 1) for i in range(11)],
            "nav": [
                100.0, 90.0, 80.0, 70.0,  # Run A: idx 1..3 (3 underwater bars)
                100.0, 100.0, 100.0, 100.0,  # recovery to peak
                90.0, 80.0, 70.0,  # Run B: idx 8..10 (3 underwater bars, ends at last bar)
            ],
        }
    )
    report = drawdown_duration_report(curve)
    assert report.days == 3
    assert report.is_censored_at_end is False
    assert report.peak_dt == date(2024, 1, 1)
    assert report.trough_dt == date(2024, 1, 4)


def test_drawdown_duration_report_raises_on_missing_columns() -> None:
    bad = pl.DataFrame({"dt": [date(2024, 1, 1)], "value": [100.0]})
    with pytest.raises(ValueError, match="dt"):
        drawdown_duration_report(bad)


def test_drawdown_duration_report_record_is_frozen() -> None:
    """attrs.frozen immutability per the codebase record discipline."""
    report = DrawdownDurationReport(
        days=4,
        is_censored_at_end=True,
        peak_dt=date(2024, 1, 2),
        trough_dt=date(2024, 1, 5),
    )
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        report.days = 5  # type: ignore[misc]


# ----- calmar_ratio -----


def test_calmar_ratio_acceptance_fixture_pins_minus_2_367() -> None:
    """22-bar fixture: CAGR = 0.7**12 - 1 = -0.986158712799;
    max_drawdown = 50/120; Calmar = CAGR / max_drawdown = -2.36678091...
    """
    result = calmar_ratio(_acceptance_curve())
    assert result == pytest.approx(-2.36678091, abs=1e-6)


def test_calmar_ratio_positive_strategy_pins_positive_result() -> None:
    """Up-trending 22-bar synthetic curve produces a positive Calmar.
    nav rises linearly 100..121 with one 0.5 dip at idx 10
    (109 -> 108.5) so max_drawdown > 0 (the denominator guard fires
    only on flat or always-rising curves).
    """
    nav = [float(100 + i) for i in range(22)]
    nav[10] = 108.5
    curve = pl.DataFrame(
        {
            "dt": [date(2024, 1, i + 1) for i in range(22)],
            "nav": nav,
        }
    )
    result = calmar_ratio(curve)
    assert result > 0


def test_calmar_ratio_raises_on_flat_curve() -> None:
    """Flat curve: max_dd == 0; raises ValueError per Choice C floor +
    the no-denominator guard.
    """
    with pytest.raises(ValueError, match="max_drawdown == 0"):
        calmar_ratio(_flat_curve())


def test_calmar_ratio_raises_below_minimum_periods_floor() -> None:
    """Per the M4 PR 2 post-impl reviewer Medium 4 the floor is 21 bars
    (one trading month per LdP 2018 ch 14). Anything < 21 bars raises.
    """
    tiny = pl.DataFrame(
        {
            "dt": [date(2024, 1, i + 1) for i in range(20)],
            "nav": [100.0 + i for i in range(20)],
        }
    )
    with pytest.raises(ValueError, match="height >="):
        calmar_ratio(tiny)


def test_calmar_ratio_raises_on_non_positive_periods_per_year() -> None:
    with pytest.raises(ValueError, match="periods_per_year"):
        calmar_ratio(_acceptance_curve(), periods_per_year=0)


def test_calmar_ratio_periods_per_year_custom_value_reads_through() -> None:
    """Different periods_per_year values must produce different Calmar
    results on the same curve; pins that the parameter is consumed.
    Annual (252) vs quarterly (4) annualization explodes vs collapses
    the CAGR exponent.
    """
    annual = calmar_ratio(_acceptance_curve(), periods_per_year=252)
    quarterly = calmar_ratio(_acceptance_curve(), periods_per_year=4)
    assert annual != quarterly
