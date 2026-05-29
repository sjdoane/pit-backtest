"""CloseFillMatchingEngine tests.

Per the M1 day 3 reviewer pass, this is a real engine class (not a
private BarLoop helper) so it gets its own tests against the Order/Fill
contract.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import (
    CloseFillMatchingEngine,
    MarketState,
    UnsupportedFillPriceModelError,
)
from pit_backtest.execution.orders import FillPriceModel, Order
from pit_backtest.utils.timezones import NEW_YORK


def _make_clock() -> TestClock:
    return TestClock(start_dt=date(2024, 3, 1), end_dt=date(2024, 3, 31))


def _make_market_state(close: str = "500.00") -> MarketState:
    return MarketState(
        asset_id=AssetId(0),
        dt=datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK),
        open=Decimal("499.00"),
        high=Decimal("501.00"),
        low=Decimal("498.00"),
        close=Decimal(close),
        volume=1_000_000,
    )


def _make_order(qty: str = "100", model: FillPriceModel = FillPriceModel.CLOSE) -> Order:
    return Order(
        order_id="o-001",
        asset_id=AssetId(0),
        quantity=Decimal(qty),
        fill_price_model=model,
        submit_dt=datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK),
    )


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
        dt=datetime(2024, 3, 15, 16, 0, tzinfo=NEW_YORK),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=1000,
    )
    with pytest.raises(ValueError, match="asset_id"):
        matcher.submit(order, state)
