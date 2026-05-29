"""MatchingEngine protocol, CloseFillMatchingEngine (M1), and
SquareRootImpactMatchingEngine (M2).

Per ADR 0003 decision 6: submit returns list[Fill] (empty = no fill,
multi = partial-fill or multi-bar rollover).

Per ADR 0009 lock #6, the MatchingEngine Protocol grows on_bar_start(dt)
called by the BarLoop at the top of each per-bar iteration. M1's
CloseFillMatchingEngine implements a no-op; M2's
SquareRootImpactMatchingEngine clears the one-fill-per-(asset, dt)
dedup set.

Per ADR 0009 lock #5, MarketState gains a `prior_close` optional field
for FillPriceModel.ARRIVAL. NEXT_BAR_OPEN remains unsupported at M2
(raises UnsupportedFillPriceModelError per ADR 0009 lock #4) because the
deferred-fill mechanism requires Order plumbing that is M3 scope.

Per ADR 0009 lock #11, exceptions share a MatchingError base so callers
can catch the cross-cutting class while named subclasses identify the
specific failure mode.

Per ADR 0009 lock #14, MarketState construction is keyword-only; the
lint test at tests/lint/test_market_state_keyword_only.py guards against
positional construction silently shifting field assignments.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource
from pit_backtest.execution.clock import Clock
from pit_backtest.execution.cost.base import (
    CostModel,
    Direction,
    FillState,
)
from pit_backtest.execution.cost.commission import Commission
from pit_backtest.execution.cost.commission import _BPS_TO_FRACTION_DIVISOR
from pit_backtest.execution.cost.impact import (
    _et_date,
    _to_boundary_decimal,
)
from pit_backtest.execution.orders import Fill, FillPriceModel, Order


@attrs.frozen(slots=True)
class MarketState:
    """Per-bar snapshot the matching engine needs to compute fills.

    open, high, low, close are bar prices. For M2 the BarLoop populates
    these from Sharadar SEP's split-adjusted open/high/low and unadjusted
    closeunadj (consistent with M1's existing closeunadj usage at
    bar_loop.py:148). volume is unimpacted bar volume.

    prior_close (added per ADR 0009 lock #5) supports
    FillPriceModel.ARRIVAL: arrival price equals the previous bar's
    close. The BarLoop tracks last_close_by_ticker across bars and
    populates this field; first-bar fills with ARRIVAL price model raise
    UnsupportedFillPriceModelError because prior_close is None.

    Per ADR 0009 lock #14 construction is keyword-only; the lint test
    tests/lint/test_market_state_keyword_only.py guards positional
    construction.
    """

    asset_id: AssetId
    dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    prior_close: Decimal | None = None


class MatchingError(ValueError):
    """Base class for matcher-specific errors.

    Per ADR 0009 lock #11 the exception hierarchy is shallow and named:
    UnsupportedFillPriceModelError and MultipleFillsPerBarError derive
    from MatchingError so callers can catch the cross-cutting class
    while named subclasses identify the specific failure mode.
    """


class UnsupportedFillPriceModelError(MatchingError):
    """Raised when an Order's fill_price_model is not supported by the
    current MatchingEngine implementation.

    CloseFillMatchingEngine raises this for any model other than CLOSE.
    SquareRootImpactMatchingEngine raises this for VWAP (real intraday
    data is v1.1) and NEXT_BAR_OPEN (M3 deferred-fill mechanism per
    ADR 0009 lock #4) and ARRIVAL when prior_close is None (first-bar
    edge).
    """


class MultipleFillsPerBarError(MatchingError):
    """Raised on a second submit for the same (asset_id, dt) within the
    same bar.

    Per ADR 0005 step 7 and ADR 0009 lock #7: one-fill-per-(asset, dt)
    is the daily-bar v1 constraint. Intraday slicing (which would allow
    multiple fills per bar) is v1.1 scope.
    """


class MatchingEngine(Protocol):
    """Order-to-fill translation. Per ADR 0003 decision 6 returns list[Fill].

    Per ADR 0009 lock #6, the Protocol grows on_bar_start(bar_dt) called
    by the BarLoop at the top of each per-bar iteration. M1's
    CloseFillMatchingEngine implements a no-op; M2's
    SquareRootImpactMatchingEngine clears the per-bar dedup set.
    """

    def on_bar_start(self, bar_dt: date) -> None:
        """Hook called by the BarLoop at the start of each per-bar
        iteration after clock.advance_to(bar_dt).

        Implementations use this to reset per-bar state (e.g., the
        one-fill-per-(asset, dt) dedup set in M2). No-op for matchers
        that do not carry per-bar state.
        """
        ...

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        """Apply fill_price_model, compute cost, return zero or more Fill records.

        Empty list = no fill (e.g., market closed for the bar, or volume
        is zero). One Fill = full fill in this bar. Multiple Fills =
        partial fill with rollover (M2+).
        """
        ...


@attrs.frozen(slots=True)
class CloseFillMatchingEngine:
    """M1 matching engine: every order fills at today's close, zero cost.

    Accepts only FillPriceModel.CLOSE. Raises UnsupportedFillPriceModelError
    for other models so a future bug (e.g., M5 momentum signal accidentally
    using NEXT_BAR_OPEN) surfaces at the matcher, not as a silent close-price
    substitution.

    Slippage, temporary impact, permanent impact, commission all zero.
    M2's SquareRootImpactMatchingEngine replaces this with a real cost
    model; the BarLoop wiring is unchanged.

    Per ADR 0009 lock #6 implements on_bar_start as a no-op so the M1
    matcher continues to satisfy the extended MatchingEngine Protocol.
    """

    clock: Clock

    def on_bar_start(self, bar_dt: date) -> None:
        """No-op for M1. The constraint that the BarLoop never submits
        more than one order per (asset, dt) is upstream of this matcher;
        the M1 demos do not exercise the dedup edge.
        """
        del bar_dt

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        if order.fill_price_model != FillPriceModel.CLOSE:
            raise UnsupportedFillPriceModelError(
                f"CloseFillMatchingEngine accepts only FillPriceModel.CLOSE; "
                f"got {order.fill_price_model}. M2 SquareRootImpactMatchingEngine "
                f"adds the other fill-price models."
            )
        if order.asset_id != market_state.asset_id:
            raise ValueError(
                f"order asset_id {order.asset_id} does not match market_state "
                f"asset_id {market_state.asset_id}"
            )
        return [
            Fill(
                order_id=order.order_id,
                asset_id=order.asset_id,
                quantity=order.quantity,
                fill_price=market_state.close,
                slippage_bps=Decimal("0"),
                temporary_impact_bps=Decimal("0"),
                permanent_impact_per_share=Decimal("0"),
                commission=Decimal("0"),
                dt=market_state.dt,
            )
        ]


class SquareRootImpactMatchingEngine:
    """M2 matching engine with Almgren 2005 square-root cost model and
    permanent-impact register.

    Composes:
    - clock: time source (unused at v1 except for forward compatibility
      with intraday execution; M2 fills are instantaneous at the bar's
      arrival price).
    - cost_model: SquareRootImpactCostModel (or any PreTradeCostEstimator
      + FillCostComputer). The matcher calls cost_model.compute(fill_state)
      to get the per-fill bps breakdown.
    - commission: PerShareCommission or BasisPointsCommission. Sums into
      Fill.commission.
    - impacted_source: ImpactedPriceSource. The matcher calls
      apply_permanent_impact(asset_id, signed_per_share_decimal) after
      each fill so subsequent BarLoop reads of the asset's price reflect
      the cumulative impact.

    Supported FillPriceModel values per ADR 0009 lock #4:
    - OPEN: arrival = market_state.open
    - CLOSE: arrival = market_state.close
    - ARRIVAL: arrival = market_state.prior_close (raises if None)

    Unsupported:
    - VWAP: raises (v1.1 intraday data per ADR 0005 step 4)
    - NEXT_BAR_OPEN: raises (M3 deferred-fill mechanism)

    Sign convention per ADR 0009 lock #9:
    - signed_temp_fraction = temp_bps / 10_000 * sign(order.quantity)
    - fill_price = arrival * (1 + signed_temp_fraction)
      (buy fills above arrival; sell fills below arrival)
    - signed_perm = perm_bps / 10_000 * fill_price * sign(order.quantity)
      (uses fill_price not arrival per reviewer Medium #7 so OPEN/CLOSE/
      ARRIVAL agree on the dollar magnitude that hits the impact register)
    - permanent_impact_per_share = _to_boundary_decimal(signed_perm)

    Decimal boundary discipline per ADR 0009 Author response item 14:
    every float-to-Decimal conversion at the matcher boundary uses
    _to_boundary_decimal from execution.cost.impact (which uses
    localcontext(prec=28) so a third-party library that mutates
    decimal.getcontext().prec cannot silently change the conversion).

    Per ADR 0009 lock #7 one-fill-per-(asset, dt) is enforced via the
    _fills_this_bar set (membership-only; the determinism invariant
    allows set membership tests, only banning set iteration in signal/
    policy code). on_bar_start clears the set; the BarLoop calls it at
    the start of each per-bar iteration.

    Per ADR 0009 lock #8 the cost-model tolerance contract is NOT
    actively enforced at M2; the methodology doc documents the formula
    and tests/integration/test_cost_estimate_vs_fill_tolerance.py
    exercises it symbolically. PR C lands active enforcement when
    Order.estimate_bps_at_submit ships.
    """

    __slots__ = ("_clock", "_cost_model", "_commission", "_impacted_source",
                 "_fills_this_bar")

    def __init__(
        self,
        clock: Clock,
        cost_model: CostModel,
        commission: Commission,
        impacted_source: ImpactedPriceSource,
    ) -> None:
        self._clock = clock
        self._cost_model = cost_model
        self._commission = commission
        self._impacted_source = impacted_source
        self._fills_this_bar: set[tuple[AssetId, date]] = set()

    def on_bar_start(self, bar_dt: date) -> None:
        """Clear the per-bar dedup set.

        Per ADR 0009 lock #6 the BarLoop calls this at the top of each
        per-bar iteration after clock.advance_to(bar_dt). The bar_dt
        argument is unused at v1 (the set clears wholesale) but is
        retained for the Protocol contract and for forward compatibility
        with a v1.1 partial-fill rollover that would need to distinguish
        bars.
        """
        del bar_dt
        self._fills_this_bar.clear()

    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        """Apply the fill_price_model, compute cost, register permanent
        impact, return one Fill.

        Raises:
        - UnsupportedFillPriceModelError for VWAP, NEXT_BAR_OPEN, or
          ARRIVAL with prior_close=None.
        - MultipleFillsPerBarError for a second submit at the same
          (asset_id, _et_date(market_state.dt)) within the same bar.
        - ValueError for order.asset_id != market_state.asset_id.
        """
        if order.asset_id != market_state.asset_id:
            raise ValueError(
                f"order asset_id {order.asset_id} does not match market_state "
                f"asset_id {market_state.asset_id}"
            )

        # Zero-quantity early return per post-impl reviewer Medium finding:
        # a zero-share order has no direction and no economic effect; the
        # symmetric matcher path would arbitrarily assign direction="sell"
        # (because 0 > 0 is False) and call apply_permanent_impact with
        # Decimal("0"). The early return short-circuits both the misleading
        # direction assignment and the spurious register-touch call.
        if order.quantity == Decimal("0"):
            return [Fill(
                order_id=order.order_id,
                asset_id=order.asset_id,
                quantity=Decimal("0"),
                fill_price=_arrival_price_for_model(order, market_state),
                slippage_bps=Decimal("0"),
                temporary_impact_bps=Decimal("0"),
                permanent_impact_per_share=Decimal("0"),
                commission=Decimal("0"),
                dt=market_state.dt,
            )]

        bar_key = (order.asset_id, _et_date(market_state.dt))
        if bar_key in self._fills_this_bar:
            raise MultipleFillsPerBarError(
                f"second fill attempted for asset_id={order.asset_id} at "
                f"{_et_date(market_state.dt)}; per ADR 0005 step 7 and "
                f"ADR 0009 lock #7 one-fill-per-(asset, dt) is the v1 "
                f"daily-bar constraint. Intraday slicing is v1.1."
            )

        arrival = _arrival_price_for_model(order, market_state)

        # Build the FillState for the cost model. The cost-model's
        # _almgren_terms is symmetric in shares (abs(Q) is used inside);
        # the matcher applies the sign for fill_price and signed_perm.
        direction: Direction = "buy" if order.quantity > 0 else "sell"
        fill_state = FillState(
            asset_id=order.asset_id,
            dt=market_state.dt,
            shares=order.quantity,
            direction=direction,
            bar_open=market_state.open,
            bar_close=market_state.close,
            bar_volume=market_state.volume,
        )
        breakdown = self._cost_model.compute(fill_state)

        # Sign convention per ADR 0009 lock #9. signed_temp_fraction is the
        # multiplicative fraction applied to arrival to get fill_price.
        sign = 1.0 if order.quantity > 0 else -1.0
        signed_temp_fraction = (
            float(breakdown.temporary_impact_bps)
            / float(_BPS_TO_FRACTION_DIVISOR)
            * sign
        )
        fill_price_float = float(arrival) * (1.0 + signed_temp_fraction)
        fill_price = _to_boundary_decimal(fill_price_float)

        # Per ADR 0009 lock #9 signed_perm uses fill_price not arrival so
        # OPEN/CLOSE/ARRIVAL agree on the dollar magnitude that hits the
        # impact register. fill_price is the realized economic price.
        signed_perm = (
            float(breakdown.permanent_impact_bps)
            / float(_BPS_TO_FRACTION_DIVISOR)
            * fill_price_float
            * sign
        )
        permanent_impact_per_share = _to_boundary_decimal(signed_perm)

        # Commission via the signed-notional convention per execution/cost/
        # commission.py: shares is signed; notional = shares * fill_price
        # is signed; commission_for uses abs() so the result is positive
        # regardless of direction.
        notional = order.quantity * fill_price
        commission = self._commission.commission_for(
            shares=order.quantity, notional=notional
        )

        fill = Fill(
            order_id=order.order_id,
            asset_id=order.asset_id,
            quantity=order.quantity,
            fill_price=fill_price,
            slippage_bps=Decimal("0"),
            temporary_impact_bps=breakdown.temporary_impact_bps,
            permanent_impact_per_share=permanent_impact_per_share,
            commission=commission,
            dt=market_state.dt,
        )

        # Register the permanent-impact effect on the decorator so
        # subsequent BarLoop reads of this asset reflect the cumulative
        # impact (per ADR 0003 decision 3).
        self._impacted_source.apply_permanent_impact(
            order.asset_id, permanent_impact_per_share
        )

        self._fills_this_bar.add(bar_key)
        return [fill]


def _arrival_price_for_model(
    order: Order, market_state: MarketState
) -> Decimal:
    """Resolve the arrival price per FillPriceModel.

    Raises UnsupportedFillPriceModelError for VWAP (v1.1 intraday data),
    NEXT_BAR_OPEN (M3 deferred-fill mechanism per ADR 0009 lock #4), and
    ARRIVAL when market_state.prior_close is None (first-bar edge case).
    """
    model = order.fill_price_model
    if model == FillPriceModel.OPEN:
        return market_state.open
    if model == FillPriceModel.CLOSE:
        return market_state.close
    if model == FillPriceModel.ARRIVAL:
        if market_state.prior_close is None:
            raise UnsupportedFillPriceModelError(
                f"FillPriceModel.ARRIVAL requires market_state.prior_close "
                f"but it is None for asset_id={order.asset_id} at "
                f"{market_state.dt}. The BarLoop should populate prior_close "
                f"from the previous bar's close on bars after the first; "
                f"first-bar ARRIVAL fills are not supported. Use OPEN or "
                f"CLOSE on the first bar of the backtest."
            )
        return market_state.prior_close
    if model == FillPriceModel.VWAP:
        raise UnsupportedFillPriceModelError(
            f"FillPriceModel.VWAP requires intraday tick data which is "
            f"v1.1 scope per ADR 0005 step 4. Synthetic VWAP via "
            f"(O+H+L+C)/4 is explicitly refused (the no-silent-substitution "
            f"discipline). For typical-price fills use a future "
            f"FillPriceModel.TYPICAL_PRICE enum value; v1.1 work."
        )
    if model == FillPriceModel.NEXT_BAR_OPEN:
        raise UnsupportedFillPriceModelError(
            f"FillPriceModel.NEXT_BAR_OPEN is an M3 deliverable per "
            f"ADR 0009 lock #4. The deferred-fill mechanism (matcher "
            f"returns no fill on bar N, fill materializes on bar N+1) "
            f"requires Order plumbing that is M3 scope. M2 only supports "
            f"OPEN, CLOSE, and ARRIVAL."
        )
    raise UnsupportedFillPriceModelError(
        f"unknown FillPriceModel {model}"
    )
