"""Rolling-window helpers for cost-model market-state pre-computation.

Per ADR 0005 step 8: cost-model arithmetic uses pre-computed Polars frames
for sigma_D / V_D / Theta computed at Backtest.__init__. The rolling-window
computations use pure NumPy (np.convolve + variance via second-moment minus
first-moment-squared) to insulate the cost model's numeric outputs from
Polars version bumps; np.convolve with mode="valid" is byte-deterministic
under the pinned NumPy 1.26.4.

Alignment contract (locked per M2 PR A reviewer pass):
- Input series indexed by bar position 0..T-1 (whatever the caller's
  date<->index mapping is).
- Output index `i` of `compute_rolling_*(series, window=w)` corresponds to
  the statistic over input bars `[i, i+w-1]` inclusive (the standard
  np.convolve "valid" semantics).
- Callers that want a LOOKAHEAD-SAFE rolling stat at bar `t` (using only
  bars strictly before `t`) MUST pass `series[:t]` and read `output[t-w]`.
  Passing `series[:t+1]` and reading `output[t-w+1]` leaks bar `t` into
  the window.

The lookahead-safe alignment is exercised in
`tests/data/test_rolling.py::test_rolling_adv_no_lookahead`.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


DEFAULT_WINDOW: int = 20  # one trading month per Almgren 2005 Section 3.


def _validate(
    series: NDArray[np.float64], window: int, *, allow_negative: bool
) -> NDArray[np.float64]:
    """Common validation for the rolling helpers.

    - Coerces input to np.float64.
    - Raises on window <= 0, window > len(series), NaN, inf, or (for ADV)
      negative entries.
    Returns the coerced array.
    """
    if window <= 0:
        raise ValueError(f"window must be positive; got {window}")
    if series.size == 0:
        raise ValueError("series is empty; cannot compute rolling stat")
    if window > series.size:
        raise ValueError(
            f"window={window} is larger than series length={series.size}"
        )
    coerced = np.asarray(series, dtype=np.float64)
    if not np.isfinite(coerced).all():
        raise ValueError(
            "series contains NaN or inf; cost-model market state requires "
            "finite inputs (clean upstream before computing rolling stats)"
        )
    if not allow_negative and np.any(coerced < 0.0):
        raise ValueError(
            "series contains negative values; volume cannot be negative"
        )
    return coerced


def compute_rolling_adv(
    volume: NDArray[np.float64], window: int = DEFAULT_WINDOW
) -> NDArray[np.float64]:
    """Rolling average daily volume via np.convolve.

    `output[i]` is the mean of `volume[i:i+window]` (inclusive of both
    endpoints in terms of indices). For lookahead-safe ADV at bar `t`,
    callers must pass `volume[:t]` and read `output[t-window]`.

    Raises ValueError on window <= 0, window > len(volume), NaN/inf, or
    negative volume entries.
    """
    coerced = _validate(volume, window, allow_negative=False)
    weights = np.ones(window, dtype=np.float64) / float(window)
    return np.convolve(coerced, weights, mode="valid")


def compute_rolling_daily_vol(
    returns: NDArray[np.float64], window: int = DEFAULT_WINDOW
) -> NDArray[np.float64]:
    """Rolling daily standard deviation of returns via convolution.

    Uses the second-moment-minus-first-moment-squared identity:
        var(window) = mean(r^2) - mean(r)^2
        sigma_D    = sqrt(var)

    This is the true standard deviation (corrected for the rolling mean),
    not the RMS. For daily equity returns with mean ~5 bps/day, the RMS
    would overstate sigma by approximately that amount; the bias compounds
    in the Almgren cost-model output. The plan's earlier proposal of
    `sqrt(mean(r^2))` was rejected in the M2 PR A reviewer pass per
    Critical finding C3.

    `output[i]` is the standard deviation of `returns[i:i+window]`. Same
    lookahead-safe indexing convention as `compute_rolling_adv`.

    Raises ValueError on window <= 0, window > len(returns), or NaN/inf.
    Negative returns are allowed (they are common for any asset).
    """
    coerced = _validate(returns, window, allow_negative=True)
    weights = np.ones(window, dtype=np.float64) / float(window)
    mean_r = np.convolve(coerced, weights, mode="valid")
    mean_r_squared = np.convolve(coerced * coerced, weights, mode="valid")
    var: NDArray[np.float64] = mean_r_squared - mean_r * mean_r
    # Floating-point arithmetic can produce tiny negative variances on
    # constant-return windows; clamp at zero before sqrt.
    var = np.maximum(var, 0.0)
    sigma: NDArray[np.float64] = np.sqrt(var)
    return sigma
