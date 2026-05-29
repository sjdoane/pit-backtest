"""Cost models: pre-trade estimation and fill cost computation.

Per ADR 0003 decision 4: PreTradeCostEstimator and FillCostComputer are
separate protocols. A single concrete class can implement both, but the
protocols themselves are distinct so researchers cannot accidentally put
expensive computation in the pre-trade path.

Per ADR 0001 decision 6 and ADR 0005 step 1 the default cost model is
SquareRootImpactCostModel with the Almgren 2005 Risk magazine v18
Section 3 calibration (eta=0.142, beta=0.6, gamma=0.314). Labeled as a
1998-2000 NYSE/Nasdaq calibration in every backtest report.

Per ADR 0005 step 10: PerShareCommission and BasisPointsCommission both
carry a /100 silent-rescale regression guard (the backtrader bug class).
"""

from pit_backtest.execution.cost.base import (
    CostBreakdown,
    Direction,
    FillCostComputer,
    FillState,
    PreTradeCostEstimator,
)
from pit_backtest.execution.cost.commission import (
    BasisPointsCommission,
    Commission,
    PerShareCommission,
)
from pit_backtest.execution.cost.impact import (
    DEFAULT_BETA,
    DEFAULT_ETA,
    DEFAULT_GAMMA,
    DEFAULT_T,
    FixedBps,
    LinearImpact,
    MarketStateLookup,
    MarketStateRow,
    NoImpact,
    SquareRootImpactCostModel,
)


__all__ = [
    "BasisPointsCommission",
    "Commission",
    "CostBreakdown",
    "DEFAULT_BETA",
    "DEFAULT_ETA",
    "DEFAULT_GAMMA",
    "DEFAULT_T",
    "Direction",
    "FillCostComputer",
    "FillState",
    "FixedBps",
    "LinearImpact",
    "MarketStateLookup",
    "MarketStateRow",
    "NoImpact",
    "PerShareCommission",
    "PreTradeCostEstimator",
    "SquareRootImpactCostModel",
]
