"""Tests for analytics.bootstrap.stationary_block_bootstrap (ADR 0016 dec 5)."""

from __future__ import annotations

import random

import pytest

from pit_backtest.analytics.bootstrap import stationary_block_bootstrap


def _lag1_autocorr(series: list[float]) -> float:
    """Stdlib lag-1 autocorrelation (no numpy; analytics is stdlib-only)."""
    n = len(series)
    mean = sum(series) / n
    num = sum((series[i] - mean) * (series[i - 1] - mean) for i in range(1, n))
    den = sum((x - mean) ** 2 for x in series)
    return num / den if den != 0.0 else 0.0


def _ar1_series(n: int, phi: float, seed: int) -> list[float]:
    """A deterministic AR(1) series with strong positive autocorrelation."""
    rng = random.Random(seed)
    out = [0.0]
    for _ in range(n - 1):
        out.append(phi * out[-1] + rng.gauss(0.0, 1.0))
    return out


_RETURNS = [0.01, -0.02, 0.03, -0.01, 0.005, 0.02, -0.015, 0.0, 0.012, -0.008]


def test_reproducible_same_seed_same_paths() -> None:
    a = stationary_block_bootstrap(_RETURNS, 5, expected_block_length=3.0, seed=42)
    b = stationary_block_bootstrap(_RETURNS, 5, expected_block_length=3.0, seed=42)
    assert a == b


def test_different_seed_differs() -> None:
    a = stationary_block_bootstrap(_RETURNS, 5, expected_block_length=3.0, seed=1)
    b = stationary_block_bootstrap(_RETURNS, 5, expected_block_length=3.0, seed=2)
    assert a != b


def test_output_shape_is_n_paths_by_len_returns() -> None:
    paths = stationary_block_bootstrap(_RETURNS, 7, expected_block_length=3.0, seed=0)
    assert len(paths) == 7
    assert all(len(p) == len(_RETURNS) for p in paths)


def test_resampled_values_are_drawn_from_input() -> None:
    source = {1.0, 2.0, 3.0, 4.0, 5.0}
    paths = stationary_block_bootstrap(
        sorted(source), 10, expected_block_length=2.5, seed=99
    )
    for path in paths:
        assert all(value in source for value in path)


def test_values_are_floats_even_from_int_input() -> None:
    paths = stationary_block_bootstrap([1, 2, 3], 3, expected_block_length=2.0, seed=5)
    assert all(isinstance(v, float) for p in paths for v in p)


def test_single_element_returns_yields_constant_paths() -> None:
    paths = stationary_block_bootstrap([0.07], 4, expected_block_length=2.0, seed=3)
    assert paths == [[0.07], [0.07], [0.07], [0.07]]


def test_large_block_length_preserves_more_serial_correlation() -> None:
    """The property that justifies the stationary bootstrap over iid: longer
    expected blocks preserve more of the input's lag-1 autocorrelation."""
    ar1 = _ar1_series(200, phi=0.9, seed=2024)

    def mean_acf(expected_block_length: float) -> float:
        paths = stationary_block_bootstrap(
            ar1, 50, expected_block_length=expected_block_length, seed=7
        )
        return sum(_lag1_autocorr(p) for p in paths) / len(paths)

    acf_short = mean_acf(2.0)
    acf_long = mean_acf(20.0)
    # Directional inequality with margin (not a brittle point pin).
    assert acf_long > acf_short + 0.15


# ----- loud-fail matrix (ADR 0013 dec 7) -----


def test_empty_returns_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        stationary_block_bootstrap([], 3, expected_block_length=2.0, seed=0)


def test_n_paths_below_one_raises() -> None:
    with pytest.raises(ValueError, match="n_paths"):
        stationary_block_bootstrap(_RETURNS, 0, expected_block_length=2.0, seed=0)


def test_block_length_exactly_one_raises() -> None:
    with pytest.raises(ValueError, match="expected_block_length"):
        stationary_block_bootstrap(_RETURNS, 3, expected_block_length=1.0, seed=0)


def test_block_length_below_one_raises() -> None:
    with pytest.raises(ValueError, match="expected_block_length"):
        stationary_block_bootstrap(_RETURNS, 3, expected_block_length=0.5, seed=0)


def test_block_length_nan_raises() -> None:
    with pytest.raises(ValueError, match="expected_block_length"):
        stationary_block_bootstrap(
            _RETURNS, 3, expected_block_length=float("nan"), seed=0
        )


def test_non_int_seed_raises() -> None:
    with pytest.raises(ValueError, match="seed"):
        stationary_block_bootstrap(
            _RETURNS, 3, expected_block_length=2.0, seed=1.5  # type: ignore[arg-type]
        )


def test_bool_seed_raises() -> None:
    with pytest.raises(ValueError, match="seed"):
        stationary_block_bootstrap(
            _RETURNS, 3, expected_block_length=2.0, seed=True  # type: ignore[arg-type]
        )
