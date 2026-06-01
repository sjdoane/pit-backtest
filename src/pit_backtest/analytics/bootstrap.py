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

Block-length selection: the principled choice of `expected_block_length`
for a serially-dependent series is the Politis and White (2004) automatic
selection. M5 does NOT implement automatic selection; the study (M5 PR 3)
documents its chosen `expected_block_length` and the justification (tied to
the momentum return autocorrelation horizon) so the report defends a value
rather than presenting an unjustified magic number.

Reference: Politis, D. N. and Romano, J. P. (1994), "The Stationary
Bootstrap", Journal of the American Statistical Association 89(428),
1303-1313.
"""

from __future__ import annotations

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
