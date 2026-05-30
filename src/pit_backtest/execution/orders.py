"""Order and Fill record types; FillPriceModel enum.

Per ADR 0001 decision 7 and ADR 0003 architecture: every Order requires
an explicit FillPriceModel (no default); MOO and MOC are special cases of
OPEN and CLOSE with extra slippage applied by the cost layer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

import attrs

from pit_backtest.data.records import AssetId


class FillPriceModel(Enum):
    """Per ADR 0001 decision 7: every Order requires a FillPriceModel.

    No default. Forgetting to specify is a TypeError at Order construction,
    not a silent default that researchers discover months later.
    """

    OPEN = "open"
    CLOSE = "close"
    VWAP = "vwap"
    ARRIVAL = "arrival"
    NEXT_BAR_OPEN = "next_bar_open"


@attrs.frozen(slots=True)
class Order:
    """A trade request submitted to the matching engine.

    quantity is signed: positive = buy, negative = sell. Notional sign-
    conventions for cash flows are derived from quantity at fill time.

    Per ADR 0011 the `estimate_bps_at_submit` attribute is a dormancy
    tripwire at M2. The tolerance contract in
    `docs/methodology/cost_model_tolerance.md` is documentation-only
    because the Almgren-2005 cost model at M2 is mid-insensitive
    (see `src/pit_backtest/execution/cost/impact.py:162-193`); any
    matcher-side tolerance check would test whether `abs(0.0)` is
    bounded by a non-negative quantity, which is always true.

    The stub property exists so a future contributor at M3 (when
    distinct policy-time vs matcher-time `MarketStateLookup` snapshots
    land) must deliberately delete the NotImplementedError before
    populating the attribute. The activation gate is structural
    (distinct snapshots), NOT `epsilon_bps > 0` (which controls
    slippage, not impact; epsilon does not inject mid into the
    Almgren formula).
    """

    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price_model: FillPriceModel
    submit_dt: datetime

    @property
    def estimate_bps_at_submit(self) -> Decimal:
        """Policy's frozen pre-trade total-bps estimate at order-submit time.

        Per ADR 0011 this attribute is DORMANT at M2. Active enforcement
        of the tolerance contract requires distinct policy-time vs
        matcher-time `MarketStateLookup` snapshots; the M2 default
        wiring uses one cost-model instance for both arms (locked at
        `tests/integration/test_cost_estimator_wired_to_policy.py:141`),
        so the matcher's tolerance check would always pass trivially.

        Reading this attribute raises `NotImplementedError` with a
        diagnostic pointing at ADR 0011 so a future M3 contributor
        cannot accidentally route around the dormancy.

        `hasattr` semantic note (per post-impl reviewer Finding 1):
        Python 3's `hasattr(order, "estimate_bps_at_submit")` does NOT
        return True/False here; the property's `__get__` raises
        `NotImplementedError` rather than `AttributeError`, and only the
        latter is caught by `hasattr`. So `hasattr` PROPAGATES the
        NotImplementedError. This is intentional: the asymmetric
        contract (raise rather than False) prevents a future M3 probe
        of "is the field present" from silently treating dormant as
        absent. Callers that need a presence check should use
        `"estimate_bps_at_submit" in dir(type(order))` or catch
        `NotImplementedError` explicitly.

        Activation-gate wording note (per post-impl reviewer Finding 2):
        The M2 cost model has NO slippage parameter (`epsilon_bps`) at
        all (it is referenced in ADR 0005 step 3 and the methodology
        doc as a future v1.1 knob; `SquareRootImpactCostModel` does not
        expose it). Even adding `epsilon_bps > 0` in v1.1 would not
        inject mid into the Almgren formula. The activation gate is
        therefore structural (distinct snapshots), not a knob flip on
        existing config.
        """
        raise NotImplementedError(
            "ADR 0011: Order.estimate_bps_at_submit is dormant until M3 "
            "introduces distinct policy-time vs matcher-time "
            "MarketStateLookup snapshots. The Almgren-2005 cost model "
            "at M2 is mid-insensitive (no mid argument in _almgren_terms; "
            "no slippage parameter on SquareRootImpactCostModel either). "
            "The tolerance formula in docs/methodology/cost_model_tolerance.md "
            "is documentation-only. See docs/decisions/0011-tolerance-contract-"
            "dormancy-at-m2.md for the activation gate."
        )


@attrs.frozen(slots=True)
class Fill:
    """A single execution result.

    Per ADR 0003 decision 6, MatchingEngine.submit returns list[Fill] so
    partial fills and multi-fill auctions express naturally. quantity here
    may be less than the originating Order.quantity for partial fills.

    Per ADR 0003 decision 14, slippage and impact are split conceptually:
    slippage_bps is the model's component attributable to crossing the
    spread; temporary_impact_bps is the model's component attributable to
    the order's size; permanent_impact_per_share is what the
    ImpactedPriceSource will apply to subsequent reads for this asset.
    """

    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price: Decimal
    slippage_bps: Decimal
    temporary_impact_bps: Decimal
    permanent_impact_per_share: Decimal
    commission: Decimal
    dt: datetime
