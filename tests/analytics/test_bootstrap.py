"""Tests for analytics.bootstrap (ADR 0016 dec 5): the stationary block
bootstrap and the Politis-White automatic block-length selector."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from pit_backtest.analytics.bootstrap import (
    politis_white_block_length,
    stationary_block_bootstrap,
)


def _arch_reference_block_length(x: list[float]) -> float:
    """numpy transcription of the canonical arch `_single_optimal_block`
    (stationary branch), used ONLY as a port-fidelity oracle for the pure-Python
    `politis_white_block_length`. This transcribes the same source, so it checks
    the pure-Python translation (indexing, loop bounds), NOT the algorithm's
    independent correctness (the limiting-behavior tests below do that)."""
    arr = np.asarray(x, dtype=float)
    nobs = arr.shape[0]
    if nobs < 11:
        return 1.0
    eps = arr - arr.mean()
    b_max = math.ceil(min(3 * math.sqrt(nobs), nobs / 3))
    kn = max(5, int(math.log10(nobs)))
    m_max = int(math.ceil(math.sqrt(nobs))) + kn
    cv = 2 * math.sqrt(math.log10(nobs) / nobs)
    acv = np.zeros(m_max + 1)
    abs_acorr = np.zeros(m_max + 1)
    opt_m: int | None = None
    for i in range(m_max + 1):
        v1 = eps[i + 1 :] @ eps[i + 1 :]
        v2 = eps[: -(i + 1)] @ eps[: -(i + 1)]
        cross = eps[i:] @ eps[: nobs - i]
        acv[i] = cross / nobs
        abs_acorr[i] = abs(cross) / math.sqrt(v1 * v2) if v1 * v2 > 0 else math.inf
        if i >= kn and opt_m is None and np.all(abs_acorr[i - kn : i] < cv):
            opt_m = i - kn
    m = min(2 * max(opt_m, 1) if opt_m is not None else m_max, m_max)
    g = 0.0
    lr_acv = acv[0]
    for k in range(1, m + 1):
        lam = 1.0 if k / m <= 0.5 else 2 * (1 - k / m)
        g += 2 * lam * k * acv[k]
        lr_acv += 2 * lam * acv[k]
    b_sb = ((2 * g**2) / (2 * lr_acv**2)) ** (1 / 3) * nobs ** (1 / 3)
    return float(min(b_sb, b_max))


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


# ----- Politis-White automatic block-length selector -----


def _wn(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 1.0) for _ in range(n)]


def test_politis_white_matches_arch_reference_port_fidelity() -> None:
    """The pure-Python port equals the numpy transcription of arch's canonical
    `_single_optimal_block` to floating-point precision across diverse series.
    This is the port-fidelity oracle (it catches indexing / loop-bound / constant
    translation errors), not an independent-correctness check."""
    cases = [
        _wn(240, 1),
        _wn(50, 7),
        _ar1_series(300, 0.9, 3),
        _ar1_series(200, 0.5, 11),
        _ar1_series(500, 0.99, 13),
        [0.1 * math.sin(t) for t in range(120)],
    ]
    for series in cases:
        assert politis_white_block_length(series) == pytest.approx(
            _arch_reference_block_length(series), abs=1e-9
        )


def test_politis_white_monotone_in_dependence() -> None:
    """The selected block length increases with serial dependence: white noise <
    AR(0.5) < AR(0.9) < near-unit-root. An independent qualitative check of the
    algorithm (robust to the exact transcription)."""
    wn = politis_white_block_length(_wn(400, 5))
    ar5 = politis_white_block_length(_ar1_series(400, 0.5, 5))
    ar9 = politis_white_block_length(_ar1_series(400, 0.9, 5))
    ar99 = politis_white_block_length(_ar1_series(400, 0.99, 5))
    assert wn < ar5 < ar9 < ar99


def test_politis_white_strong_ar_is_large() -> None:
    """A strongly autocorrelated AR(0.9) series yields a large block length
    (tens of periods), the textbook Politis-White behavior."""
    assert politis_white_block_length(_ar1_series(300, 0.9, 3)) > 12.0


def test_politis_white_caps_at_b_max() -> None:
    """A near-unit-root series would push b past the b_max = ceil(min(3*sqrt(n),
    n/3)) cap; the result is clamped to it."""
    n = 500
    b_max = math.ceil(min(3 * math.sqrt(n), n / 3))
    b = politis_white_block_length(_ar1_series(n, 0.995, 21))
    assert b <= b_max
    assert b > 20.0  # still large (the dependence is strong)


def test_politis_white_near_iid_below_one() -> None:
    """White noise is judged near-iid: the raw optimum falls below the minimal
    serial-dependence block of 2.0 (often below 1.0), which is why the study
    floors it. This is the honest behavior on the real momentum series."""
    assert politis_white_block_length(_wn(500, 17)) < 2.0


def test_politis_white_short_series_returns_one() -> None:
    """A series too short to estimate the spectrum (n < 11, where m_max would
    reach n-1 and the lag-window partial sums go empty) returns 1.0."""
    assert politis_white_block_length([0.1, -0.2, 0.05, 0.0, -0.1]) == 1.0
    assert politis_white_block_length(_RETURNS) == 1.0  # the length-10 fixture
    assert politis_white_block_length(_wn(11, 0)) != 1.0  # 11 is computable


def test_politis_white_pinned_value() -> None:
    """A fixed deterministic AR(0.6) series of length 100 (well below its b_max
    of 30, so the full flat-top arithmetic is exercised, not the cap) pins the
    exact result as a regression guard against silent drift."""
    series = _ar1_series(100, 0.6, 0)
    assert politis_white_block_length(series) == pytest.approx(3.8484, abs=1e-3)


def test_politis_white_deterministic() -> None:
    """Pure arithmetic, no RNG: identical input gives identical output."""
    series = _ar1_series(120, 0.7, 9)
    assert politis_white_block_length(series) == politis_white_block_length(series)
