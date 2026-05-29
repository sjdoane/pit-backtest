"""Permanent-impact next-bar-mid-drops fixture (ADR 0002 M2 acceptance
criterion 5).

> "A permanent-impact fixture verifies next-bar mid-price drop"

The matcher updates the ImpactedPriceSource decorator after each fill;
subsequent BarLoop reads of the asset's price reflect the cumulative
impact. This test constructs a synthetic 2-bar scenario with a single
asset, a single fill on bar 1, and asserts that the decorator's
adjust_price on bar 2 reflects the per-share permanent impact.

Per ADR 0009 lock #9 the signed convention is:
- buy: positive signed_perm; lifts subsequent reads
- sell: negative signed_perm; lowers subsequent reads

Tests construct the matcher directly (no BarLoop) so the assertion
focuses on the matcher-decorator handshake without BarLoop noise.

Calibration-region note (per post-impl reviewer Medium finding): the
small-cap fixture's participation rate (Q/V_D = 100,000/1,000,000 = 0.1)
is at the edge of the Almgren formula's empirical calibration region
(institutional studies typically fit up to 0.05). The tests assert
SIGN only (next-bar adjusted close less than raw; signed cumulative
matches direction) not magnitude, so the extrapolation does not affect
correctness. The fixture is sized to make the next-bar mid drop
measurable at scale rather than to claim formula validity at 0.1
participation.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource, PitDataSource
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.commission import PerShareCommission
from pit_backtest.execution.cost.impact import (
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)
from pit_backtest.execution.matching import (
    MarketState,
    SquareRootImpactMatchingEngine,
)
from pit_backtest.execution.orders import FillPriceModel, Order
from pit_backtest.utils.timezones import NEW_YORK


SMALL_CAP_ASSET = AssetId(1)
SMALL_CAP_V_D = 1_000_000.0  # 10x smaller than SPY so a large sell is measurable
SMALL_CAP_THETA = 100_000_000.0
SMALL_CAP_SIGMA_D = 0.025

BAR_DATES = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))


class _StubPitDataSource(PitDataSource):
    def get_price(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def get_fundamental(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def get_corporate_actions(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def get_cash_flows(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def members_at(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def get_delisting(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError

    def get_table(self, *a, **kw):  # type: ignore[no-untyped-def, override]
        raise NotImplementedError


def _make_lookup() -> MarketStateLookup:
    by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for d in BAR_DATES:
        by_key[(SMALL_CAP_ASSET, d)] = MarketStateRow(
            sigma_D=SMALL_CAP_SIGMA_D,
            V_D=SMALL_CAP_V_D,
            Theta=SMALL_CAP_THETA,
        )
    return MarketStateLookup(by_key=by_key)


def _build_matcher() -> tuple[SquareRootImpactMatchingEngine, ImpactedPriceSource]:
    impacted = ImpactedPriceSource(raw=_StubPitDataSource())
    cost_model = SquareRootImpactCostModel(market_state=_make_lookup())
    commission = PerShareCommission(rate_per_share=Decimal("0"))
    clock = TestClock(start_dt=date(2024, 1, 1), end_dt=date(2024, 1, 10))
    matcher = SquareRootImpactMatchingEngine(
        clock=clock,
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted,
    )
    return matcher, impacted


def _make_state(d: date, close: str = "500.00") -> MarketState:
    return MarketState(
        asset_id=SMALL_CAP_ASSET,
        dt=datetime(d.year, d.month, d.day, 16, 0, tzinfo=NEW_YORK),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=int(SMALL_CAP_V_D),
    )


def _make_order(qty: str) -> Order:
    return Order(
        order_id=f"o-{qty}",
        asset_id=SMALL_CAP_ASSET,
        quantity=Decimal(qty),
        fill_price_model=FillPriceModel.CLOSE,
        submit_dt=datetime(2024, 1, 2, 16, 0, tzinfo=NEW_YORK),
    )


def test_sell_lowers_next_bar_adjusted_price() -> None:
    """Per ADR 0002 M2 criterion 5: after a SELL fill on bar 1, the
    decorator's adjust_price on bar 2's raw close returns a value LOWER
    than the raw close by the per-share permanent impact dollar amount.
    """
    matcher, impacted = _build_matcher()
    state_bar_1 = _make_state(BAR_DATES[0], close="500.00")
    matcher.on_bar_start(BAR_DATES[0])
    matcher.submit(_make_order(qty="-100000"), state_bar_1)

    # After the sell, cumulative is negative.
    cumulative_after_bar_1 = impacted.cumulative_for(SMALL_CAP_ASSET)
    assert cumulative_after_bar_1 < Decimal("0")

    # On bar 2, raw close is $502; adjusted close is $502 + cumulative
    # (which is negative), so impacted close is less than raw.
    raw_close_bar_2 = Decimal("502.00")
    adjusted_close_bar_2 = impacted.adjust_price(SMALL_CAP_ASSET, raw_close_bar_2)
    assert adjusted_close_bar_2 < raw_close_bar_2
    assert adjusted_close_bar_2 == raw_close_bar_2 + cumulative_after_bar_1


def test_buy_raises_next_bar_adjusted_price() -> None:
    """Symmetric: a BUY fill on bar 1 lifts subsequent adjust_price."""
    matcher, impacted = _build_matcher()
    state_bar_1 = _make_state(BAR_DATES[0], close="500.00")
    matcher.on_bar_start(BAR_DATES[0])
    matcher.submit(_make_order(qty="100000"), state_bar_1)

    cumulative_after_bar_1 = impacted.cumulative_for(SMALL_CAP_ASSET)
    assert cumulative_after_bar_1 > Decimal("0")

    raw_close_bar_2 = Decimal("502.00")
    adjusted_close_bar_2 = impacted.adjust_price(SMALL_CAP_ASSET, raw_close_bar_2)
    assert adjusted_close_bar_2 > raw_close_bar_2
    assert adjusted_close_bar_2 == raw_close_bar_2 + cumulative_after_bar_1


def test_buy_then_sell_cancels_to_smaller_magnitude_cumulative() -> None:
    """A buy followed by a sell at the same magnitude partially cancels.

    The two fills cannot exactly cancel because they occur at slightly
    different fill_prices (the matcher uses arrival * (1 +/- temp); after
    the buy, the impacted source raises the price the sell observes; the
    sell's permanent impact then is computed against the lifted fill_price
    and is slightly larger in magnitude than the buy's). The cumulative
    after the round-trip is therefore NOT zero; it's a small negative
    number (the sell's larger magnitude dominates).
    """
    matcher, impacted = _build_matcher()

    state_buy = _make_state(BAR_DATES[0], close="500.00")
    matcher.on_bar_start(BAR_DATES[0])
    matcher.submit(_make_order(qty="100000"), state_buy)
    cumulative_after_buy = impacted.cumulative_for(SMALL_CAP_ASSET)
    assert cumulative_after_buy > Decimal("0")

    # Bar 2: sell at the impacted price.
    raw_close_bar_2 = Decimal("500.00")
    matcher.on_bar_start(BAR_DATES[1])
    # MarketState constructed by the BarLoop would have adjusted prices;
    # here we model that by passing the impact-adjusted close as the
    # state's open/close.
    impacted_close_bar_2 = impacted.adjust_price(SMALL_CAP_ASSET, raw_close_bar_2)
    state_sell = MarketState(
        asset_id=SMALL_CAP_ASSET,
        dt=datetime(BAR_DATES[1].year, BAR_DATES[1].month, BAR_DATES[1].day, 16, 0, tzinfo=NEW_YORK),
        open=impacted_close_bar_2,
        high=impacted_close_bar_2,
        low=impacted_close_bar_2,
        close=impacted_close_bar_2,
        volume=int(SMALL_CAP_V_D),
    )
    sell_order = Order(
        order_id="o-sell",
        asset_id=SMALL_CAP_ASSET,
        quantity=Decimal("-100000"),
        fill_price_model=FillPriceModel.CLOSE,
        submit_dt=datetime(BAR_DATES[1].year, BAR_DATES[1].month, BAR_DATES[1].day, 16, 0, tzinfo=NEW_YORK),
    )
    matcher.submit(sell_order, state_sell)

    cumulative_after_round_trip = impacted.cumulative_for(SMALL_CAP_ASSET)
    # Magnitude is smaller than after just the buy (the sell partially
    # cancelled).
    assert abs(cumulative_after_round_trip) < abs(cumulative_after_buy)


def test_cumulative_resets_after_impacted_source_reset() -> None:
    """impacted_source.reset() clears the cumulative impact; subsequent
    adjust_price calls return the raw price.
    """
    matcher, impacted = _build_matcher()
    state = _make_state(BAR_DATES[0], close="500.00")
    matcher.on_bar_start(BAR_DATES[0])
    matcher.submit(_make_order(qty="-100000"), state)

    cumulative_before_reset = impacted.cumulative_for(SMALL_CAP_ASSET)
    assert cumulative_before_reset < Decimal("0")

    impacted.reset()
    cumulative_after_reset = impacted.cumulative_for(SMALL_CAP_ASSET)
    assert cumulative_after_reset == Decimal("0")

    # adjust_price now returns the raw price.
    raw_price = Decimal("550.00")
    assert impacted.adjust_price(SMALL_CAP_ASSET, raw_price) == raw_price


def test_two_assets_register_independently() -> None:
    """A fill on asset A does not affect adjust_price for asset B."""
    _matcher, impacted = _build_matcher()

    asset_2 = AssetId(2)
    # We rebuild the matcher with a lookup that covers both assets.
    by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for d in BAR_DATES:
        for aid in (SMALL_CAP_ASSET, asset_2):
            by_key[(aid, d)] = MarketStateRow(
                sigma_D=SMALL_CAP_SIGMA_D,
                V_D=SMALL_CAP_V_D,
                Theta=SMALL_CAP_THETA,
            )
    fresh_lookup = MarketStateLookup(by_key=by_key)
    fresh_cost_model = SquareRootImpactCostModel(market_state=fresh_lookup)
    matcher_fresh = SquareRootImpactMatchingEngine(
        clock=TestClock(start_dt=date(2024, 1, 1), end_dt=date(2024, 1, 10)),
        cost_model=fresh_cost_model,
        commission=PerShareCommission(rate_per_share=Decimal("0")),
        impacted_source=impacted,
    )

    state_a = _make_state(BAR_DATES[0], close="500.00")
    matcher_fresh.on_bar_start(BAR_DATES[0])
    matcher_fresh.submit(_make_order(qty="-100000"), state_a)

    # Asset A's cumulative is negative; asset B's is zero.
    assert impacted.cumulative_for(SMALL_CAP_ASSET) < Decimal("0")
    assert impacted.cumulative_for(asset_2) == Decimal("0")
    # Adjust price for B is identity.
    raw_price = Decimal("100.00")
    assert impacted.adjust_price(asset_2, raw_price) == raw_price
