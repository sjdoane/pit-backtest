"""Tests for analytics.sharpe (M4 PR 1).

Per ADR 0001 dec 4 + ADR 0002 acceptance criterion 1 + ADR 0013. The
DSR=0.766 within 1e-3 acceptance pin is `test_dsr_matches_bailey_ldp_2014_numerical_example`; everything else is per-function happy paths,
domain-violation raises, monotonicity smoke, and Acklam Phi_inv spot
checks against table values.

These functions are pure scalar arithmetic; no lookahead-leak boundary
and no RNG, so the determinism + PIT-leak regression families do not
apply. The Acklam (1998) polynomial is bit-deterministic.
"""

from __future__ import annotations

import math

import pytest

from pit_backtest.analytics.sharpe import (
    _EULER_MASCHERONI,
    _phi,
    _phi_inv,
    dsr,
    min_trl,
    psr,
)


# ----- Acklam Phi_inv spot checks -----


def test_phi_inv_at_half_returns_exactly_zero() -> None:
    """Central branch's q = p - 0.5 = 0 annihilates the polynomial; the
    return must be exactly 0.0 with no float tolerance.
    """
    assert _phi_inv(0.5) == 0.0


def test_phi_inv_at_975_matches_table_value() -> None:
    """Phi_inv(0.975) is the canonical "1.96" of the 95% two-sided
    confidence interval. Acklam absolute error < 1.15e-9.
    """
    assert _phi_inv(0.975) == pytest.approx(1.959964, abs=1e-5)


def test_phi_inv_at_025_matches_negative_table_value() -> None:
    assert _phi_inv(0.025) == pytest.approx(-1.959964, abs=1e-5)


def test_phi_inv_at_95_matches_table_value() -> None:
    """Phi_inv(0.95) is 1.6449 for the one-sided 5% test."""
    assert _phi_inv(0.95) == pytest.approx(1.6448536, abs=1e-6)


def test_phi_inv_deep_lower_tail_remains_finite() -> None:
    """At p = 1e-10 the lower-tail branch fires (q = sqrt(-2 ln p) ~ 6.79);
    the result must be finite, not -inf.
    """
    result = _phi_inv(1e-10)
    assert math.isfinite(result)
    assert result < -6.0


def test_phi_inv_rejects_p_at_or_outside_zero_one() -> None:
    """Per post-impl Medium 1: regex pins the literal open-interval
    surface so a future regression mangling the message to `"p in [0, 1)"`
    (with brackets) is caught.
    """
    pattern = r"p in \(0, 1\); got p"
    with pytest.raises(ValueError, match=pattern):
        _phi_inv(0.0)
    with pytest.raises(ValueError, match=pattern):
        _phi_inv(1.0)
    with pytest.raises(ValueError, match=pattern):
        _phi_inv(-0.1)
    with pytest.raises(ValueError, match=pattern):
        _phi_inv(1.5)


def test_phi_saturates_at_z_above_eight_point_three() -> None:
    """Per post-impl Medium 2: `_phi` docstring promises saturation via
    `math.erf` at `z > 8.3` (returns 1.0 exactly) and `z < -8.3`
    (returns 0.0 exactly). Pin both so a future swap to an Acklam-style
    polynomial that does NOT saturate would be caught.
    """
    assert _phi(8.3) == 1.0
    assert _phi(-8.3) == 0.0


# ----- PSR -----


def test_psr_sr_hat_equals_sr_star_returns_one_half() -> None:
    """Numerator is zero; Phi(0) = 0.5 exactly."""
    assert psr(sr_hat=1.0, sr_star=1.0, T=60, gamma_3=0.0, gamma_4=3.0) == 0.5


def test_psr_normal_returns_matches_hand_computation() -> None:
    """For normal returns (gamma_3=0, gamma_4=3): sigma_sq = 1 + SR_hat^2/2.
    SR_hat = 0.5, SR* = 0, T = 100, sigma_sq = 1.125; z = 0.5 * sqrt(99)
    / sqrt(1.125) = 4.690. Phi(4.690) ~ 0.99999863. Tolerance 1e-7
    accommodates Acklam-vs-math.erf precision.
    """
    result = psr(sr_hat=0.5, sr_star=0.0, T=100, gamma_3=0.0, gamma_4=3.0)
    expected = _phi(0.5 * math.sqrt(99) / math.sqrt(1.125))
    assert result == pytest.approx(expected, abs=1e-7)
    assert result == pytest.approx(0.99999863, abs=1e-6)


def test_psr_raises_when_t_below_two() -> None:
    with pytest.raises(ValueError, match="T >= 2"):
        psr(sr_hat=1.5, sr_star=0.0, T=1, gamma_3=0.0, gamma_4=3.0)
    with pytest.raises(ValueError, match="T >= 2"):
        psr(sr_hat=1.5, sr_star=0.0, T=0, gamma_3=0.0, gamma_4=3.0)


def test_psr_t_equals_two_boundary_returns_finite() -> None:
    """Smallest valid T; sqrt(T-1) = 1. Asserts no raise and finite
    result in [0, 1].
    """
    result = psr(sr_hat=1.0, sr_star=0.0, T=2, gamma_3=0.0, gamma_4=3.0)
    assert math.isfinite(result)
    assert 0.0 <= result <= 1.0


def test_psr_raises_when_sigma_sq_non_positive() -> None:
    """Algebra-degenerate corner: SR_hat = 4, gamma_3 = 4, gamma_4 = 1:
    sigma_sq = 1 - 16 + 0 = -15.
    """
    with pytest.raises(ValueError, match="sigma_sq"):
        psr(sr_hat=4.0, sr_star=0.0, T=60, gamma_3=4.0, gamma_4=1.0)


def test_psr_monotone_in_sr_hat() -> None:
    """Higher SR_hat (holding sr_star, T, gamma_3, gamma_4 fixed) raises
    the numerator and lowers sigma_sq (for gamma_3 < 0), producing
    larger z and therefore larger PSR.
    """
    low = psr(sr_hat=0.5, sr_star=0.0, T=60, gamma_3=0.0, gamma_4=3.0)
    high = psr(sr_hat=1.5, sr_star=0.0, T=60, gamma_3=0.0, gamma_4=3.0)
    assert low < high


def test_psr_normal_vs_negative_skew_penalty() -> None:
    """Negative skewness inflates sigma_sq and lowers PSR holding SR_hat
    fixed. Crash-prone strategies receive a larger penalty.
    """
    normal = psr(sr_hat=1.0, sr_star=0.0, T=60, gamma_3=0.0, gamma_4=3.0)
    skewed = psr(sr_hat=1.0, sr_star=0.0, T=60, gamma_3=-1.0, gamma_4=6.0)
    assert skewed < normal


# ----- DSR (the M4 PR 1 acceptance gate) -----


def test_dsr_matches_bailey_ldp_2014_numerical_example() -> None:
    """ADR 0002 acceptance criterion 1 (as corrected by ADR 0013):
    SR_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5, v_sr=0.4, n_effective=30
    -> DSR = 0.766 within 1e-3.

    The original ADR text claimed 0.971 derived from incorrect
    inverse-normal quantile values in the methodology research note;
    ADR 0013 locks the canonical Bailey-LdP 2014 Wald form (sigma_sq
    uses SR_hat, not SR_0) and the corrected pin.

    The 1e-3 tolerance is the ADR-mandated abs; a tighter informational
    pin lives in
    `test_dsr_bailey_ldp_2014_tighter_pin_for_implementation_visibility`.
    """
    result = dsr(
        sr_hat=1.5,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
        v_sr=0.4,
        n_effective=30,
    )
    assert result == pytest.approx(0.766, abs=1e-3)


def test_dsr_bailey_ldp_2014_tighter_pin_for_implementation_visibility() -> None:
    """Informational tighter pin so a future Acklam-coefficient refactor
    that drifts within the 1e-3 acceptance window is still observable
    in the regression suite. scipy.stats.norm v1.17.1 on the project
    venv returns 0.765653 for the same inputs (the ground truth ADR
    0013 cited); the Acklam-based implementation lands within 1e-4 of
    that value.
    """
    result = dsr(
        sr_hat=1.5,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
        v_sr=0.4,
        n_effective=30,
    )
    assert result == pytest.approx(0.7657, abs=1e-4)


def test_dsr_n_effective_one_degenerates_to_psr_zero() -> None:
    """Per ADR 0013 decision 5 + methodology doc line 214: with no
    multiple-testing penalty, DSR reduces to PSR with the threshold set
    to zero.
    """
    expected = psr(
        sr_hat=1.2, sr_star=0.0, T=60, gamma_3=0.0, gamma_4=3.0
    )
    result = dsr(
        sr_hat=1.2,
        T=60,
        gamma_3=0.0,
        gamma_4=3.0,
        v_sr=0.4,
        n_effective=1,
    )
    assert result == expected


def test_dsr_raises_when_n_effective_below_one() -> None:
    with pytest.raises(ValueError, match="n_effective >= 1"):
        dsr(
            sr_hat=1.5,
            T=60,
            gamma_3=0.0,
            gamma_4=3.0,
            v_sr=0.4,
            n_effective=0,
        )
    with pytest.raises(ValueError, match="n_effective >= 1"):
        dsr(
            sr_hat=1.5,
            T=60,
            gamma_3=0.0,
            gamma_4=3.0,
            v_sr=0.4,
            n_effective=-5,
        )


def test_dsr_raises_when_v_sr_negative() -> None:
    with pytest.raises(ValueError, match="v_sr >= 0"):
        dsr(
            sr_hat=1.5,
            T=60,
            gamma_3=0.0,
            gamma_4=3.0,
            v_sr=-0.1,
            n_effective=10,
        )


def test_dsr_monotone_decreasing_in_n_effective() -> None:
    """Higher N raises sr_0 (more trials produce a higher false-strategy
    maximum), which lowers DSR. Pin the monotonicity with two N values
    holding everything else fixed.
    """
    low_n = dsr(
        sr_hat=1.5,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
        v_sr=0.4,
        n_effective=10,
    )
    high_n = dsr(
        sr_hat=1.5,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
        v_sr=0.4,
        n_effective=100,
    )
    assert high_n < low_n


def test_dsr_v_sr_zero_degenerates_to_psr_zero() -> None:
    """When v_sr = 0 the sr_0 expression collapses to 0 because the
    sqrt(v_sr) prefactor is zero; DSR equals PSR(0) regardless of N.
    """
    result = dsr(
        sr_hat=1.5,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
        v_sr=0.0,
        n_effective=30,
    )
    expected = psr(
        sr_hat=1.5,
        sr_star=0.0,
        T=60,
        gamma_3=-0.5,
        gamma_4=5.0,
    )
    assert result == pytest.approx(expected, abs=1e-12)


# ----- MinTRL -----


def test_min_trl_normal_returns_matches_methodology_doc_value() -> None:
    """Methodology doc line 261: SR_hat = 1.0, SR* = 0, alpha = 0.05,
    normal returns -> 5.06 months. Per ADR 0013 decision 6 the return
    type is `float`; callers apply `math.ceil` for an integer count.
    """
    result = min_trl(
        sr_hat=1.0,
        sr_star=0.0,
        alpha=0.05,
        gamma_3=0.0,
        gamma_4=3.0,
    )
    assert result == pytest.approx(5.058, abs=1e-2)


def test_min_trl_options_selling_profile_matches_methodology_doc_value() -> None:
    """Methodology doc line 262: SR_hat = 1.0, SR* = 0, alpha = 0.05,
    gamma_3 = -1, gamma_4 = 6 -> 9.79 months.
    """
    result = min_trl(
        sr_hat=1.0,
        sr_star=0.0,
        alpha=0.05,
        gamma_3=-1.0,
        gamma_4=6.0,
    )
    assert result == pytest.approx(9.794, abs=1e-2)


def test_min_trl_returns_float_per_adr_0013_amendment() -> None:
    """Per ADR 0013 decision 6 the return type is `float`, NOT `int`
    (the ADR 0003 stub's `-> int` was a misreading of "minimum"). Pin
    the type so a future refactor that adds `math.ceil` is caught.
    """
    result = min_trl(
        sr_hat=1.0,
        sr_star=0.0,
        alpha=0.05,
        gamma_3=0.0,
        gamma_4=3.0,
    )
    assert isinstance(result, float)


def test_min_trl_alpha_half_degenerates_to_one() -> None:
    """Phi_inv(0.5) = 0 -> z = 0 -> MinTRL = 1 + sigma_sq * 0 = 1. The
    50% confidence boundary degenerates to the single-observation
    minimum.
    """
    result = min_trl(
        sr_hat=1.5,
        sr_star=0.0,
        alpha=0.5,
        gamma_3=0.0,
        gamma_4=3.0,
    )
    assert result == pytest.approx(1.0, abs=1e-9)


def test_min_trl_raises_when_alpha_outside_open_unit_interval() -> None:
    with pytest.raises(ValueError, match="alpha"):
        min_trl(
            sr_hat=1.5, sr_star=0.0, alpha=0.0, gamma_3=0.0, gamma_4=3.0
        )
    with pytest.raises(ValueError, match="alpha"):
        min_trl(
            sr_hat=1.5, sr_star=0.0, alpha=1.0, gamma_3=0.0, gamma_4=3.0
        )
    with pytest.raises(ValueError, match="alpha"):
        min_trl(
            sr_hat=1.5, sr_star=0.0, alpha=-0.1, gamma_3=0.0, gamma_4=3.0
        )


def test_min_trl_raises_when_sr_hat_not_above_sr_star() -> None:
    """Equality and below both raise; formula has no finite lower bound
    when the strategy never exceeds the threshold.
    """
    with pytest.raises(ValueError, match="sr_hat > sr_star"):
        min_trl(
            sr_hat=1.0, sr_star=1.0, alpha=0.05, gamma_3=0.0, gamma_4=3.0
        )
    with pytest.raises(ValueError, match="sr_hat > sr_star"):
        min_trl(
            sr_hat=0.5, sr_star=1.0, alpha=0.05, gamma_3=0.0, gamma_4=3.0
        )


def test_min_trl_alpha_tighter_lengthens_required_period() -> None:
    """Tighter alpha (smaller) needs more observations. Pin monotonicity:
    alpha=0.01 demands more T than alpha=0.05.
    """
    t_99 = min_trl(
        sr_hat=1.0,
        sr_star=0.0,
        alpha=0.01,
        gamma_3=0.0,
        gamma_4=3.0,
    )
    t_95 = min_trl(
        sr_hat=1.0,
        sr_star=0.0,
        alpha=0.05,
        gamma_3=0.0,
        gamma_4=3.0,
    )
    assert t_99 > t_95


# ----- Constants -----


def test_euler_mascheroni_at_published_precision() -> None:
    """Per ADR 0013 decision 8 hardcoded at 16-digit precision."""
    assert _EULER_MASCHERONI == 0.5772156649015329
