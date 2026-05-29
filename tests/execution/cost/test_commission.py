"""Commission tests (M2 PR A).

Per ADR 0005 step 10 / M2 PR A reviewer pass:
- /100 silent-rescale killer assertions on both PerShareCommission and
  BasisPointsCommission.
- Meta-tests verifying that a deliberately faulty /100 implementation
  would fall in the rejection band (proves the killer assert is not
  dead code).
- Sign-convention test: shares is signed at the Order boundary; notional
  is signed (shares * fill_price); commission is always positive.
- Byte-exact Decimal equality (not pytest.approx) for canonical inputs.
"""

from __future__ import annotations

from decimal import Decimal

import attrs
import pytest

from pit_backtest.execution.cost.commission import (
    BasisPointsCommission,
    Commission,
    PerShareCommission,
)


# ----- PerShareCommission -----


def test_per_share_commission_canonical_value() -> None:
    """1000 shares at $0.005 per share = $5.00 exactly. Byte-exact Decimal."""
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    cost = commission.commission_for(
        shares=Decimal("1000"),
        notional=Decimal("50000"),  # unused by PerShareCommission
    )
    assert cost == Decimal("5.000"), f"expected exactly 5.000, got {cost}"


def test_per_share_commission_no_silent_rescale() -> None:
    """Killer assertion: backtrader's historical bug class.

    A PerShareCommission(0.005) on 1000 shares should produce $5.00. The
    backtrader regression silently divided by 100 producing $0.05; this
    test fails loudly if anyone ever introduces that pattern.
    """
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    cost = commission.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    assert cost == Decimal("5.000")
    assert not (Decimal("0.04") <= cost <= Decimal("0.06")), (
        "PerShareCommission silently divided by 100; backtrader bug class. "
        "If this assert fires, the implementation is dividing by 100 inside "
        "commission_for."
    )


def test_per_share_commission_meta_test_faulty_impl_falls_in_band() -> None:
    """Proves the killer assert is not dead code.

    A deliberately faulty wrapper that divides by 100 must trip the
    rejection band; if the meta-test stops finding the bug class, the
    killer assert above is no longer load-bearing.
    """

    @attrs.frozen(slots=True)
    class FaultyPerShareCommission(Commission):
        rate_per_share: Decimal

        def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
            del notional
            return abs(shares) * self.rate_per_share / Decimal("100")

    faulty = FaultyPerShareCommission(rate_per_share=Decimal("0.005"))
    cost = faulty.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    assert Decimal("0.04") <= cost <= Decimal("0.06"), (
        "faulty /100 impl should fall in [0.04, 0.06]; meta-test broken"
    )


def test_per_share_commission_sign_symmetry() -> None:
    """Commission is positive regardless of trade direction.

    Per the M2 PR A reviewer pass convention: shares are signed at the
    Order boundary; commission_for uses abs() so a sell order produces
    the same dollar cost as a buy order of the same magnitude.
    """
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    buy = commission.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    sell = commission.commission_for(
        shares=Decimal("-1000"), notional=Decimal("-50000")
    )
    assert buy == sell
    assert buy > 0
    assert sell > 0


def test_per_share_commission_typo_unit_error_not_silently_rescaled() -> None:
    """Constructing PerShareCommission(rate=5) for $5/share returns $5000
    on 1000 shares; the killer assert at 0.005-rate does not interfere
    with a legitimate $5/share fee schedule.
    """
    commission = PerShareCommission(rate_per_share=Decimal("5"))
    cost = commission.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    assert cost == Decimal("5000")


# ----- BasisPointsCommission -----


def test_basis_points_commission_canonical_value() -> None:
    """5 bps on $50,000 notional = $25.00 exactly. Byte-exact Decimal."""
    commission = BasisPointsCommission(rate_bps=Decimal("5"))
    cost = commission.commission_for(
        shares=Decimal("1000"),  # unused by BasisPointsCommission
        notional=Decimal("50000"),
    )
    assert cost == Decimal("25"), f"expected exactly 25, got {cost}"


def test_basis_points_commission_no_silent_rescale() -> None:
    """Killer assertion mirroring the PerShareCommission /100 guard."""
    commission = BasisPointsCommission(rate_bps=Decimal("5"))
    cost = commission.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    assert cost == Decimal("25")
    assert not (Decimal("0.20") <= cost <= Decimal("0.30")), (
        "BasisPointsCommission silently divided by 100; backtrader bug class. "
        "If this assert fires, the implementation is dividing by 100 inside "
        "commission_for (e.g. dividing rate_bps by 100 before the bps/10000 "
        "conversion)."
    )


def test_basis_points_commission_meta_test_faulty_impl_falls_in_band() -> None:
    """Meta-test: a faulty /100 impl trips the rejection band."""

    @attrs.frozen(slots=True)
    class FaultyBasisPointsCommission(Commission):
        rate_bps: Decimal

        def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
            del shares
            return abs(notional) * self.rate_bps / Decimal("1000000")  # /10000 then /100

    faulty = FaultyBasisPointsCommission(rate_bps=Decimal("5"))
    cost = faulty.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    assert Decimal("0.20") <= cost <= Decimal("0.30"), (
        "faulty /100 impl should fall in [0.20, 0.30]; meta-test broken"
    )


def test_basis_points_commission_sign_symmetry() -> None:
    """Signed notional convention: buy and sell of same magnitude produce
    the same dollar commission.
    """
    commission = BasisPointsCommission(rate_bps=Decimal("5"))
    buy = commission.commission_for(
        shares=Decimal("1000"), notional=Decimal("50000")
    )
    sell = commission.commission_for(
        shares=Decimal("-1000"), notional=Decimal("-50000")
    )
    assert buy == sell
    assert buy > 0
    assert sell > 0


def test_basis_points_commission_zero_notional_zero_cost() -> None:
    """A trade with zero notional produces zero commission (edge case)."""
    commission = BasisPointsCommission(rate_bps=Decimal("5"))
    cost = commission.commission_for(
        shares=Decimal("0"), notional=Decimal("0")
    )
    assert cost == Decimal("0")


def test_per_share_commission_zero_shares_zero_cost() -> None:
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    cost = commission.commission_for(
        shares=Decimal("0"), notional=Decimal("0")
    )
    assert cost == Decimal("0")
