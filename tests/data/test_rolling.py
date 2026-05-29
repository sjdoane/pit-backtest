"""Tests for pit_backtest.data.rolling (M2 PR A).

Per the M2 PR A reviewer pass:
- Critical finding C2: np.convolve alignment is locked at "output[i] is
  the stat over input[i:i+window]." A lookahead-safe ADV at bar t uses
  output[t-window] on volume[:t].
- Critical finding C3: compute_rolling_daily_vol returns sqrt(var) where
  var = mean(r^2) - mean(r)^2; NOT sqrt(mean(r^2)) (which would be RMS
  biased upward by mean(r)^2).
"""

from __future__ import annotations

import numpy as np
import pytest

from pit_backtest.data.rolling import (
    DEFAULT_WINDOW,
    compute_rolling_adv,
    compute_rolling_daily_vol,
)


def test_compute_rolling_adv_hand_computed_reference() -> None:
    """np.convolve with weights=ones/w gives the rolling arithmetic mean.

    Input [1, 2, 3, 4, 5] with window=3:
      output[0] = mean(1, 2, 3) = 2.0
      output[1] = mean(2, 3, 4) = 3.0
      output[2] = mean(3, 4, 5) = 4.0
    """
    volume = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = compute_rolling_adv(volume, window=3)
    np.testing.assert_allclose(result, [2.0, 3.0, 4.0], rtol=0, atol=1e-12)


def test_compute_rolling_adv_default_window_is_20() -> None:
    """The default window matches the Almgren 2005 Section 3 convention."""
    assert DEFAULT_WINDOW == 20


def test_rolling_adv_no_lookahead() -> None:
    """Lookahead-safe ADV at bar t uses volume[:t] indexed at output[t-window].

    Reviewer Critical finding C2: if a caller mistakenly passes volume[:t+1]
    and indexes output[t-window+1], bar t's volume leaks into the window.
    Build a series where bar t's volume is 10x days [t-window, t-1] and
    confirm the safe call ignores it.
    """
    window = 20
    base_volume = 1_000_000.0
    spike_volume = 10_000_000.0
    series_len = 30
    t = 25  # the bar we are computing ADV "for"

    volume = np.full(series_len, base_volume, dtype=np.float64)
    volume[t] = spike_volume

    # Safe call: pass volume[:t] (exclusive), read output[t-window].
    safe = compute_rolling_adv(volume[:t], window=window)
    safe_adv = safe[t - window]
    assert safe_adv == pytest.approx(base_volume, abs=1e-9), (
        f"safe call should give clean ADV; got {safe_adv}"
    )

    # Demonstrate the leak the docstring warns against:
    leaked = compute_rolling_adv(volume[: t + 1], window=window)
    leaked_adv_wrong = leaked[t - window + 1]
    # The leaked window includes bar t, so the average is inflated:
    # 19 * base + 1 * spike = 19_000_000 + 10_000_000 = 29M / 20 = 1.45M.
    expected_leaked = (19 * base_volume + spike_volume) / window
    assert leaked_adv_wrong == pytest.approx(expected_leaked, abs=1e-6)
    assert leaked_adv_wrong > safe_adv * 1.4, (
        "leak should materially inflate ADV; lookahead-safe alignment "
        "is exactly what protects against this bug"
    )


def test_compute_rolling_daily_vol_returns_std_not_rms() -> None:
    """Reviewer Critical finding C3: the helper must compute sigma, not RMS.

    For a series with non-zero mean, sqrt(mean(r^2)) overstates the true
    std because sqrt(mean(r^2)) = sqrt(var + mean^2). Construct a
    constant-return series and assert the rolling vol is 0 (not the
    constant return value).
    """
    # Constant return of +0.005 daily (50 bps per day): true variance is
    # 0, true std is 0. RMS would give 0.005.
    returns = np.full(40, 0.005, dtype=np.float64)
    sigma = compute_rolling_daily_vol(returns, window=20)
    np.testing.assert_allclose(sigma, np.zeros(21), rtol=0, atol=1e-12)


def test_compute_rolling_daily_vol_known_variance() -> None:
    """Returns alternating +1% and -1% have mean=0, var=1e-4, sigma=0.01.

    Reproduces the population standard deviation formula exactly so the
    reviewer's mean-correction concern is locked in code.
    """
    returns = np.array([0.01, -0.01] * 10, dtype=np.float64)
    sigma = compute_rolling_daily_vol(returns, window=4)
    np.testing.assert_allclose(sigma, np.full(17, 0.01), rtol=0, atol=1e-12)


def test_compute_rolling_daily_vol_nonzero_mean_subtracts_correctly() -> None:
    """A series with mean=0.005 and the same dispersion around the mean
    should produce the same sigma as a zero-mean series. RMS would not.
    """
    raw = np.array([0.01, -0.01] * 10, dtype=np.float64)
    sigma_zero_mean = compute_rolling_daily_vol(raw, window=4)
    sigma_shifted = compute_rolling_daily_vol(raw + 0.005, window=4)
    np.testing.assert_allclose(sigma_zero_mean, sigma_shifted, rtol=0, atol=1e-12)


def test_compute_rolling_adv_rejects_negative_volume() -> None:
    volume = np.array([1.0, -2.0, 3.0, 4.0, 5.0])
    with pytest.raises(ValueError, match="negative values"):
        compute_rolling_adv(volume, window=3)


def test_compute_rolling_adv_rejects_nan() -> None:
    volume = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
    with pytest.raises(ValueError, match="NaN or inf"):
        compute_rolling_adv(volume, window=3)


def test_compute_rolling_daily_vol_accepts_negative_returns() -> None:
    """Returns can be negative; the helper must not reject them."""
    returns = np.array([0.01, -0.02, 0.005, -0.005, 0.001], dtype=np.float64)
    sigma = compute_rolling_daily_vol(returns, window=3)
    assert sigma.shape == (3,)
    assert np.all(sigma >= 0)


def test_compute_rolling_adv_window_too_large_raises() -> None:
    volume = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="larger than series length"):
        compute_rolling_adv(volume, window=5)


def test_compute_rolling_adv_zero_window_raises() -> None:
    volume = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="positive"):
        compute_rolling_adv(volume, window=0)


def test_compute_rolling_adv_negative_window_raises() -> None:
    volume = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="positive"):
        compute_rolling_adv(volume, window=-3)


def test_compute_rolling_adv_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_rolling_adv(np.array([]), window=5)


def test_compute_rolling_adv_accepts_int_input() -> None:
    """Sharadar volume is int64; the helper must coerce cleanly."""
    volume = np.array([1, 2, 3, 4, 5], dtype=np.int64)
    result = compute_rolling_adv(volume, window=3)
    np.testing.assert_allclose(result, [2.0, 3.0, 4.0], rtol=0, atol=1e-12)
