"""BarLoop: single-process sequential per-bar dispatch.

Per ADR 0003 event-loop decision: sequential per-bar dispatch, not a
message bus or event queue. The kernel-sharing claim from ADR 0001
decision 5 is honored by the Clock injection pattern; the message bus is
not required to keep that promise. v1.2 work can refactor to a message
bus if live trading is added.

Per-bar sequence:
1. clock.advance_to(dt)
2. data_source.get_corporate_actions(...) -> apply unit transformations
3. data_source.get_cash_flows(...) -> credit per-asset cash flows
4. signal.compute(universe, dt, pit_view) -> signal output
5. policy.target_positions(signal, current, cost_estimator, dt) -> targets
6. matching_engine.submit(orders, market_state) -> fills
7. portfolio_state.apply(fills) -> updated positions, cash, P&L
8. analytics.record_bar(dt, portfolio_state, fills, signal)

Delistings are ordered per ADR 0003 decision 13: signal computation on day
T uses prices through T-1; delisting cash flow applied at the open of T+1.
"""

from __future__ import annotations

from datetime import datetime

from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.data.sources.base import PitDataSource
from pit_backtest.data.universe import Universe
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.base import FillCostComputer, PreTradeCostEstimator
from pit_backtest.execution.cost.commission import Commission
from pit_backtest.execution.matching import MatchingEngine
from pit_backtest.policy.base import Policy
from pit_backtest.signal.base import Signal
from pit_backtest.validation.trial_registry import TrialRegistry


class BarLoop:
    """The per-bar dispatch driver."""

    def __init__(
        self,
        *,
        data_source: PitDataSource,
        universe: Universe,
        signal: Signal,
        policy: Policy,
        pre_trade_cost_estimator: PreTradeCostEstimator,
        fill_cost_computer: FillCostComputer,
        commission: Commission,
        matching_engine: MatchingEngine,
        clock: TestClock,
        trial_registry: TrialRegistry | None = None,
    ) -> None:
        raise NotImplementedError("M1 deliverable")

    def run(self, start_dt: datetime, end_dt: datetime) -> BacktestResult:
        raise NotImplementedError("M1 deliverable")

    def timing_breakdown(self) -> dict[str, float]:
        """Per-step elapsed time in seconds, for the perf-budget CI check.

        Per ADR 0003 decision 18: returned at the end of every run; CI
        dumps to the artifacts bundle on every benchmark run.
        """
        raise NotImplementedError("M2 deliverable")
