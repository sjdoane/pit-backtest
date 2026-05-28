"""Cost model protocols and CostBreakdown render target.

Per ADR 0003 decision 4: PreTradeCostEstimator and FillCostComputer are
separate protocols. Pre-trade is called once per asset per bar (up to 500
times for a 500-name universe); fill computation only on assets that
actually got an order (10-50 per bar). Different speed requirements; the
split prevents researchers putting slow code in the pre-trade path.

CostBreakdown is a Pydantic render target (user-facing) per the boundary
contract in docs/methodology/pydantic_polars_boundary.md.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

import attrs
from pydantic import BaseModel, ConfigDict

from pit_backtest.data.records import AssetId


Direction = Literal["buy", "sell"]


@attrs.frozen(slots=True)
class FillState:
    """Per-fill state needed by FillCostComputer.

    Carries the impacted prices, the bar volume, and the realized share
    count so the cost computer can produce a full CostBreakdown without
    re-querying the data source.
    """

    asset_id: AssetId
    dt: datetime
    shares: Decimal
    direction: Direction
    bar_open: Decimal
    bar_close: Decimal
    bar_volume: int


class CostBreakdown(BaseModel):
    """User-facing cost decomposition for a single fill.

    Per ADR 0003 decision 14: slippage and impact split explicitly. Total
    cost is the sum of the bps components applied to notional, plus the
    commission dollar amount.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    slippage_bps: Decimal
    temporary_impact_bps: Decimal
    permanent_impact_bps: Decimal
    commission: Decimal

    @property
    def total_bps(self) -> Decimal:
        return self.slippage_bps + self.temporary_impact_bps + self.permanent_impact_bps


class PreTradeCostEstimator(Protocol):
    """Fast pre-trade cost lookup, called once per candidate trade per bar."""

    def estimate(
        self,
        asset_id: AssetId,
        shares: Decimal,
        direction: Direction,
        dt: datetime,
    ) -> Decimal:
        """Returns expected total cost in basis points of notional.

        Fast path; the policy layer uses this to decide whether to commit
        to a trade list. Detailed breakdown is reserved for FillCostComputer.
        """
        ...


class FillCostComputer(Protocol):
    """Per-fill cost computation, called only for orders that actually trade."""

    def compute(self, fill_state: FillState) -> CostBreakdown:
        """Return the detailed cost breakdown for this fill."""
        ...
