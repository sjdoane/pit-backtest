"""PSR, DSR, MinTRL (Bailey-LdP 2012 + Bailey-LdP 2014).

Per ADR 0001 decision 4 + ADR 0002 acceptance criterion 1 + ADR 0003
decision 14 + ADR 0013 (the API contract + Bailey-LdP 2014 numerical
pin correction). Implementations match the Bailey-LdP 2014 worked
example to within 1e-3 absolute on the canonical inputs:
SR_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4 ->
DSR=0.766 per ADR 0013. The original 0.971 number came from incorrect
inverse-normal quantile values in the methodology research note.

Conventions locked by ADR 0013:
- `sigma_sq = 1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2` (Wald
  form, SR_hat in the denominator) for both PSR and DSR.
- `MinTRL -> float` (Bailey-LdP 2012 publishes a real-valued lower bound;
  the ADR 0003 stub `-> int` was a misreading of the "minimum" qualifier).
- `dsr(n_effective=1)` degenerates to `psr(sr_hat, sr_star=0.0, ...)` per
  methodology doc line 214 (does NOT raise).
- Domain violations raise `ValueError` consistent with codebase
  discipline; NaN-returning analytics are an anti-pattern that would
  silently propagate through the M4 PR 5 scorecard renderer.

Dependencies: Python stdlib `math` only. `Phi` via `math.erf`; `Phi_inv`
via the Acklam (1998) public-domain polynomial approximation with
absolute error below 1.15e-9 over [1e-15, 1 - 1e-15]. The 1e-3
acceptance pin tolerates the 1e-9 inverse-CDF accuracy with three
orders of magnitude of headroom. scipy is NOT a project dependency
per ADR 0013 decision 11.

See `docs/research/sources/methodology-backtest-overfitting.md` for the
formula derivations and the corrected worked example.
"""

from __future__ import annotations

import math
from typing import Final


_EULER_MASCHERONI: Final[float] = 0.5772156649015329
"""The Euler-Mascheroni constant gamma_E to 16 significant digits (the
maximum precision IEEE-754 binary64 can represent for this irrational
constant). Used in the False Strategy Theorem benchmark sr_0 derivation
inside DSR. Cited in `methodology-backtest-overfitting.md:153` and the
Wikipedia 'Euler-Mascheroni constant' page."""


# Acklam (1998) inverse normal CDF rational approximation coefficients.
# Public-domain reference: Peter J. Acklam, "An algorithm for computing
# the inverse normal cumulative distribution function" (1998). Absolute
# error |Phi_inv(p) - true| < 1.15e-9 over [1e-15, 1 - 1e-15].
#
# Three regions per Acklam: lower tail (p < _P_LOW), central
# (_P_LOW <= p <= _P_HIGH), upper tail (p > _P_HIGH). The central
# branch uses a (a, b) rational; the tail branches use a (c, d) rational
# on q = sqrt(-2 * ln(p)) (or 1 - p for the upper tail by mirror
# symmetry).
_P_LOW: Final[float] = 0.02425
_P_HIGH: Final[float] = 1.0 - _P_LOW

_A: Final[tuple[float, ...]] = (
    -39.69683028665376,
    220.9460984245205,
    -275.9285104469687,
    138.3577518672690,
    -30.66479806614716,
    2.506628277459239,
)
_B: Final[tuple[float, ...]] = (
    -54.47609879822406,
    161.5858368580409,
    -155.6989798598866,
    66.80131188771972,
    -13.28068155288572,
)
_C: Final[tuple[float, ...]] = (
    -0.007784894002430293,
    -0.3223964580411365,
    -2.400758277161838,
    -2.549732539343734,
    4.374664141464968,
    2.938163982698783,
)
_D: Final[tuple[float, ...]] = (
    0.007784695709041462,
    0.3224671290700398,
    2.445134137142996,
    3.754408661907416,
)


def _phi(z: float) -> float:
    """Standard normal CDF via `math.erf`.

    `Phi(z) = 0.5 * (1 + erf(z / sqrt(2)))`. `math.erf` is stdlib since
    Python 3.2 and accurate to ~1e-15 absolute. For `z > 8.3` the
    function saturates at exactly 1.0; for `z < -8.3` it saturates at
    exactly 0.0. Both regimes are well outside the operating range of
    PSR/DSR (which expects `|z|` of single digits).
    """
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Standard normal inverse CDF via Acklam (1998) rational approximation.

    Returns the standard normal quantile such that `Phi(z) = p`.

    `_phi_inv(0.5) == 0.0` exactly (central branch's `q = p - 0.5 = 0`
    annihilates the polynomial). `_phi_inv(0.025)` and `_phi_inv(0.975)`
    return approximately `-1.960` and `+1.960`. Absolute error below
    1.15e-9 over `[1e-15, 1 - 1e-15]`; at the extreme tails `Phi_inv`
    is undefined (infinite quantile) and the function raises.

    Raises:
      ValueError: when `p` is not in `(0, 1)`.
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"_phi_inv requires p in (0, 1); got p={p!r}")
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        numerator = (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q
            + _C[5]
        )
        denominator = (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        )
        return numerator / denominator
    if p > _P_HIGH:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        numerator = (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q
            + _C[5]
        )
        denominator = (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        )
        return -numerator / denominator
    q = p - 0.5
    r = q * q
    numerator = (
        (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r
            + _A[5])
        * q
    )
    denominator = (
        ((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0
    )
    return numerator / denominator


def _sigma_sq(sr_hat: float, gamma_3: float, gamma_4: float) -> float:
    """Wald-form asymptotic-variance denominator term (Lo 2002 estimator).

    `sigma_sq = 1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2`. Used
    inside the PSR formula (denominator of the standardization) and
    inherited by DSR per ADR 0013 decision 4 (Wald form, SR_hat in the
    denominator). For normal returns (`gamma_3 = 0`, `gamma_4 = 3`) the
    expression reduces to `1 + SR_hat^2 / 2`. Negative skewness and
    excess kurtosis both inflate the variance, which is why crash-prone
    strategies receive a larger DSR penalty.
    """
    return 1.0 - gamma_3 * sr_hat + (gamma_4 - 1.0) / 4.0 * sr_hat * sr_hat


def psr(
    sr_hat: float, sr_star: float, T: int, gamma_3: float, gamma_4: float
) -> float:
    """Probabilistic Sharpe Ratio (Bailey-LdP 2012; ADR 0013 decision 4).

    `PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(T - 1) / sqrt(sigma_sq) )`

    where `sigma_sq` is the Lo 2002 asymptotic-variance correction
    incorporating realized skewness and kurtosis.

    Args:
      sr_hat: estimated Sharpe ratio (non-annualized, in the same units
        as the return observations).
      sr_star: benchmark / reference Sharpe ratio (often 0 or the Sharpe
        of a passive benchmark).
      T: number of return observations; must be >= 2.
      gamma_3: realized skewness (third standardized moment).
      gamma_4: realized kurtosis (fourth standardized moment, non-excess
        form; the normal-distribution value is 3).

    Returns:
      The probability that the true SR exceeds `sr_star` given T
      observations. Range `[0, 1]`.

    Raises:
      ValueError: when `T < 2` or when the variance term `sigma_sq` goes
        non-positive (the algebra-degenerate corner; flagged loudly per
        ADR 0013 decision 7).
    """
    if T < 2:
        raise ValueError(f"PSR requires T >= 2 observations; got T={T}")
    sigma_sq = _sigma_sq(sr_hat, gamma_3, gamma_4)
    if sigma_sq <= 0.0:
        raise ValueError(
            f"PSR variance term sigma_sq={sigma_sq:.6f} is non-positive; "
            f"inputs (sr_hat={sr_hat}, gamma_3={gamma_3}, gamma_4={gamma_4}) "
            f"are in the algebra-degenerate corner."
        )
    z = (sr_hat - sr_star) * math.sqrt(T - 1) / math.sqrt(sigma_sq)
    return _phi(z)


def dsr(
    sr_hat: float,
    T: int,
    gamma_3: float,
    gamma_4: float,
    v_sr: float,
    n_effective: int,
) -> float:
    """Deflated Sharpe Ratio (Bailey-LdP 2014; ADR 0013 decision 5).

    `DSR = PSR(sr_0)` where `sr_0` is the False Strategy Theorem
    benchmark:

      `sr_0 = sqrt(V[{SR_n}]) * ( (1 - gamma_E) * Phi_inv(1 - 1/N)
                                  + gamma_E * Phi_inv(1 - 1/(N*e)) )`

    Args:
      sr_hat: estimated Sharpe ratio.
      T: number of return observations; must be >= 2.
      gamma_3: realized skewness.
      gamma_4: realized kurtosis.
      v_sr: cross-sectional variance of the Sharpe estimates across the
        n_effective trials. Must be >= 0.
      n_effective: effective number of independent trials. Must be >= 1.
        For `n_effective == 1` (no multiple-testing deflation) the
        function degenerates to `psr(sr_hat, sr_star=0.0, T, gamma_3,
        gamma_4)` per methodology doc line 214 and ADR 0013 decision 5.

    Returns:
      The probability that the true SR exceeds the multiple-testing
      threshold `sr_0` given T observations. Range `[0, 1]`.

    Raises:
      ValueError: when `v_sr < 0` or `n_effective < 1`. Other domain
        errors propagate from the nested PSR call (`T < 2`, `sigma_sq
        <= 0`).
    """
    if n_effective < 1:
        raise ValueError(
            f"DSR requires n_effective >= 1; got n_effective={n_effective}"
        )
    if v_sr < 0.0:
        raise ValueError(
            f"DSR requires v_sr >= 0 (variance of SR across trials); "
            f"got v_sr={v_sr}"
        )
    if n_effective == 1:
        # ADR 0013 decision 5: degenerates to PSR(0) per methodology
        # doc line 214 (single-trial case; no multiple-testing penalty).
        return psr(sr_hat, 0.0, T, gamma_3, gamma_4)
    q1 = _phi_inv(1.0 - 1.0 / n_effective)
    q2 = _phi_inv(1.0 - 1.0 / (n_effective * math.e))
    sr_0 = math.sqrt(v_sr) * (
        (1.0 - _EULER_MASCHERONI) * q1 + _EULER_MASCHERONI * q2
    )
    return psr(sr_hat, sr_0, T, gamma_3, gamma_4)


def min_trl(
    sr_hat: float,
    sr_star: float,
    alpha: float,
    gamma_3: float,
    gamma_4: float,
) -> float:
    """Minimum Track Record Length (Bailey-LdP 2012; ADR 0013 decision 6).

    `MinTRL(SR*) = 1 + sigma_sq * (Phi_inv(1 - alpha) / (SR_hat - SR*))^2`

    The smallest number of observations T such that the realized SR
    exceeds `sr_star` with confidence at least `1 - alpha` under the
    Lo 2002 asymptotic distribution.

    Args:
      sr_hat: estimated Sharpe ratio. Must exceed sr_star (otherwise the
        formula has no positive lower bound; ADR 0013 decision 7 raises).
      sr_star: benchmark Sharpe ratio.
      alpha: significance level. Must be in `(0, 1)`.
      gamma_3: realized skewness.
      gamma_4: realized kurtosis.

    Returns:
      The real-valued lower bound on T per Bailey-LdP 2012's published
      form (ADR 0013 decision 6 amended the ADR 0003 stub's `-> int`
      misreading). Callers that need an integer period count apply
      `math.ceil` at the call site.

    Raises:
      ValueError: when `alpha` is outside `(0, 1)`, `sr_hat <= sr_star`,
        or `sigma_sq <= 0`.
    """
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"MinTRL requires alpha in (0, 1); got alpha={alpha}")
    if sr_hat <= sr_star:
        raise ValueError(
            f"MinTRL requires sr_hat > sr_star for a finite track record "
            f"lower bound; got sr_hat={sr_hat}, sr_star={sr_star}"
        )
    sigma_sq = _sigma_sq(sr_hat, gamma_3, gamma_4)
    if sigma_sq <= 0.0:
        raise ValueError(
            f"MinTRL variance term sigma_sq={sigma_sq:.6f} is non-positive; "
            f"inputs (sr_hat={sr_hat}, gamma_3={gamma_3}, gamma_4={gamma_4}) "
            f"are in the algebra-degenerate corner."
        )
    z = _phi_inv(1.0 - alpha)
    return 1.0 + sigma_sq * (z / (sr_hat - sr_star)) ** 2
