"""Tests for analytics.distribution (M4 PR 2 + ADR 0015 prep PR 3a).

Per Plan-reviewer Medium 6 `__init__` raises on empty paths. Per High 2
the percentile algorithm is nearest-rank defended on first principles,
not on the original plan's fabricated LdP citation.

Per ADR 0015 the TypeVar is bounded by `SupportsRichComparison`;
`BacktestResult.__lt__` keyed on `sr_hat` lets the engine surface
`BacktestPathDistribution[BacktestResult]` sort cleanly. Two new test
groups exercise (a) `BacktestResult` ordering on `sr_hat` and (b)
nearest-rank percentile lookup over a `BacktestPathDistribution[BacktestResult]`.

Acceptance fixture (10-path numeric distribution):
  paths = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
  Nearest-rank: p10 -> rank=1, value=0.1; p50 -> rank=5, value=0.5;
                p90 -> rank=9, value=0.9.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal

import pytest

from pit_backtest.analytics.distribution import BacktestPathDistribution
from pit_backtest.analytics.drawdown import DrawdownDurationReport
from pit_backtest.analytics.scorecard import (
    Attribution,
    BacktestResult,
    GeneralCharacteristics,
    ImplementationShortfall,
    Performance,
    RiskAdjusted,
    RunsAndDrawdowns,
    Scorecard,
)
from pit_backtest.validation.confidence_tier import ConfidenceTier


def _acceptance_paths() -> list[float]:
    return [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def _suppress_low_path_warning() -> warnings.catch_warnings:
    """Context manager to suppress the path_count < 30 stability warning.

    pyproject.toml filterwarnings = ["error"] makes warnings errors;
    when the test constructs a distribution with path_count < 30 (as
    most of these tests do for hand-pinnability) we ignore the warning.
    """
    return warnings.catch_warnings()


# ----- Acceptance pin -----


def test_percentiles_acceptance_fixture_pins_p10_p50_p90() -> None:
    """10-path distribution; nearest-rank yields rank=1, 5, 9."""
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=_acceptance_paths(), path_count=10
        )
    result = dist.percentiles([10.0, 50.0, 90.0])
    assert result[10.0] == pytest.approx(0.1, abs=1e-12)
    assert result[50.0] == pytest.approx(0.5, abs=1e-12)
    assert result[90.0] == pytest.approx(0.9, abs=1e-12)


def test_percentiles_shuffled_input_returns_same_result() -> None:
    """Internal sort handles unsorted input; the same percentiles fire
    regardless of construction order.
    """
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=[0.5, 0.1, 0.9, 0.3, 0.7, 0.2, 0.6, 0.4, 0.8, 1.0],
            path_count=10,
        )
    result = dist.percentiles([10.0, 50.0, 90.0])
    assert result[10.0] == pytest.approx(0.1, abs=1e-12)
    assert result[50.0] == pytest.approx(0.5, abs=1e-12)
    assert result[90.0] == pytest.approx(0.9, abs=1e-12)


# ----- Convenience method consistency -----


def test_median_p10_p90_match_corresponding_percentile_call() -> None:
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=_acceptance_paths(), path_count=10
        )
    explicit = dist.percentiles([10.0, 50.0, 90.0])
    assert dist.median() == explicit[50.0]
    assert dist.p10() == explicit[10.0]
    assert dist.p90() == explicit[90.0]


# ----- Single path -----


def test_single_path_distribution_returns_that_path_for_every_percentile() -> None:
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=[0.42], path_count=1
        )
    assert dist.median() == 0.42
    assert dist.p10() == 0.42
    assert dist.p90() == 0.42


# ----- __init__ guards -----


def test_init_raises_on_empty_paths() -> None:
    """Per Plan-reviewer Medium 6: empty distribution is mathematically
    incoherent at construction time; raise loudly rather than warn-only.
    """
    with pytest.raises(ValueError, match="at least one path"):
        BacktestPathDistribution(paths=[], path_count=10)


def test_init_raises_on_nan_paths() -> None:
    """IEEE-754 sort-order trap; nan paths break the nearest-rank algorithm."""
    with pytest.raises(ValueError, match="NaN"):
        BacktestPathDistribution(
            paths=[0.1, float("nan"), 0.3], path_count=3
        )


def test_init_warns_when_path_count_below_stability_threshold() -> None:
    """Pre-PR-2 behavior preserved: path_count < 30 warns (does not raise)."""
    with pytest.warns(UserWarning, match="stability threshold"):
        BacktestPathDistribution(paths=[0.1, 0.2], path_count=2)


def test_init_does_not_warn_when_path_count_at_threshold() -> None:
    """30 paths is the boundary; no warning fires."""
    paths = [float(i) for i in range(30)]
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        BacktestPathDistribution(paths=paths, path_count=30)


# ----- percentiles input validation -----


def test_percentiles_raises_on_values_outside_zero_one_hundred() -> None:
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=_acceptance_paths(), path_count=10
        )
    with pytest.raises(ValueError, match="out-of-range") as exc_info:
        dist.percentiles([10.0, 200.0, 50.0, -5.0])
    msg = str(exc_info.value)
    assert "200.0" in msg
    assert "-5.0" in msg


def test_percentiles_zero_returns_smallest_path() -> None:
    """rank = max(1, ceil(0)) = max(1, 0) = 1; returns smallest."""
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=_acceptance_paths(), path_count=10
        )
    assert dist.percentiles([0.0])[0.0] == 0.1


def test_percentiles_one_hundred_returns_largest_path() -> None:
    """rank = ceil(100/100 * 10) = 10; returns largest."""
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=_acceptance_paths(), path_count=10
        )
    assert dist.percentiles([100.0])[100.0] == 1.0


def test_percentiles_does_not_mutate_internal_paths_list() -> None:
    """Sort returns a new list; self._paths is not mutated."""
    paths = [0.5, 0.1, 0.9, 0.3, 0.7]
    with _suppress_low_path_warning():
        warnings.simplefilter("ignore", UserWarning)
        dist: BacktestPathDistribution[float] = BacktestPathDistribution(
            paths=paths, path_count=5
        )
    dist.percentiles([50.0])
    assert paths == [0.5, 0.1, 0.9, 0.3, 0.7]


# ----- ADR 0015: SupportsRichComparison bound + BacktestResult ordering -----


def _make_backtest_result(sr_hat: float) -> BacktestResult:
    """Build a synthetic BacktestResult; sr_hat is the ordering key,
    everything else is fixed-shape filler. The PSR/DSR fields are set
    to a non-None value so the render-path validator does not fire
    on the CPCV tier.
    """
    scorecard = Scorecard(
        general=GeneralCharacteristics(
            n_trading_days=252,
            n_assets=1,
            universe_id="SPY",
            start_dt="2024-01-01",
            end_dt="2024-12-31",
        ),
        performance=Performance(
            total_return=0.10,
            annualized_return=0.10,
            annualized_volatility=0.15,
        ),
        runs_and_drawdowns=RunsAndDrawdowns(
            max_drawdown=0.05,
            drawdown_duration=DrawdownDurationReport(
                days=10,
                is_censored_at_end=False,
                peak_dt=date(2024, 3, 1),
                trough_dt=date(2024, 3, 11),
            ),
            longest_winning_run=5,
            longest_losing_run=3,
        ),
        implementation_shortfall=ImplementationShortfall(
            total_commission=Decimal("0"),
            total_slippage_bps=Decimal("0"),
            total_temporary_impact_bps=Decimal("0"),
            total_permanent_impact_bps=Decimal("0"),
        ),
        risk_adjusted=RiskAdjusted(
            sr_hat=sr_hat, psr=0.75, dsr=0.70, min_trl=None
        ),
        attribution=Attribution(by_year={2024: 0.10}),
    )
    return BacktestResult(
        sr_hat=sr_hat,
        psr=0.75,
        dsr=0.70,
        min_trl=None,
        confidence_tier=ConfidenceTier.CPCV_WITH_DSR_CORRECTION,
        scorecard=scorecard,
    )


def test_backtest_result_lt_orders_by_sr_hat() -> None:
    """Per ADR 0015 the BacktestResult __lt__ method orders by sr_hat.
    A lower sr_hat is "less than" a higher one.
    """
    low = _make_backtest_result(sr_hat=0.5)
    high = _make_backtest_result(sr_hat=1.5)
    assert low < high
    assert not (high < low)
    assert sorted([high, low])[0] is low


def test_backtest_result_lt_returns_notimplemented_for_other_types() -> None:
    """Per ADR 0015 the __lt__ returns NotImplemented for non-BacktestResult
    comparands; Python's comparison machinery then raises TypeError cleanly.
    """
    result = _make_backtest_result(sr_hat=1.0)
    with pytest.raises(TypeError):
        _ = result < 0.5  # type: ignore[operator]


def test_backtest_path_distribution_accepts_backtest_result_t() -> None:
    """Per ADR 0015 BacktestPathDistribution[BacktestResult] satisfies
    the SupportsRichComparison TypeVar bound; the existing
    engine/runner.py:92 surface stays compatible.
    """
    paths = [_make_backtest_result(sr_hat=float(i)) for i in range(30)]
    dist: BacktestPathDistribution[BacktestResult] = BacktestPathDistribution(
        paths=paths, path_count=30
    )
    # p10 -> rank = ceil(10/100 * 30) = 3 -> sorted index 2 -> sr_hat=2.0
    assert dist.p10().sr_hat == pytest.approx(2.0, abs=1e-12)
    # p50 -> rank = ceil(50/100 * 30) = 15 -> sorted index 14 -> sr_hat=14.0
    assert dist.median().sr_hat == pytest.approx(14.0, abs=1e-12)
    # p90 -> rank = ceil(90/100 * 30) = 27 -> sorted index 26 -> sr_hat=26.0
    assert dist.p90().sr_hat == pytest.approx(26.0, abs=1e-12)
