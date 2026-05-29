"""Golden-fixture test for the M2 cost-model math (M2 PR A).

Per ADR 0005 step 13: a 3-bar synthetic fixture pins the cost arithmetic
out-of-band. The JSON at `tests/integration/fixtures/cost_3bar_golden.json`
is the source of truth; this loader constructs SquareRootImpactCostModel
and PerShareCommission from the JSON, computes per-order estimate and
compute outputs, and asserts:

1. Per-order temporary_impact_bps and permanent_impact_bps fall in the
   expected bands documented in the fixture (sub-bp scale on each window
   so we band them rather than pinning a 17-digit literal).
2. estimate equals temporary_impact_bps + permanent_impact_bps to within
   1e-12 (the reviewer's H4 finding: the identity is what catches a
   swap of the two terms).
3. Commission dollars are byte-exact Decimal equals to the fixture.
4. slippage_bps is 0 per ADR 0005 step 3.

PR B's BarLoop end-to-end test will reuse this same JSON to verify the
matcher wiring produces fills consistent with the cost-model math; that
test is out of scope for PR A.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.execution.cost.base import FillState
from pit_backtest.execution.cost.commission import PerShareCommission
from pit_backtest.execution.cost.impact import (
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)


_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cost_3bar_golden.json"
)


def _load_fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _build_market_state(rows: list[dict[str, object]]) -> MarketStateLookup:
    by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for row in rows:
        asset_id = AssetId(int(row["asset_id"]))
        dt = date.fromisoformat(str(row["dt"]))
        by_key[(asset_id, dt)] = MarketStateRow(
            sigma_D=float(row["sigma_D"]),
            V_D=float(row["V_D"]),
            Theta=float(row["Theta"]),
        )
    return MarketStateLookup(by_key=by_key)


def test_fixture_file_exists() -> None:
    assert _FIXTURE_PATH.exists(), (
        f"golden fixture missing at {_FIXTURE_PATH}; check git status"
    )


def test_golden_fixture_matches_expected_bands() -> None:
    """Per-order temporary and permanent impact land in the documented
    bands. Sub-bp scale on each order makes pinning a 17-digit literal
    impractical across float64 platforms; the band check is the right
    granularity.
    """
    fixture = _load_fixture()
    market_state = _build_market_state(fixture["market_state"])  # type: ignore[arg-type]

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

    bars_by_dt = {
        bar["dt"]: bar
        for bar in fixture["bars"]  # type: ignore[union-attr]
    }
    expected_by_id = {
        exp["order_id"]: exp
        for exp in fixture["expected"]  # type: ignore[union-attr]
    }

    for order in fixture["orders"]:  # type: ignore[union-attr]
        order_id = str(order["order_id"])
        dt_str = str(order["dt"])
        bar = bars_by_dt[dt_str]
        dt = datetime.fromisoformat(dt_str + "T16:00:00")
        asset_id = AssetId(1)
        shares = Decimal(str(order["shares"]))

        # Sanity: notional_usd is the policy-intended target notional;
        # actual fill notional = abs(shares) * bar.open may differ by
        # a small percentage due to intraday bar moves. Allow 2% drift.
        notional = abs(shares) * Decimal(str(bar["open"]))
        expected_notional = abs(Decimal(str(order["notional_usd"])))
        drift_pct = abs(notional - expected_notional) / expected_notional
        assert drift_pct < Decimal("0.02"), (
            f"order {order_id}: |shares|*open notional {notional} drifts "
            f"{drift_pct*100:.2f}% from declared {expected_notional}; "
            f"expected <2% intraday drift"
        )

        fill_state = FillState(
            asset_id=asset_id,
            dt=dt,
            shares=shares,
            direction=str(order["direction"]),  # type: ignore[arg-type]
            bar_open=Decimal(str(bar["open"])),
            bar_close=Decimal(str(bar["close"])),
            bar_volume=int(bar["volume"]),
        )
        estimate = cost_model.estimate(
            asset_id=asset_id,
            shares=shares,
            direction=str(order["direction"]),  # type: ignore[arg-type]
            dt=dt,
        )
        breakdown = cost_model.compute(fill_state)
        comm = commission.commission_for(
            shares=shares, notional=shares * Decimal(str(bar["open"]))
        )

        exp = expected_by_id[order_id]
        # Band checks (sub-bp; widened to handle float64 round-trip).
        perm_bps = float(breakdown.permanent_impact_bps)
        temp_bps = float(breakdown.temporary_impact_bps)
        assert exp["permanent_impact_bps_min"] <= perm_bps <= exp["permanent_impact_bps_max"], (
            f"order {order_id}: permanent_impact_bps {perm_bps} outside band "
            f"[{exp['permanent_impact_bps_min']}, {exp['permanent_impact_bps_max']}]"
        )
        assert exp["temporary_impact_bps_min"] <= temp_bps <= exp["temporary_impact_bps_max"], (
            f"order {order_id}: temporary_impact_bps {temp_bps} outside band "
            f"[{exp['temporary_impact_bps_min']}, {exp['temporary_impact_bps_max']}]"
        )

        # Reviewer's H4: identity check. estimate == temporary + permanent
        # (slippage is 0 per ADR 0005 step 3; commission is 0 in compute).
        identity_diff = abs(
            float(estimate)
            - (
                float(breakdown.temporary_impact_bps)
                + float(breakdown.permanent_impact_bps)
                + float(breakdown.slippage_bps)
            )
        )
        assert identity_diff < 1e-12, (
            f"order {order_id}: estimate vs compute identity broken; "
            f"estimate={estimate}, sum={breakdown.temporary_impact_bps + breakdown.permanent_impact_bps}, "
            f"diff={identity_diff}"
        )

        # Byte-exact slippage and commission.
        assert breakdown.slippage_bps == Decimal(str(exp["slippage_bps"]))
        assert breakdown.commission == Decimal("0"), (
            f"order {order_id}: cost-model compute returns commission=0; "
            f"the matcher (PR B) wires the Commission instance in. "
            f"got {breakdown.commission}"
        )
        # Standalone Commission call.
        assert comm == Decimal(str(exp["commission_usd"])), (
            f"order {order_id}: commission {comm} not equal to fixture "
            f"{exp['commission_usd']}"
        )


def test_golden_fixture_signed_notional_convention() -> None:
    """Reviewer's H2: signed notional convention.

    Order o2 has shares=-1500 (sell) and notional_usd=-750000. The
    PerShareCommission ignores notional, but the magnitude check should
    still produce a positive commission of $7.500.
    """
    fixture = _load_fixture()
    sell_order = next(
        o for o in fixture["orders"] if o["order_id"] == "o2"  # type: ignore[union-attr]
    )
    assert Decimal(str(sell_order["shares"])) < Decimal("0")
    assert Decimal(str(sell_order["notional_usd"])) < Decimal("0")

    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    comm = commission.commission_for(
        shares=Decimal(str(sell_order["shares"])),
        notional=Decimal(str(sell_order["notional_usd"])),
    )
    assert comm == Decimal("7.500"), (
        f"signed-input commission should be positive 7.500; got {comm}"
    )
