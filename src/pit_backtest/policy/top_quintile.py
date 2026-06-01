"""TopQuintileLongPolicy: long the top-quintile momentum names, equal-weight.

The M5 worked study's policy (ADR 0002 decision 20): single-factor JT1993
12-1 momentum, monthly rebalance, top-quintile long equal-weight. This is a
thin rank-then-select layer over `EqualWeightMonthlyRebalancePolicy`: it
ranks the signal scores, keeps the top quintile, and hands those names to
the equal-weight policy with uniform weights, so the equal-weight policy's
existing price-filter + NAV + re-normalization + Decimal-boundary logic is
reused verbatim (one source of truth for the dollar-target arithmetic).

Equal-weight, NOT score-weight: the equal-weight long is the JT1993 long-leg
convention, and momentum scores can be negative, so weighting by score would
produce negative dollar targets that the long-only invariant (ADR 0003
decision 15) forbids. Equal-weighting the selected winners sidesteps that
entirely; the cross-sectional rank, not the absolute sign, is what selects.

Determinism: the rank is `sorted(items, key=(-score, asset_id))`, a total
order (scores descending, AssetId ascending as the tie-break), so two runs
select the same names; no set iteration in the output path (Determinism
Requirement 4).
"""

from __future__ import annotations

import math
from datetime import date, datetime

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.policy.base import (
    Policy,
    PortfolioStateLike,
    PreTradeCostEstimatorLike,
    TargetPositions,
)
from pit_backtest.policy.equal_weight import (
    EqualWeightMonthlyRebalancePolicy,
    PriceLookup,
)


@attrs.frozen(slots=True)
class TopQuintileLongPolicy(Policy):
    """Long the top-quintile-ranked names equal-weight on rebalance days.

    rebalance_dates is a frozenset of dates (membership test only; never
    iterated). price_lookup returns today's close or None; it is passed
    through to the composed `EqualWeightMonthlyRebalancePolicy`.

    On a rebalance day with a non-empty signal, the policy ranks the scores
    (descending, AssetId tie-break), keeps the top `ceil(n / 5)` names (at
    least one), assigns them uniform weights, and delegates to the
    equal-weight policy. On a non-rebalance day or an empty signal it returns
    empty targets (the BarLoop interprets that as "no orders"). A selected
    name with no price at dt is filtered by the equal-weight policy and the
    remaining names re-share the book, matching the ADR 0017 omission of an
    untradeable member.
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
        if not signal_output:
            return TargetPositions(dt=dt, targets={})

        # Rank descending by score, AssetId ascending as the deterministic
        # tie-break. signal_output is a dict (no set iteration).
        ranked = sorted(
            signal_output.items(), key=lambda item: (-item[1], item[0])
        )
        # Top quintile: ceil(n / 5), never empty on a non-empty signal.
        cut = max(1, math.ceil(len(ranked) / 5))
        # Uniform weights: the equal-weight policy re-normalizes 1.0 each to
        # 1 / cut over the priced subset, yielding equal dollar weight.
        equal_weights: dict[AssetId, float] = {
            asset_id: 1.0 for asset_id, _score in ranked[:cut]
        }

        # Delegate the price-filter + NAV + re-normalization + Decimal
        # boundary to the equal-weight policy (single source of truth).
        equal_weight_policy = EqualWeightMonthlyRebalancePolicy(
            rebalance_dates=self.rebalance_dates,
            price_lookup=self.price_lookup,
        )
        return equal_weight_policy.target_positions(
            signal_output=equal_weights,
            current_positions=current_positions,
            cost_estimator=cost_estimator,
            dt=dt,
        )
