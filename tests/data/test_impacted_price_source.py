"""ImpactedPriceSource unit tests (M2 PR B).

Per ADR 0009 lock #1 the decorator is a standalone class with the
__slots__ + four-method surface (apply_permanent_impact, adjust_price,
cumulative_for, reset). It does NOT inherit from PitDataSource at M2.
Per ADR 0009 lock #11 it carries trust boundary item 12 in the
determinism doc; signal/policy modules MUST NOT import it.

These tests construct the decorator against a minimal fake PitDataSource
(only the methods used by the decorator's __init__ contract are needed;
adjust_price never consults the wrapped source).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

import polars as pl
import pytest

from pit_backtest.data.records import AssetId, CashFlow, CorporateAction
from pit_backtest.data.sources.base import (
    FundamentalFlavor,
    ImpactedPriceSource,
    PitDataSource,
    PriceField,
)


class _StubPitDataSource(PitDataSource):
    """Minimal PitDataSource that raises on every protocol method.

    The decorator never calls back into the wrapped source at M2
    (adjust_price is offset-only; get_price is M3 work). This stub
    exists so the ImpactedPriceSource construction takes a Protocol-
    conforming argument without forcing the test to set up a full
    SharadarDataSource bundle.
    """

    def get_price(
        self, asset_id: AssetId, dt: datetime, field: PriceField
    ) -> Decimal:
        raise NotImplementedError("M2 decorator does not consult the raw source")

    def get_fundamental(
        self,
        asset_id: AssetId,
        available_dt: datetime,
        field: str,
        flavor: FundamentalFlavor,
    ) -> Decimal | None:
        raise NotImplementedError

    def get_corporate_actions(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CorporateAction]:
        raise NotImplementedError

    def get_cash_flows(
        self, asset_id: AssetId, start_dt: datetime, end_dt: datetime
    ) -> list[CashFlow]:
        raise NotImplementedError

    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        raise NotImplementedError

    def get_delisting(
        self, asset_id: AssetId
    ) -> CashFlow | CorporateAction | None:
        raise NotImplementedError

    def get_table(self, table_name: str) -> pl.LazyFrame:
        raise NotImplementedError


def _new_decorator() -> ImpactedPriceSource:
    return ImpactedPriceSource(raw=_StubPitDataSource())


def test_impacted_source_wraps_raw_data_source() -> None:
    """The decorator construction takes a PitDataSource Protocol-
    conforming argument. The wrapped source is stored but never
    consulted by the M2 surface.
    """
    raw = _StubPitDataSource()
    decorator = ImpactedPriceSource(raw=raw)
    # The decorator does not expose the raw source publicly, but the
    # _raw slot is the only way the M3 path would access it. The
    # construction succeeds; that is the contract.
    assert isinstance(decorator, ImpactedPriceSource)


def test_adjust_price_returns_raw_on_never_impacted_asset() -> None:
    """Before any apply_permanent_impact call, adjust_price is identity."""
    decorator = _new_decorator()
    asset_id = AssetId(1)
    raw_price = Decimal("500.00")
    assert decorator.adjust_price(asset_id, raw_price) == raw_price


def test_apply_permanent_impact_accumulates_signed() -> None:
    """Two applies in the same direction accumulate; opposite directions
    partially cancel.
    """
    decorator = _new_decorator()
    asset_id = AssetId(1)
    decorator.apply_permanent_impact(asset_id, Decimal("0.05"))
    assert decorator.cumulative_for(asset_id) == Decimal("0.05")
    decorator.apply_permanent_impact(asset_id, Decimal("0.03"))
    assert decorator.cumulative_for(asset_id) == Decimal("0.08")
    decorator.apply_permanent_impact(asset_id, Decimal("-0.10"))
    assert decorator.cumulative_for(asset_id) == Decimal("-0.02")


def test_adjust_price_reflects_cumulative_after_multiple_applies() -> None:
    """adjust_price returns raw_price + cumulative_for(asset_id) at the
    time of the call.
    """
    decorator = _new_decorator()
    asset_id = AssetId(1)
    raw_price = Decimal("500.00")

    decorator.apply_permanent_impact(asset_id, Decimal("0.10"))
    assert decorator.adjust_price(asset_id, raw_price) == Decimal("500.10")

    decorator.apply_permanent_impact(asset_id, Decimal("0.05"))
    assert decorator.adjust_price(asset_id, raw_price) == Decimal("500.15")


def test_reset_zeros_the_register() -> None:
    """reset() clears the cumulative impact for all assets."""
    decorator = _new_decorator()
    decorator.apply_permanent_impact(AssetId(1), Decimal("0.05"))
    decorator.apply_permanent_impact(AssetId(2), Decimal("-0.03"))
    assert decorator.cumulative_for(AssetId(1)) == Decimal("0.05")
    assert decorator.cumulative_for(AssetId(2)) == Decimal("-0.03")

    decorator.reset()
    assert decorator.cumulative_for(AssetId(1)) == Decimal("0")
    assert decorator.cumulative_for(AssetId(2)) == Decimal("0")


def test_volume_reads_bypass_adjustment() -> None:
    """Volume is bypassed by contract per ADR 0005 step 5: the decorator
    is never asked to adjust a volume value. The BarLoop and matcher
    route volume directly from the raw source. This test asserts the
    contract by demonstrating that the decorator's only price-adjusting
    method is adjust_price (volume is an int, not a Decimal, and the
    Python type system would reject a volume passed to adjust_price).
    """
    decorator = _new_decorator()
    # The decorator exposes adjust_price only for Decimal-typed prices;
    # there is no adjust_volume method. The contract is enforced by the
    # absence of an API surface.
    assert hasattr(decorator, "adjust_price")
    assert not hasattr(decorator, "adjust_volume")


def test_missing_asset_in_register_returns_raw_unchanged() -> None:
    """An asset never seen by apply_permanent_impact has cumulative 0
    and adjust_price is identity for that asset.
    """
    decorator = _new_decorator()
    decorator.apply_permanent_impact(AssetId(1), Decimal("0.10"))
    raw_price = Decimal("200.00")
    # AssetId(2) has not been impacted.
    assert decorator.cumulative_for(AssetId(2)) == Decimal("0")
    assert decorator.adjust_price(AssetId(2), raw_price) == raw_price


def test_buy_per_share_signed_lifts_price() -> None:
    """A positive per_share_signed lifts subsequent adjust_price reads."""
    decorator = _new_decorator()
    asset_id = AssetId(1)
    raw_price = Decimal("100.00")
    decorator.apply_permanent_impact(asset_id, Decimal("0.20"))
    assert decorator.adjust_price(asset_id, raw_price) > raw_price


def test_sell_per_share_signed_lowers_price() -> None:
    """A negative per_share_signed lowers subsequent adjust_price reads."""
    decorator = _new_decorator()
    asset_id = AssetId(1)
    raw_price = Decimal("100.00")
    decorator.apply_permanent_impact(asset_id, Decimal("-0.15"))
    assert decorator.adjust_price(asset_id, raw_price) < raw_price


def test_zero_impact_is_noop_and_does_not_allocate_register_entry() -> None:
    """apply_permanent_impact with zero per-share does not allocate a
    register entry. This is the documented contract per the decorator's
    docstring: never-traded and round-tripped-to-zero assets are
    indistinguishable via cumulative_for, but the underlying register
    distinguishes them.
    """
    decorator = _new_decorator()
    asset_id = AssetId(1)
    decorator.apply_permanent_impact(asset_id, Decimal("0"))
    assert decorator.cumulative_for(asset_id) == Decimal("0")
    # Register is NOT allocated for the never-seen asset.
    assert asset_id not in decorator._cumulative_per_share
    # adjust_price still works as identity.
    raw_price = Decimal("50.00")
    assert decorator.adjust_price(asset_id, raw_price) == raw_price


def test_buy_then_offset_sell_leaves_zero_cumulative_with_register_entry() -> None:
    """A buy followed by an exact-magnitude sell leaves cumulative=0 BUT
    the register entry IS allocated. This locks the documented contract
    that "never-traded" and "round-tripped-to-zero" are observably
    distinguishable via the register membership.
    """
    decorator = _new_decorator()
    asset_id = AssetId(1)
    decorator.apply_permanent_impact(asset_id, Decimal("0.05"))
    decorator.apply_permanent_impact(asset_id, Decimal("-0.05"))
    assert decorator.cumulative_for(asset_id) == Decimal("0")
    # Register IS allocated for the round-tripped asset.
    assert asset_id in decorator._cumulative_per_share
    assert decorator._cumulative_per_share[asset_id] == Decimal("0")


def test_impacted_source_does_not_inherit_from_pit_data_source() -> None:
    """Per ADR 0009 lock #1 the decorator is a standalone class at M2.
    The get_price per-row path is M3 deliverable; the decorator MUST NOT
    claim to satisfy the PitDataSource Protocol at M2 because that would
    expose a NotImplementedError-raising method via the Protocol surface.
    """
    decorator = _new_decorator()
    # mypy + isinstance check: the decorator should NOT be a PitDataSource.
    # PitDataSource is a runtime Protocol; isinstance returns True iff the
    # class structurally satisfies the methods. ImpactedPriceSource does
    # not implement get_price/get_fundamental/etc. so isinstance returns
    # False. This locks the M2 surface decision.
    # We do not test isinstance directly because PitDataSource is a
    # structural Protocol without runtime_checkable; we test the contract:
    # the four-method surface is the only public API.
    public_methods = {
        name for name in dir(decorator)
        if not name.startswith("_") and callable(getattr(decorator, name))
    }
    assert public_methods == {
        "apply_permanent_impact", "adjust_price", "cumulative_for", "reset"
    }


def test_decorator_uses_slots() -> None:
    """__slots__ locks the attribute shape against typos that would
    silently create a new attribute on a plain class (e.g.,
    decorator.cumulatives_per_share = ... would not raise AttributeError
    without __slots__).
    """
    decorator = _new_decorator()
    with pytest.raises(AttributeError):
        decorator.some_typo = "x"  # type: ignore[attr-defined]


def test_buy_impact_register_persists_across_adjust_price_calls() -> None:
    """The register accumulates across calls without leaking state to
    other assets.
    """
    decorator = _new_decorator()
    decorator.apply_permanent_impact(AssetId(1), Decimal("0.10"))
    decorator.apply_permanent_impact(AssetId(2), Decimal("-0.20"))

    raw = Decimal("100.00")
    assert decorator.adjust_price(AssetId(1), raw) == Decimal("100.10")
    assert decorator.adjust_price(AssetId(2), raw) == Decimal("99.80")
    # Re-read does not change state.
    assert decorator.adjust_price(AssetId(1), raw) == Decimal("100.10")
    assert decorator.adjust_price(AssetId(2), raw) == Decimal("99.80")
