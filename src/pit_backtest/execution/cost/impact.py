"""Impact cost models.

Per ADR 0001 decision 6: default is SquareRootImpactCostModel with Almgren
2005 calibration (eta=0.142, beta=0.6, gamma=0.314), labeled as a 1998-2000
calibration in every backtest report. LinearImpact and FixedBps available
as alternatives. NoImpact only with unsuitable_for_deployment=True flag
and a runtime warning per ADR 0002 decision 5.

A --impact-model=bouchaud flag substitutes beta=0.5 per ADR 0001 decision 6
and ADR 0002 decision 5.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pit_backtest.data.records import AssetId
from pit_backtest.execution.cost.base import (
    CostBreakdown,
    Direction,
    FillCostComputer,
    FillState,
    PreTradeCostEstimator,
)


class SquareRootImpactCostModel(PreTradeCostEstimator, FillCostComputer):
    """Almgren 2005 square-root market-impact model.

    Default parameters (1998-2000 NYSE/Nasdaq calibration):
      eta = 0.142
      beta = 0.6
      gamma = 0.314

    Bouchaud override: beta = 0.5, surfaces via the --impact-model=bouchaud
    CLI flag.
    """

    def __init__(
        self,
        eta: Decimal = Decimal("0.142"),
        beta: Decimal = Decimal("0.6"),
        gamma: Decimal = Decimal("0.314"),
    ) -> None:
        raise NotImplementedError("M2 deliverable")

    def estimate(
        self,
        asset_id: AssetId,
        shares: Decimal,
        direction: Direction,
        dt: datetime,
    ) -> Decimal:
        raise NotImplementedError("M2 deliverable")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        raise NotImplementedError("M2 deliverable")


class LinearImpact(PreTradeCostEstimator, FillCostComputer):
    """Almgren-Chriss 2000 linear model. Available; not default."""

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        raise NotImplementedError("M2 deliverable")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        raise NotImplementedError("M2 deliverable")


class FixedBps(PreTradeCostEstimator, FillCostComputer):
    """Single-parameter slippage. Available; not default."""

    def __init__(self, bps: Decimal) -> None:
        raise NotImplementedError("M2 deliverable")

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        raise NotImplementedError("M2 deliverable")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        raise NotImplementedError("M2 deliverable")


class NoImpact(PreTradeCostEstimator, FillCostComputer):
    """Zero-cost cost model.

    Constructable only with unsuitable_for_deployment=True. Emits a
    runtime warning when used. Per ADR 0002 decision 5, this is the API-
    level safety belt that prevents accidentally leaving zero-cost flags
    on across a study.
    """

    def __init__(self, unsuitable_for_deployment: Literal[True]) -> None:
        if not unsuitable_for_deployment:
            raise ValueError(
                "NoImpact requires unsuitable_for_deployment=True. "
                "Backtests with zero-cost slippage are not deployment-ready."
            )
        warnings.warn(
            "NoImpact in use; results overstate strategy returns.",
            stacklevel=2,
        )

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        return Decimal("0")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        return CostBreakdown(
            slippage_bps=Decimal("0"),
            temporary_impact_bps=Decimal("0"),
            permanent_impact_bps=Decimal("0"),
            commission=Decimal("0"),
        )
