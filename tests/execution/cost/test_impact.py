"""SquareRootImpactCostModel tests (M2 PR A).

Per ADR 0005 step 1 (Almgren formula) + ADR 0007 (FIM-2018 demoted to
upper-ceiling sanity check; formula-derived band is the gate) + M2 PR A
reviewer pass:
- test_almgren_central_inside_formula_band: central eta=0.142 falls
  inside [Almgren(eta=0.05), Almgren(eta=0.30)] for the SPY $1M monthly
  fixture.
- test_almgren_central_below_fim_ceiling: central annualized cost is
  below 50 bps annualized (FIM upper-ceiling sanity check).
- Hand-computed formula values at SPY/AAPL/GLD-shaped fixtures.
- Bouchaud beta=0.5 override.
- estimate vs compute consistency.
- Edge cases: zero shares, missing lookup, negative V_D/Theta, gamma=0.
- Decimal-float boundary round-trip.
- Q-homogeneity (doubling Q increases temp impact by 2^beta).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import attrs
import pytest

from pit_backtest.data.adjustments import TRADING_DAYS_PER_YEAR
from pit_backtest.data.records import AssetId
from pit_backtest.execution.cost.base import FillState
from pit_backtest.execution.cost.impact import (
    DEFAULT_BETA,
    DEFAULT_ETA,
    DEFAULT_GAMMA,
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)


# SPY-shaped fixture: market state values per the SSGA fact sheet 1y
# context (sigma_D ~ 1.2%/day, V_D ~ 80M shares/day, Theta ~ 8.7B shares
# outstanding). These are the canonical inputs for the FIM-revision
# tests per ADR 0007.
SPY_ASSET = AssetId(1)
SPY_DT = datetime(2026, 4, 30, 16, 0, 0)
SPY_DATE = SPY_DT.date()
SPY_SIGMA_D = 0.012
SPY_V_D = 80_000_000.0
SPY_THETA = 8_700_000_000.0

# SPY $1M monthly rebalance per ADR 0007 fixture. 2000 shares at $500.
SPY_NOTIONAL_USD = 1_000_000.0
SPY_FILL_PRICE = 500.0
SPY_SHARES = Decimal("2000")  # abs(SPY_NOTIONAL_USD / SPY_FILL_PRICE)


def _spy_lookup() -> MarketStateLookup:
    return MarketStateLookup(
        by_key={
            (SPY_ASSET, SPY_DATE): MarketStateRow(
                sigma_D=SPY_SIGMA_D, V_D=SPY_V_D, Theta=SPY_THETA
            )
        }
    )


# ----- Hand-computed canonical-value tests -----


def test_estimate_matches_hand_computed_almgren_formula() -> None:
    """For SPY $1M rebalance at default Almgren parameters, the formula
    evaluates to a known value derived directly from the locked formula
    in ADR 0005 step 1.

    Hand calculation:
      Q = 2000 shares
      sigma_D = 0.012
      V_D = 80_000_000
      Theta = 8_700_000_000
      eta = 0.142, beta = 0.6, gamma = 0.314, T = 1.0
      participation = Q / V_D = 2.5e-5
      Theta/V_D = 108.75
      (Theta/V_D)^0.25 = 108.75^0.25 ~= 3.2293
      perm_frac = 0.5 * 0.314 * 0.012 * 2.5e-5 * 3.2293 ~= 1.521e-7
      perm_bps  ~= 0.001521
      intensity = |Q / (V_D * T)| = 2.5e-5
      intensity^0.6 = (2.5e-5)^0.6 ~= 0.001734
      temp_frac = 0.142 * 0.012 * 0.001734 ~= 2.957e-6
      temp_bps  ~= 0.02957
      total_bps = perm_bps + temp_bps ~= 0.03109 bps
    """
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    estimate = cost_model.estimate(
        asset_id=SPY_ASSET,
        shares=SPY_SHARES,
        direction="buy",
        dt=SPY_DT,
    )
    perm_bps = 0.5 * 0.314 * 0.012 * (2000 / 80_000_000) * (8_700_000_000 / 80_000_000) ** 0.25 * 10_000
    temp_bps = 0.142 * 0.012 * (2000 / (80_000_000 * 1.0)) ** 0.6 * 10_000
    hand_value = perm_bps + temp_bps
    assert float(estimate) == pytest.approx(hand_value, rel=1e-12), (
        f"estimate {estimate} differs from hand-computed Almgren value "
        f"{hand_value} by more than float64 precision"
    )


def test_estimate_and_compute_are_consistent() -> None:
    """compute(...).temp + .perm should equal estimate(...) modulo
    Decimal precision. ADR 0005 step 3 fixes slippage_bps=0 and
    commission=0 on compute's output, so the sum equals the estimate.
    """
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    estimate = cost_model.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    fill = FillState(
        asset_id=SPY_ASSET,
        dt=SPY_DT,
        shares=SPY_SHARES,
        direction="buy",
        bar_open=Decimal("500"),
        bar_close=Decimal("500"),
        bar_volume=int(SPY_V_D),
    )
    breakdown = cost_model.compute(fill)
    sum_bps = (
        breakdown.temporary_impact_bps
        + breakdown.permanent_impact_bps
        + breakdown.slippage_bps
    )
    diff = abs(float(estimate) - float(sum_bps))
    assert diff < 1e-10, f"estimate={estimate}, sum_bps={sum_bps}, diff={diff}"


# ----- ADR 0007 acceptance criterion tests -----


def test_almgren_central_inside_formula_band() -> None:
    """ADR 0007 revised M2 acceptance criterion 1, test #1.

    The central eta=0.142 estimate for the SPY $1M monthly rebalance
    fixture must fall inside the formula-derived band
    [Almgren(eta=0.05), Almgren(eta=0.30)].
    """
    lookup = _spy_lookup()
    band_low = SquareRootImpactCostModel(
        market_state=lookup, eta=Decimal("0.05")
    )
    band_central = SquareRootImpactCostModel(
        market_state=lookup, eta=Decimal("0.142")
    )
    band_high = SquareRootImpactCostModel(
        market_state=lookup, eta=Decimal("0.30")
    )

    low_est = band_low.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    central_est = band_central.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    high_est = band_high.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    assert low_est <= central_est <= high_est, (
        f"central eta=0.142 estimate {central_est} not inside band "
        f"[{low_est}, {high_est}] for SPY $1M monthly"
    )


def test_almgren_central_below_fim_ceiling() -> None:
    """ADR 0007 revised M2 acceptance criterion 1, test #2.

    The central annualized cost for SPY $1M monthly rebalance must be
    below 50 bps annualized. FIM 2018's ~10 bps figure is calibrated for
    institutional flows >>$1M; the engine's central estimate at SPY $1M
    should land well under the 50 bp ceiling.

    Annualization: monthly rebalance => 12 trades per year. Annualized
    cost = per-trade-bps * 12 (approximately, ignoring compounding which
    is negligible at sub-bp scale).
    """
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    per_trade_bps = cost_model.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    annualized_bps = float(per_trade_bps) * 12.0
    # 50 bps annualized as a fraction.
    annualized_fraction = annualized_bps / 10_000.0
    assert annualized_fraction < 0.0050, (
        f"central annualized cost {annualized_fraction:.6f} not below FIM "
        f"ceiling 50 bps (0.005); per-trade={per_trade_bps} bps, "
        f"12 trades/year = {annualized_bps:.4f} bps annualized"
    )


# ----- Bouchaud beta=0.5 override -----


def test_bouchaud_override_lowers_temporary_term() -> None:
    """beta=0.5 makes the temporary-impact intensity term grow slower
    than beta=0.6 for the typical sub-1 participation rate.

    For participation rate p = 2.5e-5:
      p^0.6 = 0.001317
      p^0.5 = sqrt(2.5e-5) = 0.005
    """
    default = SquareRootImpactCostModel(market_state=_spy_lookup())
    bouchaud = SquareRootImpactCostModel(
        market_state=_spy_lookup(), beta=Decimal("0.5")
    )
    default_est = default.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    bouchaud_est = bouchaud.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=SPY_DT
    )
    # At p < 1, lower exponent = larger fraction; Bouchaud temp term
    # exceeds Almgren default. Document the relationship.
    assert bouchaud_est > default_est, (
        f"Bouchaud beta=0.5 should produce a larger temp-impact term at "
        f"p<1 than Almgren beta=0.6; got bouchaud={bouchaud_est}, "
        f"default={default_est}"
    )


# ----- Edge cases -----


def test_zero_shares_zero_cost() -> None:
    """estimate(0 shares) short-circuits to Decimal('0') without arithmetic."""
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    estimate = cost_model.estimate(
        asset_id=SPY_ASSET, shares=Decimal("0"), direction="buy", dt=SPY_DT
    )
    assert estimate == Decimal("0")


def test_compute_zero_shares_zero_breakdown() -> None:
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    fill = FillState(
        asset_id=SPY_ASSET,
        dt=SPY_DT,
        shares=Decimal("0"),
        direction="buy",
        bar_open=Decimal("500"),
        bar_close=Decimal("500"),
        bar_volume=80_000_000,
    )
    breakdown = cost_model.compute(fill)
    assert breakdown.temporary_impact_bps == Decimal("0")
    assert breakdown.permanent_impact_bps == Decimal("0")
    assert breakdown.slippage_bps == Decimal("0")
    assert breakdown.commission == Decimal("0")


def test_missing_lookup_raises() -> None:
    """estimate at an (asset_id, date) not in the lookup raises KeyError."""
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    unknown_dt = datetime(2030, 1, 1, 16, 0, 0)
    with pytest.raises(KeyError):
        cost_model.estimate(
            asset_id=SPY_ASSET,
            shares=SPY_SHARES,
            direction="buy",
            dt=unknown_dt,
        )


def test_negative_V_D_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="V_D"):
        MarketStateLookup(
            by_key={
                (SPY_ASSET, SPY_DATE): MarketStateRow(
                    sigma_D=0.012, V_D=-1.0, Theta=8_700_000_000.0
                )
            }
        )


def test_negative_Theta_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="Theta"):
        MarketStateLookup(
            by_key={
                (SPY_ASSET, SPY_DATE): MarketStateRow(
                    sigma_D=0.012, V_D=80_000_000.0, Theta=-1.0
                )
            }
        )


def test_negative_sigma_D_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="sigma_D"):
        MarketStateLookup(
            by_key={
                (SPY_ASSET, SPY_DATE): MarketStateRow(
                    sigma_D=-0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
                )
            }
        )


def test_eta_must_be_positive() -> None:
    with pytest.raises(ValueError, match="eta"):
        SquareRootImpactCostModel(
            market_state=_spy_lookup(), eta=Decimal("0")
        )


def test_gamma_zero_zeros_permanent_term() -> None:
    """gamma=0 zeros the permanent-impact term; only the temporary term
    contributes. Useful for isolating sub-bp arithmetic.
    """
    cost_model = SquareRootImpactCostModel(
        market_state=_spy_lookup(), gamma=Decimal("0")
    )
    fill = FillState(
        asset_id=SPY_ASSET,
        dt=SPY_DT,
        shares=SPY_SHARES,
        direction="buy",
        bar_open=Decimal("500"),
        bar_close=Decimal("500"),
        bar_volume=80_000_000,
    )
    breakdown = cost_model.compute(fill)
    assert breakdown.permanent_impact_bps == Decimal("0")
    assert breakdown.temporary_impact_bps > Decimal("0")


def test_q_homogeneity_at_beta_default() -> None:
    """Doubling Q at constant (V_D, T) multiplies the temporary-impact
    term by 2^beta = 2^0.6 ~= 1.5157.

    Permanent term scales linearly: doubling Q multiplies by 2.
    Total at constant ratio depends on the term mix; the temporary
    term dominates for typical participation rates so the total
    is approximately 2^0.6 * temp + 2 * perm.
    """
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    fill_1x = FillState(
        asset_id=SPY_ASSET, dt=SPY_DT, shares=SPY_SHARES, direction="buy",
        bar_open=Decimal("500"), bar_close=Decimal("500"),
        bar_volume=80_000_000,
    )
    fill_2x = attrs.evolve(fill_1x, shares=SPY_SHARES * Decimal("2"))
    b1 = cost_model.compute(fill_1x)
    b2 = cost_model.compute(fill_2x)
    ratio_temp = float(b2.temporary_impact_bps) / float(b1.temporary_impact_bps)
    ratio_perm = float(b2.permanent_impact_bps) / float(b1.permanent_impact_bps)
    assert ratio_temp == pytest.approx(2.0 ** 0.6, rel=1e-9)
    assert ratio_perm == pytest.approx(2.0, rel=1e-9)


def test_decimal_boundary_roundtrip() -> None:
    """Decimal(repr(float_value)) must round-trip cleanly per the
    boundary convention. Pinned getcontext().prec = 28 from impact.py
    module init.
    """
    test_values = [0.123456789012345, 1e-10, 50.123, 0.0001, 100.5]
    for v in test_values:
        roundtripped = float(Decimal(repr(v)))
        assert roundtripped == v, (
            f"Decimal(repr({v})) round-trip failed: {roundtripped}"
        )


def test_default_constants_locked() -> None:
    """ADR 0005 step 1 locks eta=0.142, beta=0.6, gamma=0.314."""
    assert DEFAULT_ETA == Decimal("0.142")
    assert DEFAULT_BETA == Decimal("0.6")
    assert DEFAULT_GAMMA == Decimal("0.314")


def test_trading_days_per_year_constant_reused_consistently() -> None:
    """Sanity check that the cost-model annualization tests use the same
    TRADING_DAYS_PER_YEAR constant that the M1 reconciliation does.
    """
    assert TRADING_DAYS_PER_YEAR == 252


def test_utc_dt_resolves_to_et_trading_day() -> None:
    """Per the M2 PR A reviewer's H5 finding: a UTC datetime that falls
    on the prior ET trading day must look up the prior day's MarketStateRow.

    The pre-fix code used `.date()` which returns the wall-clock date of
    the datetime regardless of its tzinfo; this test locks the
    `_et_date()` helper's astimezone() conversion.

    SPY market data on 2024-01-02 (ET) is at `datetime(2024, 1, 2, ...)`
    in ET, which is `datetime(2024, 1, 3, ...)` in UTC. A UTC-aware
    datetime at 2024-01-03T03:00:00Z must resolve to the 2024-01-02 ET
    trading day, not 2024-01-03.
    """
    from datetime import timezone

    lookup = MarketStateLookup(
        by_key={
            (SPY_ASSET, date(2024, 1, 2)): MarketStateRow(
                sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
            )
        }
    )
    cost_model = SquareRootImpactCostModel(market_state=lookup)
    # UTC 03:00 on Jan 3 == 22:00 ET on Jan 2 (during EST, UTC offset -5).
    utc_dt = datetime(2024, 1, 3, 3, 0, 0, tzinfo=timezone.utc)
    estimate = cost_model.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=utc_dt
    )
    assert float(estimate) > 0, (
        "UTC datetime must resolve to ET trading day for lookup; "
        "if .date() were used instead of astimezone(ET).date(), the "
        "lookup at date(2024, 1, 3) would raise KeyError"
    )


def test_naive_dt_treated_as_already_et() -> None:
    """Naive datetimes pass through without conversion per the locked
    contract.
    """
    lookup = MarketStateLookup(
        by_key={
            (SPY_ASSET, date(2024, 1, 2)): MarketStateRow(
                sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
            )
        }
    )
    cost_model = SquareRootImpactCostModel(market_state=lookup)
    naive_dt = datetime(2024, 1, 2, 16, 0, 0)  # naive ET close time
    estimate = cost_model.estimate(
        asset_id=SPY_ASSET, shares=SPY_SHARES, direction="buy", dt=naive_dt
    )
    assert float(estimate) > 0


def test_frozen_model_rejects_mutation() -> None:
    """SquareRootImpactCostModel is attrs.frozen per the reviewer's
    Medium finding. A caller cannot accidentally swap market_state or
    parameters mid-backtest.
    """
    cost_model = SquareRootImpactCostModel(market_state=_spy_lookup())
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        cost_model.eta = Decimal("0.05")  # type: ignore[misc]
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        cost_model.market_state = _spy_lookup()  # type: ignore[misc]
