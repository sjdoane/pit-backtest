"""Policy protocol and v1 LongOnlyMonthlyRebalancePolicy.

Per ADR 0003 decisions 15 and 22: LongOnlyPolicy at v1; AlgoStack composition
dropped (can return in v1.1); Policy.target_positions is a single function
that consumes signals and produces target dollar positions, querying the
pre-trade cost estimator before committing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol

import attrs

from pit_backtest.data.records import AssetId


@attrs.frozen(slots=True)
class TargetPositions:
    """Dollar target positions at a specific rebalance date.

    targets maps AssetId -> signed dollar amount; positive = long. v1's
    LongOnlyPolicy enforces all values are non-negative at construction.
    """

    dt: datetime
    targets: dict[AssetId, Decimal]


# Forward references; the real types live in execution layer and engine
# layer. Imported as TYPE_CHECKING in M1 wiring to avoid circular imports.
class PreTradeCostEstimatorLike(Protocol):
    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: str, dt: datetime
    ) -> Decimal: ...


class PortfolioStateLike(Protocol):
    """Structural protocol the Policy layer needs from PortfolioState.

    Per the M1 day 3 design: inner-loop arithmetic is float64, not Decimal.
    The full PortfolioState (engine/state.py) carries cash and positions
    as floats; TargetPositions.targets stays Decimal as a boundary type
    converted at construction via Decimal(repr(float_value)).
    """

    cash: float
    positions: dict[AssetId, float]


class Policy(Protocol):
    """Translate signal scores to target dollar positions."""

    def target_positions(
        self,
        signal_output: dict[AssetId, float],
        current_positions: PortfolioStateLike,
        cost_estimator: PreTradeCostEstimatorLike,
        dt: datetime,
    ) -> TargetPositions:
        ...


class LongOnlyMonthlyRebalancePolicy(Policy):
    """v1 default policy.

    Constructed with a signal_to_weights_fn that maps the signal dict to a
    target-weights dict (must sum to <= 1.0 and contain only non-negative
    values). On monthly rebalance dates, the policy translates the weights
    to dollar amounts against current portfolio value and emits target
    positions; on intra-month dates, target positions equal current positions.

    Rejects negative weights at construction with a ValueError. Per ADR 0003
    decision 15, short selling is v1.1.
    """

    def __init__(
        self,
        signal_to_weights_fn: Callable[[dict[AssetId, float]], dict[AssetId, Decimal]],
    ) -> None:
        raise NotImplementedError("M1 deliverable (constant-weight rebalance demo)")

    def target_positions(
        self,
        signal_output: dict[AssetId, float],
        current_positions: PortfolioStateLike,
        cost_estimator: PreTradeCostEstimatorLike,
        dt: datetime,
    ) -> TargetPositions:
        raise NotImplementedError("M1 deliverable")
