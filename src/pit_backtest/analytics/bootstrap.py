"""Stationary block bootstrap for path uncertainty (ADR 0016 decision 5).

The M5 study's genuine path-uncertainty tool. CPCV's reconstructed paths
coincide for a deterministic factor (ADR 0016 decision 4), so the honest
path-uncertainty surface is a resampling bootstrap of the realized return
series. The STATIONARY block bootstrap (Politis and Romano 1994) resamples
the series in geometrically-distributed-length blocks, which preserves the
short-range serial dependence momentum returns carry; an iid bootstrap
would understate path variance by destroying that autocorrelation.

The stationary variant is chosen over the moving-block (Kunsch 1989) and
circular-block bootstraps because its random geometric block lengths make
the resampled series strictly stationary, avoiding the fixed-block-length
artifacts of the former and the period-wrap distortion of the latter.

Determinism and dependencies: this module uses the Python standard library
`random.Random(seed)` only. The analytics layer is deliberately stdlib-only
(ADR 0013 decision 11), and ADR 0016 decision 5 specifies `random.Random`
with an explicit seed for the bootstrap. `docs/methodology/determinism.md`
Requirement 2 bans module-level RNG in the `signal` and `policy` layers (the
engine plumbs a seeded generator); the analytics layer is the explicit
carve-out, so an explicitly-seeded `random.Random` here is consistent, not a
violation. The seed and the two per-step draws (a continuation test then a
restart draw) are the only randomness, and CPython's `random.Random` is
reproducible across runs for a fixed seed and call sequence.

Block-length selection: `politis_white_block_length` implements the Politis
and White (2004) / Patton, Politis and White (2009) automatic data-driven
choice of `expected_block_length` from a flat-top lag-window spectral estimate
(stdlib-only, a faithful transcription of the canonical `arch` implementation).
The momentum study reports its raw value and applies a conservative minimal
floor; on the real monthly series the selector judges the returns near-iid (it
overrides the isolated boundary-crossing lag the +/- 2/sqrt(n) ACF rule would
treat as significant), a stronger and more honest result than a hand-chosen
block length.

References:
  - Politis, D. N. and Romano, J. P. (1994), "The Stationary Bootstrap", JASA
    89(428), 1303-1313 (the resampling method).
  - Politis, D. N. and White, H. (2004), "Automatic Block-Length Selection for
    the Dependent Bootstrap", Econometric Reviews 23(1), 53-70; Patton,
    Politis and White (2009) correction, Econometric Reviews 28(4), 372-375
    (the automatic block length).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def stationary_block_bootstrap(
    returns: Sequence[float],
    n_paths: int,
    *,
    expected_block_length: float,
    seed: int,
) -> list[list[float]]:
    """Resample `returns` into `n_paths` synthetic series of the same length.

    Each synthetic series is built by concatenating geometric-length blocks
    drawn (with wrap-around) from `returns`, with mean block length
    `expected_block_length`. The block-continuation probability is
    `p = 1 / expected_block_length`: at each position the next value either
    continues the current block (probability `1 - p`, advance the index with
    wrap-around) or starts a new block at a uniformly-random index
    (probability `p`).

    Args:
      returns: the realized per-period return series to resample. Must be
        non-empty.
      n_paths: how many synthetic series to generate. Must be >= 1.
      expected_block_length: the mean geometric block length. Must be > 1.0;
        `expected_block_length == 1.0` degenerates to the iid bootstrap (and
        `p = 1` would restart every step), which the stationary bootstrap
        exists to avoid, so it is rejected.
      seed: the `random.Random` seed for reproducibility. Must be an `int`
        (a `bool` is rejected explicitly; `True`/`False` as a seed is almost
        certainly a caller bug).

    Returns:
      A list of `n_paths` synthetic return series, each of length
      `len(returns)`, with `float` values drawn from `returns`.

    Raises:
      ValueError: per the loud-failure discipline (ADR 0013 decision 7) on a
        non-int (or bool) seed, empty `returns`, `n_paths < 1`, or
        `expected_block_length` not strictly greater than 1.0 (this also
        rejects NaN).
    """
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(
            f"seed must be an int (got {type(seed).__name__} {seed!r}); a bool "
            f"seed is rejected as a likely caller bug"
        )
    if len(returns) == 0:
        raise ValueError("returns is empty; nothing to resample")
    if n_paths < 1:
        raise ValueError(f"n_paths must be >= 1; got {n_paths}")
    if not (expected_block_length > 1.0):
        raise ValueError(
            f"expected_block_length must be > 1.0 (got {expected_block_length!r}); "
            f"1.0 degenerates to the iid bootstrap and NaN is rejected"
        )

    n = len(returns)
    p = 1.0 / expected_block_length
    rng = random.Random(seed)

    paths: list[list[float]] = []
    for _ in range(n_paths):
        idx = rng.randrange(n)
        series: list[float] = []
        while len(series) < n:
            series.append(float(returns[idx]))
            # Two draws per step, in this fixed order (reproducibility hinges
            # on the order): the continuation test, then the restart draw.
            if rng.random() < p:
                idx = rng.randrange(n)  # start a new block
            else:
                idx = (idx + 1) % n  # continue the block (wrap around)
        paths.append(series)
    return paths


def politis_white_block_length(returns: Sequence[float]) -> float:
    """Politis-White automatic optimal mean block length for the STATIONARY
    block bootstrap (the data-driven choice of `expected_block_length`).

    Politis and White (2004), corrected in Patton, Politis and White (2009),
    select the block length from a flat-top lag-window (Politis and Romano 1995)
    estimate of the spectral density at frequency zero. This is a faithful
    pure-Python transcription of the canonical `arch` implementation
    (`arch.bootstrap.optimal_block_length`, the `_single_optimal_block`
    stationary branch), kept stdlib-only (`math` + builtins) per the analytics
    layer's no-numpy/scipy discipline (ADR 0013 decision 11, ADR 0016 dec 5).

    Algorithm (for a centered series eps of length n):
      - `b_max = ceil(min(3*sqrt(n), n/3))` caps the result.
      - `kn = max(5, int(log10(n)))` is the run length of consecutive lags that
        must be jointly insignificant. (arch uses `int()` here, a floor; its own
        docstring notes a known floor-vs-ceil ambiguity, so this matches the
        reference and is NOT a bug to "fix".)
      - `m_max = ceil(sqrt(n)) + kn` is the largest lag examined.
      - A lag's normalized autocorrelation is `|sum eps_t eps_{t+i}| /
        sqrt(SS_lead * SS_lag)`; the flat-top cutoff is the deliberately wide
        band `cv = 2*sqrt(log10(n)/n)` (wider than the +/- 2/sqrt(n) ACF band,
        so it rejects the isolated boundary-crossing lags that a naive ACF rule
        treats as significant). `opt_m` is the first lag starting a run of `kn`
        insignificant lags; the bandwidth is `m = min(2*max(opt_m, 1), m_max)`.
      - With the flat-top window `lam(k) = 1` for `k/m <= 1/2` else
        `2*(1 - k/m)`: `g = sum_k 2*lam*k*acv_k`, `lr_acv = acv_0 + sum_k
        2*lam*acv_k`, and the stationary optimum is
        `b = (2*g^2 / (2*lr_acv^2))^(1/3) * n^(1/3) = ((g/lr_acv)^2 * n)^(1/3)`,
        capped at `b_max`.

    Returns the RAW optimal mean block length. It can be <= 1.0 for a series the
    flat-top criterion judges indistinguishable from iid (the optimal is then
    the iid bootstrap); the caller owns the bootstrap's `expected_block_length >
    1.0` contract (e.g. `max(2.0, b)` as a conservative minimal-dependence
    floor) and should report the raw value as a diagnostic rather than silently
    flooring it. For a series too short to estimate the spectrum (n < 11, where
    `m_max` would reach `n - 1` and the lag-window partial sums go empty) returns
    1.0 (no detectable dependence assumed).

    Reference: Politis, D. N. and White, H. (2004), "Automatic Block-Length
    Selection for the Dependent Bootstrap", Econometric Reviews 23(1), 53-70;
    Patton, A., Politis, D. N. and White, H. (2009), "Correction to ...",
    Econometric Reviews 28(4), 372-375.
    """
    n = len(returns)
    if n < 11:
        return 1.0
    mean = sum(returns) / n
    eps = [float(r) - mean for r in returns]
    b_max = math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    kn = max(5, int(math.log10(n)))
    m_max = math.ceil(math.sqrt(n)) + kn
    cv = 2.0 * math.sqrt(math.log10(n) / n)

    acv = [0.0] * (m_max + 1)
    abs_acorr = [0.0] * (m_max + 1)
    opt_m: int | None = None
    for i in range(m_max + 1):
        ss_lead = sum(e * e for e in eps[i + 1 :])
        ss_lag = sum(e * e for e in eps[: n - i - 1])
        cross = sum(eps[t + i] * eps[t] for t in range(n - i))
        acv[i] = cross / n
        denom = ss_lead * ss_lag
        # An empty/zero partial sum (only reachable if the n < 11 guard is
        # bypassed) is treated as a non-satisfying lag (inf), matching arch's
        # nan, which never satisfies the `< cv` run test.
        abs_acorr[i] = abs(cross) / math.sqrt(denom) if denom > 0.0 else math.inf
        if i >= kn and opt_m is None:
            if all(abs_acorr[j] < cv for j in range(i - kn, i)):
                opt_m = i - kn
    m = min(2 * max(opt_m, 1) if opt_m is not None else m_max, m_max)

    g = 0.0
    lr_acv = acv[0]
    for k in range(1, m + 1):
        lam = 1.0 if k / m <= 0.5 else 2.0 * (1.0 - k / m)
        g += 2.0 * lam * k * acv[k]
        lr_acv += 2.0 * lam * acv[k]
    if lr_acv == 0.0:
        return 1.0
    # The base (g/lr_acv)^2 * n is non-negative, so the cube root is real; the
    # float() cast tells mypy so (float ** float is otherwise typed Any since a
    # negative base could yield complex).
    b_sb = float(((g / lr_acv) ** 2 * n) ** (1.0 / 3.0))
    return min(b_sb, float(b_max))
