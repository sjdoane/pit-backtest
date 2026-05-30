"""Tests for analytics.distribution (M4 PR 2).

Per Plan-reviewer Medium 5 + Medium 6 the TypeVar is unbounded
(deferred to M4 PR 3's CPCV emission shape) and `__init__` raises on
empty paths. Per High 2 the percentile algorithm is nearest-rank
defended on first principles, not on the original plan's fabricated
LdP citation.

Acceptance fixture (10-path numeric distribution):
  paths = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
  Nearest-rank: p10 -> rank=1, value=0.1; p50 -> rank=5, value=0.5;
                p90 -> rank=9, value=0.9.
"""

from __future__ import annotations

import warnings

import pytest

from pit_backtest.analytics.distribution import BacktestPathDistribution


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
