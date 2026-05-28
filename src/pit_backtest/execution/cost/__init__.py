"""Cost models: pre-trade estimation and fill cost computation.

Per ADR 0003 decision 4, PreTradeCostEstimator and FillCostComputer are
separate protocols. A single concrete class can implement both, but the
protocols themselves are distinct so researchers cannot accidentally put
expensive computation in the pre-trade path.

Default cost model is SquareRootImpactCostModel with Almgren 2005 calibration
(eta=0.142, beta=0.6, gamma=0.314). Labeled as a 1998-2000 calibration in
every backtest report per ADR 0001 decision 6.
"""
