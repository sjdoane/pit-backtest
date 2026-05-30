"""Tests for analytics.concentration (M4 PR 2).

Per Plan-reviewer Medium 4 the all-zero PnL case raises (consistent
with the loud-failure discipline locked in ADR 0013 decision 7) rather
than silently returns 0.0. Per Plan-reviewer Low 8 the test set is
expanded beyond the original plan's 5-test minimum to cover the
asymptotic limit, the rescale invariance, and the 2-bar 50/50 boundary.
"""

from __future__ import annotations

import polars as pl
import pytest

from pit_backtest.analytics.concentration import hhi


def test_hhi_uniform_n_bar_series_pins_one_over_n() -> None:
    """Uniform 10-bar series: every weight is 1/10; HHI = 10 * (1/10)^2 = 0.1."""
    series = pl.Series("pnl", [1.0] * 10)
    result = hhi(series)
    assert result == pytest.approx(0.1, abs=1e-12)


def test_hhi_single_non_zero_bar_pins_one() -> None:
    """One bar carries all the PnL; weight is 1.0; HHI = 1.0 (maximum)."""
    series = pl.Series("pnl", [0.0, 0.0, 5.0, 0.0, 0.0])
    assert hhi(series) == pytest.approx(1.0, abs=1e-12)


def test_hhi_raises_on_all_zero_series_per_loud_failure_discipline() -> None:
    """Plan-reviewer Medium 4: HHI is mathematically undefined when
    sum(|pnl|) == 0. Per ADR 0013 dec 7 the codebase raises rather than
    silently returning a degenerate value.
    """
    series = pl.Series("pnl", [0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="sum"):
        hhi(series)


def test_hhi_raises_on_empty_series() -> None:
    series = pl.Series("pnl", [], dtype=pl.Float64)
    with pytest.raises(ValueError, match="non-empty"):
        hhi(series)


def test_hhi_mixed_sign_series_uses_absolute_value_contributions() -> None:
    """A series of [+10, -10, +5, -5] has concentration weights based
    on absolute values: |pnl| = [10, 10, 5, 5], sum=30, weights =
    [1/3, 1/3, 1/6, 1/6], HHI = 2*(1/9) + 2*(1/36) = 2/9 + 1/18 =
    4/18 + 1/18 = 5/18 = 0.27777...
    """
    series = pl.Series("pnl", [10.0, -10.0, 5.0, -5.0])
    result = hhi(series)
    assert result == pytest.approx(5.0 / 18.0, abs=1e-12)


def test_hhi_two_bar_equal_split_pins_one_half() -> None:
    """Plan-reviewer Low 8 add-test: 2-bar 50/50 weights both 0.5;
    HHI = 2 * 0.25 = 0.5 exactly.
    """
    series = pl.Series("pnl", [3.0, 3.0])
    assert hhi(series) == pytest.approx(0.5, abs=1e-12)


def test_hhi_one_bar_dominates_approaches_unity() -> None:
    """Plan-reviewer Low 8 add-test: with one bar carrying 99% of the
    PnL and 99 other bars carrying ~0.0001 each, HHI should be near 1.0.
    """
    series = pl.Series("pnl", [99.0] + [0.0001] * 99)
    result = hhi(series)
    assert result > 0.999


def test_hhi_invariant_under_positive_rescaling() -> None:
    """Plan-reviewer Low 8 add-test: scaling every contribution by the
    same positive constant does not change the weights, so HHI is
    invariant. This pins the formula's scale-free property.
    """
    series_small = pl.Series("pnl", [1.0, 2.0, 3.0, 4.0])
    series_large = pl.Series("pnl", [100.0, 200.0, 300.0, 400.0])
    assert hhi(series_small) == pytest.approx(hhi(series_large), abs=1e-12)
