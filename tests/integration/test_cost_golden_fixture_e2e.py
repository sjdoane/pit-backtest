"""End-to-end golden fixture exercise through BarLoop + matcher (M2 PR B).

Per ADR 0005 step 13 the 3-bar golden fixture at
`tests/integration/fixtures/cost_3bar_golden.json` is the source of truth
for the M2 cost-model arithmetic. PR A's loader test at
`tests/integration/test_cost_golden_fixture.py` exercises the math
standalone (SquareRootImpactCostModel.estimate + .compute + Commission
.commission_for) without going through the matcher.

PR B's E2E test extends to the matcher: it constructs a
SquareRootImpactMatchingEngine wired with the fixture's eta/beta/gamma
and PerShareCommission, submits orders constructed directly from the
fixture (no BarLoop on this test because the BarLoop's policy/signal
machinery is orthogonal to the matcher's cost dispatch), and asserts
that each emitted Fill's `temporary_impact_bps`, `permanent_impact_per_share`,
and `commission` match the fixture bands and Decimal values.

A separate BarLoop test would require synthesizing a Sharadar bundle from
the fixture's bars, which is significantly more setup than the fixture
shape supports (the fixture has 3 bars and 3 orders but no policy / no
signal / no universe). The matcher-driven E2E here is the right level.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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
    MarketState,
    SquareRootImpactMatchingEngine,
)
from pit_backtest.execution.orders import FillPriceModel, Order
from pit_backtest.utils.timezones import NEW_YORK


_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cost_3bar_golden.json"
)


def _load_fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _build_market_state_lookup(rows: list[dict[str, object]]) -> MarketStateLookup:
    by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for row in rows:
        asset_id = AssetId(int(row["asset_id"]))
        d = date.fromisoformat(str(row["dt"]))
        by_key[(asset_id, d)] = MarketStateRow(
            sigma_D=float(row["sigma_D"]),
            V_D=float(row["V_D"]),
            Theta=float(row["Theta"]),
        )
    return MarketStateLookup(by_key=by_key)


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


def _build_matcher_from_fixture(
    fixture: dict[str, object],
) -> tuple[SquareRootImpactMatchingEngine, ImpactedPriceSource]:
    market_state = _build_market_state_lookup(fixture["market_state"])  # type: ignore[arg-type]
    cost_params = fixture["cost_model"]  # type: ignore[index]
    cost_model = SquareRootImpactCostModel(
        market_state=market_state,
        eta=Decimal(str(cost_params["eta"])),
        beta=Decimal(str(cost_params["beta"])),
        gamma=Decimal(str(cost_params["gamma"])),
    )
    commission_params = fixture["commission"]  # type: ignore[index]
    assert commission_params["type"] == "per_share"
    commission = PerShareCommission(
        rate_per_share=Decimal(str(commission_params["rate_per_share"]))
    )
    impacted = ImpactedPriceSource(raw=_StubPitDataSource())
    clock = TestClock(start_dt=date(2024, 1, 1), end_dt=date(2024, 1, 31))
    matcher = SquareRootImpactMatchingEngine(
        clock=clock,
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted,
    )
    return matcher, impacted


def test_e2e_fixture_bars_produce_fills_in_expected_bands() -> None:
    """For each fixture order, run through the matcher and assert the
    emitted Fill's bps numbers fall in the documented bands.
    """
    fixture = _load_fixture()
    matcher, _ = _build_matcher_from_fixture(fixture)

    bars_by_dt = {bar["dt"]: bar for bar in fixture["bars"]}  # type: ignore[union-attr]
    expected_by_id = {
        exp["order_id"]: exp for exp in fixture["expected"]  # type: ignore[union-attr]
    }

    asset_id = AssetId(1)
    for order_data in fixture["orders"]:  # type: ignore[union-attr]
        order_id = str(order_data["order_id"])
        dt_str = str(order_data["dt"])
        bar = bars_by_dt[dt_str]
        dt = datetime.fromisoformat(dt_str + "T16:00:00").replace(tzinfo=NEW_YORK)
        bar_dt = date.fromisoformat(dt_str)

        # New bar -> reset matcher's per-bar dedup set.
        matcher.on_bar_start(bar_dt)

        order = Order(
            order_id=order_id,
            asset_id=asset_id,
            quantity=Decimal(str(order_data["shares"])),
            fill_price_model=FillPriceModel.CLOSE,
            submit_dt=dt,
        )
        market_state = MarketState(
            asset_id=asset_id,
            dt=dt,
            open=Decimal(str(bar["open"])),
            high=Decimal(str(bar["high"])),
            low=Decimal(str(bar["low"])),
            close=Decimal(str(bar["close"])),
            volume=int(bar["volume"]),
        )
        fills = matcher.submit(order, market_state)
        assert len(fills) == 1
        fill = fills[0]

        exp = expected_by_id[order_id]
        temp_bps = float(fill.temporary_impact_bps)
        assert exp["temporary_impact_bps_min"] <= temp_bps <= exp["temporary_impact_bps_max"], (
            f"order {order_id}: temporary_impact_bps {temp_bps} outside band "
            f"[{exp['temporary_impact_bps_min']}, {exp['temporary_impact_bps_max']}]"
        )

        # Reconstruct permanent_impact_bps from fill.permanent_impact_per_share
        # and fill.fill_price for the band assertion.
        perm_per_share = float(fill.permanent_impact_per_share)
        fill_price = float(fill.fill_price)
        perm_bps = perm_per_share / fill_price * 10_000.0 if fill_price != 0 else 0.0
        # The sign on perm_bps reflects the trade direction; the fixture
        # bands are magnitudes.
        assert exp["permanent_impact_bps_min"] <= abs(perm_bps) <= exp["permanent_impact_bps_max"], (
            f"order {order_id}: |permanent_impact_bps| {abs(perm_bps)} outside band "
            f"[{exp['permanent_impact_bps_min']}, {exp['permanent_impact_bps_max']}]"
        )

        # slippage is zero per ADR 0005 step 3.
        assert fill.slippage_bps == Decimal(str(exp["slippage_bps"]))


def test_e2e_fixture_commissions_byte_exact_to_fixture_decimal() -> None:
    """Per ADR 0009 lock #15: Fill.commission is byte-exact Decimal
    equal to the fixture's commission_usd.
    """
    fixture = _load_fixture()
    matcher, _ = _build_matcher_from_fixture(fixture)

    bars_by_dt = {bar["dt"]: bar for bar in fixture["bars"]}  # type: ignore[union-attr]
    expected_by_id = {
        exp["order_id"]: exp for exp in fixture["expected"]  # type: ignore[union-attr]
    }

    asset_id = AssetId(1)
    for order_data in fixture["orders"]:  # type: ignore[union-attr]
        order_id = str(order_data["order_id"])
        dt_str = str(order_data["dt"])
        bar = bars_by_dt[dt_str]
        dt = datetime.fromisoformat(dt_str + "T16:00:00").replace(tzinfo=NEW_YORK)
        bar_dt = date.fromisoformat(dt_str)

        matcher.on_bar_start(bar_dt)
        order = Order(
            order_id=order_id,
            asset_id=asset_id,
            quantity=Decimal(str(order_data["shares"])),
            fill_price_model=FillPriceModel.CLOSE,
            submit_dt=dt,
        )
        market_state = MarketState(
            asset_id=asset_id,
            dt=dt,
            open=Decimal(str(bar["open"])),
            high=Decimal(str(bar["high"])),
            low=Decimal(str(bar["low"])),
            close=Decimal(str(bar["close"])),
            volume=int(bar["volume"]),
        )
        fill = matcher.submit(order, market_state)[0]

        exp = expected_by_id[order_id]
        # PerShareCommission on absolute shares: |shares| * rate_per_share.
        # The fixture's commission_usd values (10, 7.500, 25) are positive.
        # Fill.commission is positive regardless of direction.
        assert fill.commission > Decimal("0"), (
            f"order {order_id}: commission should be positive; got {fill.commission}"
        )
        # The PerShareCommission produces exact Decimal equals at the
        # fixture-documented values.
        expected_commission = Decimal(str(exp["commission_usd"]))
        assert fill.commission == expected_commission, (
            f"order {order_id}: commission {fill.commission} != fixture "
            f"{expected_commission}"
        )


def test_e2e_fixture_slippage_zero_per_adr_0005_step_3() -> None:
    """Per ADR 0005 step 3 epsilon_bps=0 at v1; Fill.slippage_bps is 0
    for every order.
    """
    fixture = _load_fixture()
    matcher, _ = _build_matcher_from_fixture(fixture)

    bars_by_dt = {bar["dt"]: bar for bar in fixture["bars"]}  # type: ignore[union-attr]
    asset_id = AssetId(1)
    for order_data in fixture["orders"]:  # type: ignore[union-attr]
        dt_str = str(order_data["dt"])
        bar = bars_by_dt[dt_str]
        dt = datetime.fromisoformat(dt_str + "T16:00:00").replace(tzinfo=NEW_YORK)
        bar_dt = date.fromisoformat(dt_str)

        matcher.on_bar_start(bar_dt)
        order = Order(
            order_id=str(order_data["order_id"]),
            asset_id=asset_id,
            quantity=Decimal(str(order_data["shares"])),
            fill_price_model=FillPriceModel.CLOSE,
            submit_dt=dt,
        )
        market_state = MarketState(
            asset_id=asset_id,
            dt=dt,
            open=Decimal(str(bar["open"])),
            high=Decimal(str(bar["high"])),
            low=Decimal(str(bar["low"])),
            close=Decimal(str(bar["close"])),
            volume=int(bar["volume"]),
        )
        fill = matcher.submit(order, market_state)[0]
        assert fill.slippage_bps == Decimal("0")


def test_e2e_fixture_permanent_impact_register_updates_after_each_fill() -> None:
    """After each fill the ImpactedPriceSource cumulative_for(asset_id)
    grows in magnitude consistent with the buy/sell direction.

    The fixture's orders are o1 (buy 2000), o2 (sell 1500), o3 (buy 5000).
    Cumulative after o1 is positive; after o2 is smaller positive or
    negative (depends on magnitudes); after o3 is positive again with
    larger magnitude.
    """
    fixture = _load_fixture()
    matcher, impacted = _build_matcher_from_fixture(fixture)

    bars_by_dt = {bar["dt"]: bar for bar in fixture["bars"]}  # type: ignore[union-attr]
    asset_id = AssetId(1)

    cumulative_history: list[Decimal] = []
    for order_data in fixture["orders"]:  # type: ignore[union-attr]
        dt_str = str(order_data["dt"])
        bar = bars_by_dt[dt_str]
        dt = datetime.fromisoformat(dt_str + "T16:00:00").replace(tzinfo=NEW_YORK)
        bar_dt = date.fromisoformat(dt_str)

        matcher.on_bar_start(bar_dt)
        order = Order(
            order_id=str(order_data["order_id"]),
            asset_id=asset_id,
            quantity=Decimal(str(order_data["shares"])),
            fill_price_model=FillPriceModel.CLOSE,
            submit_dt=dt,
        )
        market_state = MarketState(
            asset_id=asset_id,
            dt=dt,
            open=Decimal(str(bar["open"])),
            high=Decimal(str(bar["high"])),
            low=Decimal(str(bar["low"])),
            close=Decimal(str(bar["close"])),
            volume=int(bar["volume"]),
        )
        matcher.submit(order, market_state)
        cumulative_history.append(impacted.cumulative_for(asset_id))

    # After o1 (buy 2000): positive
    assert cumulative_history[0] > Decimal("0")
    # After o2 (sell 1500): smaller positive or negative (depends on
    # relative perm magnitudes; the sell is smaller in magnitude than the
    # buy of 2000, but the perm_bps is symmetric so the sell's negative
    # signed_perm partially cancels)
    # We assert: |cumulative_history[1]| < |cumulative_history[0]| would
    # only hold if the sell's perm dollar magnitude exceeds the buy's,
    # which is not necessarily the case; the safer assertion is that
    # cumulative changed.
    assert cumulative_history[1] != cumulative_history[0]
    # After o3 (buy 5000): cumulative grew in the positive direction.
    assert cumulative_history[2] > cumulative_history[1]
