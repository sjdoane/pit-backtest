"""CloseFillMatchingEngine + SquareRootImpactMatchingEngine tests.

CloseFillMatchingEngine (M1): per the M1 day 3 reviewer pass this is a
real engine class (not a private BarLoop helper) so it gets its own
tests against the Order/Fill contract. Per ADR 0009 lock #6 it now also
implements a no-op on_bar_start.

SquareRootImpactMatchingEngine (M2 PR B): per ADR 0009 lock #4 supports
OPEN/CLOSE/ARRIVAL; raises UnsupportedFillPriceModelError on VWAP
(v1.1 intraday) and NEXT_BAR_OPEN (M3 deferred fill). Numeric pin tests
per ADR 0009 lock #15 assert the sign convention against specific
Decimal values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

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
    CloseFillMatchingEngine,
    MarketState,
    MatchingError,
    MultipleFillsPerBarError,
    SquareRootImpactMatchingEngine,
    UnsupportedFillPriceModelError,
)
from pit_backtest.execution.orders import FillPriceModel, Order
from pit_backtest.utils.timezones import NEW_YORK


SPY_ASSET = AssetId(0)
SPY_DT_DATE = date(2024, 3, 15)
SPY_DT = datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK)


def _make_clock() -> TestClock:
    return TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))


def _make_market_state(close: str = "500.00") -> MarketState:
    return MarketState(
        asset_id=SPY_ASSET,
        dt=SPY_DT,
        open=Decimal("499.00"),
        high=Decimal("501.00"),
        low=Decimal("498.00"),
        close=Decimal(close),
        volume=1_000_000,
    )


def _make_order(qty: str = "100", model: FillPriceModel = FillPriceModel.CLOSE) -> Order:
    return Order(
        order_id="o-001",
        asset_id=SPY_ASSET,
        quantity=Decimal(qty),
        fill_price_model=model,
        submit_dt=SPY_DT,
    )


# ----- CloseFillMatchingEngine (M1) -----


def test_close_fill_returns_single_fill_at_close() -> None:
    matcher = CloseFillMatchingEngine(clock=_make_clock())
    fills = matcher.submit(_make_order(), _make_market_state(close="500.00"))
    assert len(fills) == 1
    fill = fills[0]
    assert fill.fill_price == Decimal("500.00")
    assert fill.quantity == Decimal("100")
    assert fill.commission == Decimal("0")
    assert fill.slippage_bps == Decimal("0")
    assert fill.temporary_impact_bps == Decimal("0")
    assert fill.permanent_impact_per_share == Decimal("0")


def test_close_fill_negative_quantity_for_sells() -> None:
    matcher = CloseFillMatchingEngine(clock=_make_clock())
    fills = matcher.submit(
        _make_order(qty="-50"), _make_market_state(close="500.00")
    )
    assert fills[0].quantity == Decimal("-50")


def test_close_fill_rejects_non_close_models() -> None:
    matcher = CloseFillMatchingEngine(clock=_make_clock())
    for model in (
        FillPriceModel.OPEN,
        FillPriceModel.VWAP,
        FillPriceModel.ARRIVAL,
        FillPriceModel.NEXT_BAR_OPEN,
    ):
        with pytest.raises(UnsupportedFillPriceModelError, match="CLOSE"):
            matcher.submit(_make_order(model=model), _make_market_state())


def test_close_fill_asset_id_mismatch_raises() -> None:
    matcher = CloseFillMatchingEngine(clock=_make_clock())
    order = _make_order()  # asset_id=0
    state = MarketState(
        asset_id=AssetId(1),  # mismatched
        dt=SPY_DT,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=1000,
    )
    with pytest.raises(ValueError, match="asset_id"):
        matcher.submit(order, state)


def test_close_fill_on_bar_start_is_noop() -> None:
    """Per ADR 0009 lock #6 the M1 matcher implements on_bar_start as a
    no-op so it satisfies the extended MatchingEngine Protocol.
    """
    matcher = CloseFillMatchingEngine(clock=_make_clock())
    # Multiple calls do not raise; the BarLoop calls this unconditionally.
    matcher.on_bar_start(SPY_DT_DATE)
    matcher.on_bar_start(date(2024, 3, 16))
    # And a submit still works after on_bar_start.
    fills = matcher.submit(_make_order(), _make_market_state(close="500.00"))
    assert len(fills) == 1


# ----- SquareRootImpactMatchingEngine (M2 PR B) shared fixtures -----


# SPY-shaped MarketStateLookup row matching tests/execution/cost/test_impact.py.
SPY_SIGMA_D = 0.012
SPY_V_D = 80_000_000.0
SPY_THETA = 8_700_000_000.0


def _spy_market_state_lookup() -> MarketStateLookup:
    return MarketStateLookup(
        by_key={
            (SPY_ASSET, SPY_DT_DATE): MarketStateRow(
                sigma_D=SPY_SIGMA_D, V_D=SPY_V_D, Theta=SPY_THETA
            )
        }
    )


class _StubPitDataSource(PitDataSource):
    """Minimal source for the ImpactedPriceSource decorator."""

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


def _build_m2_matcher(
    eta: Decimal = Decimal("0.142"),
    rate_per_share: Decimal = Decimal("0.005"),
) -> tuple[SquareRootImpactMatchingEngine, ImpactedPriceSource]:
    impacted = ImpactedPriceSource(raw=_StubPitDataSource())
    cost_model = SquareRootImpactCostModel(
        market_state=_spy_market_state_lookup(), eta=eta
    )
    commission = PerShareCommission(rate_per_share=rate_per_share)
    matcher = SquareRootImpactMatchingEngine(
        clock=_make_clock(),
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted,
    )
    return matcher, impacted


def _make_m2_state(
    close: str = "500.00",
    open_: str = "500.00",
    high: str = "501.00",
    low: str = "499.00",
    prior_close: str | None = None,
) -> MarketState:
    pc = Decimal(prior_close) if prior_close is not None else None
    return MarketState(
        asset_id=SPY_ASSET,
        dt=SPY_DT,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=80_000_000,
        prior_close=pc,
    )


def _make_m2_order(
    qty: str = "2000",  # $1M / $500 = 2000 shares per ADR 0007 fixture
    model: FillPriceModel = FillPriceModel.CLOSE,
) -> Order:
    return Order(
        order_id="o-001",
        asset_id=SPY_ASSET,
        quantity=Decimal(qty),
        fill_price_model=model,
        submit_dt=SPY_DT,
    )


# ----- SquareRootImpactMatchingEngine: fill-price model coverage -----


def test_square_root_matcher_open_fill_uses_bar_open() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(open_="490.00", close="510.00")
    fills = matcher.submit(_make_m2_order(model=FillPriceModel.OPEN), state)
    assert len(fills) == 1
    # Fill price = arrival * (1 + signed_temp_fraction). Arrival = open = 490.
    # For buy 2000 shares at the SPY fixture, temp_bps ~ 0.0296 bps so the
    # fill_price is essentially equal to arrival within 0.0001%.
    assert float(fills[0].fill_price) == pytest.approx(490.00 * 1.00000003, rel=1e-5)


def test_square_root_matcher_close_fill_uses_bar_close() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(open_="490.00", close="510.00")
    fills = matcher.submit(_make_m2_order(model=FillPriceModel.CLOSE), state)
    # Arrival = close = 510.
    assert float(fills[0].fill_price) == pytest.approx(510.00 * 1.00000003, rel=1e-5)


def test_square_root_matcher_arrival_fill_uses_prior_close() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(
        open_="510.00", close="520.00", prior_close="500.00"
    )
    fills = matcher.submit(_make_m2_order(model=FillPriceModel.ARRIVAL), state)
    # Arrival = prior_close = 500.
    assert float(fills[0].fill_price) == pytest.approx(500.00 * 1.00000003, rel=1e-5)


def test_square_root_matcher_arrival_without_prior_close_raises() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(prior_close=None)
    with pytest.raises(UnsupportedFillPriceModelError, match="prior_close"):
        matcher.submit(_make_m2_order(model=FillPriceModel.ARRIVAL), state)


def test_square_root_matcher_rejects_vwap() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    with pytest.raises(UnsupportedFillPriceModelError, match="VWAP"):
        matcher.submit(_make_m2_order(model=FillPriceModel.VWAP), state)


def test_square_root_matcher_rejects_next_bar_open_m3_deliverable() -> None:
    """Per ADR 0009 lock #4 NEXT_BAR_OPEN is M3 deliverable; the matcher
    raises a typed error rather than silently peeking next bar's open.
    """
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    with pytest.raises(UnsupportedFillPriceModelError, match="NEXT_BAR_OPEN"):
        matcher.submit(
            _make_m2_order(model=FillPriceModel.NEXT_BAR_OPEN), state
        )


# ----- SquareRootImpactMatchingEngine: one-fill-per-(asset, dt) -----


def test_square_root_matcher_one_fill_per_asset_dt_raises_on_second_submit() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    order = _make_m2_order()
    fills_1 = matcher.submit(order, state)
    assert len(fills_1) == 1
    # Second submit for same (asset_id, _et_date(dt)) raises.
    with pytest.raises(MultipleFillsPerBarError, match=str(SPY_ASSET)):
        matcher.submit(order, state)


def test_square_root_matcher_on_bar_start_resets_constraint() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    order = _make_m2_order()
    matcher.submit(order, state)
    matcher.on_bar_start(SPY_DT_DATE)
    # After the bar-start hook, the dedup set is cleared and a fresh
    # submit succeeds. (In practice the BarLoop would call this with a
    # NEW bar_dt; clearing always-on is the M2 behavior.)
    fills_2 = matcher.submit(order, state)
    assert len(fills_2) == 1


def test_multiple_fills_per_bar_error_inherits_matching_error() -> None:
    """Per ADR 0009 lock #11 the exception hierarchy is shallow under
    MatchingError so callers can catch the base for cross-cutting handling.
    """
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    order = _make_m2_order()
    matcher.submit(order, state)
    with pytest.raises(MatchingError):
        matcher.submit(order, state)


def test_unsupported_fill_price_model_error_inherits_matching_error() -> None:
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state()
    with pytest.raises(MatchingError):
        matcher.submit(_make_m2_order(model=FillPriceModel.VWAP), state)


# ----- SquareRootImpactMatchingEngine: sign convention (numeric pins) -----


def test_square_root_matcher_buy_lifts_fill_price_above_arrival() -> None:
    """Per ADR 0009 lock #9 a buy fills above arrival. The temp_bps for
    SPY $1M monthly at default Almgren is ~0.0296 bps; arrival $500
    yields fill_price ~ 500.0000148. Numeric pin to 1e-9 relative.
    """
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(open_="500.00", close="500.00")
    fills = matcher.submit(_make_m2_order(qty="2000", model=FillPriceModel.OPEN), state)
    fill_price = float(fills[0].fill_price)
    arrival = 500.0
    assert fill_price > arrival
    # Hand-computed temp_bps for default Almgren at SPY shape: ~0.029568 bps.
    # signed_temp_fraction = +0.029568e-4; fill_price = 500 * (1 + 0.029568e-4)
    # = 500.001478 (approx).
    expected_fill_price = 500.0 * (
        1.0 + 0.142 * 0.012 * (2000.0 / 80_000_000.0) ** 0.6
    )
    assert fill_price == pytest.approx(expected_fill_price, rel=1e-9)


def test_square_root_matcher_sell_lowers_fill_price_below_arrival() -> None:
    """Symmetric of the buy test: a sell fills below arrival by the
    same fractional magnitude.
    """
    matcher, _ = _build_m2_matcher()
    state = _make_m2_state(open_="500.00", close="500.00")
    fills = matcher.submit(
        _make_m2_order(qty="-2000", model=FillPriceModel.OPEN), state
    )
    fill_price = float(fills[0].fill_price)
    arrival = 500.0
    assert fill_price < arrival
    # Hand-computed: arrival * (1 - temp_fraction) per the sign convention.
    expected_fill_price = 500.0 * (
        1.0 - 0.142 * 0.012 * (2000.0 / 80_000_000.0) ** 0.6
    )
    assert fill_price == pytest.approx(expected_fill_price, rel=1e-9)


def test_buy_and_sell_cash_flows_are_symmetric_about_arrival_notional() -> None:
    """Per ADR 0009 lock #15: a buy outflow + a sell inflow at identical
    |qty| equals 2 * arrival * |qty| * temp_bps / 10_000 within float64
    noise. The round-trip cost identity locks the sign convention.
    """
    # Buy fill
    matcher_buy, _ = _build_m2_matcher()
    state_buy = _make_m2_state(open_="500.00", close="500.00")
    fill_buy = matcher_buy.submit(
        _make_m2_order(qty="2000", model=FillPriceModel.OPEN), state_buy
    )[0]
    buy_notional = float(fill_buy.quantity) * float(fill_buy.fill_price)  # positive

    # Sell fill at identical |qty|
    matcher_sell, _ = _build_m2_matcher()
    state_sell = _make_m2_state(open_="500.00", close="500.00")
    fill_sell = matcher_sell.submit(
        _make_m2_order(qty="-2000", model=FillPriceModel.OPEN), state_sell
    )[0]
    sell_notional = float(fill_sell.quantity) * float(fill_sell.fill_price)  # negative

    # Round-trip: buyer pays |qty| * fill_buy; seller receives |qty| * fill_sell.
    # Net trader cost = buy_notional + sell_notional (sell_notional is negative).
    # The net cost equals 2 * arrival_avg * |qty| * temp_bps / 10000.
    net_cost = buy_notional + sell_notional
    arrival = 500.0
    abs_qty = 2000.0
    temp_bps = 0.142 * 0.012 * (2000.0 / 80_000_000.0) ** 0.6 * 10_000.0
    expected_net = 2.0 * arrival * abs_qty * temp_bps / 10_000.0
    assert net_cost == pytest.approx(expected_net, rel=1e-9)


def test_square_root_matcher_applies_permanent_impact_to_decorator() -> None:
    """After a fill, the decorator's cumulative_for must be non-zero for
    the traded asset. Buy = positive cumulative; sell = negative.
    """
    matcher, impacted = _build_m2_matcher()
    state = _make_m2_state(open_="500.00", close="500.00")
    matcher.submit(
        _make_m2_order(qty="2000", model=FillPriceModel.OPEN), state
    )
    assert impacted.cumulative_for(SPY_ASSET) > Decimal("0")

    # Second matcher for symmetry: sell creates negative cumulative.
    matcher_sell, impacted_sell = _build_m2_matcher()
    state_sell = _make_m2_state(open_="500.00", close="500.00")
    matcher_sell.submit(
        _make_m2_order(qty="-2000", model=FillPriceModel.OPEN), state_sell
    )
    assert impacted_sell.cumulative_for(SPY_ASSET) < Decimal("0")


def test_square_root_matcher_includes_commission_in_fill() -> None:
    """Fill.commission equals abs(shares) * rate_per_share for
    PerShareCommission per the signed-notional convention.
    """
    matcher, _ = _build_m2_matcher(rate_per_share=Decimal("0.005"))
    state = _make_m2_state()
    fills = matcher.submit(_make_m2_order(qty="2000"), state)
    # 2000 shares * $0.005/share = $10.
    assert fills[0].commission == Decimal("10.000")

    # Sell of same magnitude: still positive $10.
    matcher_sell, _ = _build_m2_matcher(rate_per_share=Decimal("0.005"))
    state_sell = _make_m2_state()
    fills_sell = matcher_sell.submit(_make_m2_order(qty="-2000"), state_sell)
    assert fills_sell[0].commission == Decimal("10.000")


def test_square_root_matcher_asset_id_mismatch_raises() -> None:
    matcher, _ = _build_m2_matcher()
    state = MarketState(
        asset_id=AssetId(99),
        dt=SPY_DT,
        open=Decimal("500"),
        high=Decimal("501"),
        low=Decimal("499"),
        close=Decimal("500"),
        volume=80_000_000,
    )
    with pytest.raises(ValueError, match="asset_id"):
        matcher.submit(_make_m2_order(), state)


def test_square_root_matcher_signed_perm_uses_fill_price_not_arrival() -> None:
    """Per ADR 0009 lock #9 + reviewer Medium #7: signed_perm derives
    from fill_price not arrival. For OPEN/CLOSE fills arrival equals
    open/close so this is a wash; for ARRIVAL fills with prior_close
    significantly different from current bar, the difference is
    observable.

    Test: a large prior_close-vs-open gap with ARRIVAL fill model.
    permanent_impact_per_share = perm_bps/10000 * fill_price * sign,
    NOT perm_bps/10000 * arrival * sign.
    """
    matcher, impacted = _build_m2_matcher()
    # prior_close $400; bar open $500 (big gap-up); buy 2000 shares
    state = _make_m2_state(
        open_="500.00", close="510.00", prior_close="400.00"
    )
    fills = matcher.submit(
        _make_m2_order(qty="2000", model=FillPriceModel.ARRIVAL), state
    )
    fill = fills[0]
    # fill_price = arrival * (1 + temp) = 400 * (1 + epsilon).
    # If signed_perm used arrival: signed_perm = perm_bps/10000 * 400 * 1
    # If signed_perm used fill_price: signed_perm ~ perm_bps/10000 * 400.001 * 1
    # The difference is small but the CONTRACT is fill_price.
    # We test: cumulative == perm_bps/10000 * fill_price (positive).
    cumulative = impacted.cumulative_for(SPY_ASSET)
    perm_bps_decimal = fill.permanent_impact_per_share
    # cumulative equals the per-share signed perm registered.
    assert cumulative == perm_bps_decimal
    # And the cumulative magnitude reflects fill_price not arrival.
    perm_bps_value = (
        0.5 * 0.314 * 0.012 * (2000.0 / 80_000_000.0)
        * (8_700_000_000.0 / 80_000_000.0) ** 0.25 * 10_000.0
    )
    expected_perm_per_share = perm_bps_value / 10_000.0 * float(fill.fill_price)
    assert float(cumulative) == pytest.approx(expected_perm_per_share, rel=1e-9)


def test_square_root_matcher_zero_shares_fills_at_arrival_no_register_update() -> None:
    """A zero-quantity order fills at arrival with zero everything; the
    permanent-impact register is not updated (zero is a no-op per
    ImpactedPriceSource.apply_permanent_impact).
    """
    matcher, impacted = _build_m2_matcher()
    state = _make_m2_state()
    fills = matcher.submit(_make_m2_order(qty="0"), state)
    fill = fills[0]
    assert fill.quantity == Decimal("0")
    # fill_price = arrival * (1 + 0) = arrival = close = 500.
    assert float(fill.fill_price) == pytest.approx(500.0, rel=1e-12)
    assert fill.commission == Decimal("0")
    assert fill.permanent_impact_per_share == Decimal("0")
    assert impacted.cumulative_for(SPY_ASSET) == Decimal("0")
