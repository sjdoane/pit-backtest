"""EqualWeightMonthlyRebalancePolicy: constant-weight rebalance on calendar dates.

Per ADR 0004 (rebalance calendar independence): rebalance dates are fund-
policy-determined, independent of the backtest window. start_dt is NOT
forced to be a rebalance date; the engine initializes as cash and holds
flat until the first scheduled rebalance.

The Policy takes a frozenset of rebalance dates (O(1) membership test) and
a price_lookup callable. On rebalance days, it reads current positions and
prices, computes NAV, and produces a TargetPositions with target dollar
amounts per live ticker. On non-rebalance days, it returns empty targets
(BarLoop interprets as "no orders").
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Callable

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.policy.base import (
    Policy,
    PortfolioStateLike,
    PreTradeCostEstimatorLike,
    TargetPositions,
)


# Callable signature for "what is the close price for asset_id at dt?"
# Returns None if no price is available (e.g., before ticker inception,
# or on a vendor gap). The Policy filters tickers without prices out of
# the rebalance.
PriceLookup = Callable[[AssetId, datetime], "float | None"]


@attrs.frozen(slots=True)
class EqualWeightMonthlyRebalancePolicy(Policy):
    """Equal-weight target on rebalance days.

    rebalance_dates is a frozenset of dates (membership test only; never
    iterated). price_lookup is a callable that returns today's close price
    or None for the asset_id and dt.

    On rebalance days, the Policy:
    1. Filters tickers in signal_output to those with available prices.
    2. Computes NAV = cash + sum(shares * price) over current positions
       in sorted AssetId order (float-determinism per docs/methodology/
       determinism.md Requirement 3).
    3. Re-normalizes signal weights over live tickers (so a ticker pre-
       inception does not get a non-zero target).
    4. Returns target dollar amounts as Decimal via Decimal(repr(float)).
    """

    rebalance_dates: frozenset[date]
    price_lookup: PriceLookup

    def target_positions(
        self,
        signal_output: dict[AssetId, float],
        current_positions: PortfolioStateLike,
        cost_estimator: PreTradeCostEstimatorLike,
        dt: datetime,
    ) -> TargetPositions:
        d = dt.date() if isinstance(dt, datetime) else dt
        if d not in self.rebalance_dates:
            return TargetPositions(dt=dt, targets={})

        # Fetch prices for all signal tickers; drop the ones with no price.
        prices: dict[AssetId, float] = {}
        for ticker in sorted(signal_output):
            p = self.price_lookup(ticker, dt)
            if p is not None:
                prices[ticker] = p

        if not prices:
            # Nothing tradable today; emit no orders rather than raise so
            # the BarLoop can proceed and the engine can record a flat NAV.
            return TargetPositions(dt=dt, targets={})

        # NAV via current positions and today's prices. Iteration order:
        # sorted(current_positions.positions) per the float-determinism
        # requirement. We sum only the positions whose ticker is also in
        # `prices` because a held position without a price is a data gap
        # the BarLoop will surface elsewhere.
        nav = current_positions.cash
        for ticker in sorted(current_positions.positions):
            shares = current_positions.positions[ticker]
            if shares == 0.0:
                continue
            if ticker in prices:
                nav += shares * prices[ticker]

        # Re-normalize signal weights over live tickers. For equal-weight
        # this is just (1 / live_count) per live ticker, but the explicit
        # re-norm keeps the policy correct if a future ticker-specific
        # weight is passed (M5 momentum top-quintile equal-weight).
        live = sorted(prices.keys())
        total_weight = 0.0
        for ticker in live:
            total_weight += signal_output[ticker]
        if total_weight == 0.0:
            return TargetPositions(dt=dt, targets={})

        targets: dict[AssetId, Decimal] = {}
        for ticker in live:
            weight = signal_output[ticker] / total_weight
            target_dollars = nav * weight
            # Decimal(repr(float)) is bit-stable: float(Decimal(repr(x))) == x.
            targets[ticker] = Decimal(repr(target_dollars))

        return TargetPositions(dt=dt, targets=targets)
