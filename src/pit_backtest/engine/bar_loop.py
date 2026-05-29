"""BarLoop: single-process sequential per-bar dispatch.

Per ADR 0003 event-loop decision: sequential per-bar dispatch, not a
message bus or event queue.

The M1 implementation drives the constant-weight monthly rebalance demo
per ADR 0002 acceptance criterion 2. Per the M1 day 3 skeptical-reviewer
pass, the BarLoop wires Signal -> Policy -> MatchingEngine -> PortfolioState
end-to-end so the M2 cost-model swap can re-run the same demo against the
new matcher and produce an interpretable regression check.

Per ADR 0004 (rebalance calendar independence): start_dt is NOT forced as
a rebalance date. The engine initializes as all-cash on start_dt and waits
for the first scheduled rebalance.

Per-bar sequence:
1. clock.advance_to(bar_dt) (16:00 ET).
2. Credit dividends from end-of-prior-bar shares.
3. Fetch today's prices for the strategy's tickers.
4. signal.compute(universe, dt, pit_view) -> dict[AssetId, float].
5. policy.target_positions(signal, state, cost_estimator, dt) -> TargetPositions.
6. For each target: construct Order, submit to matching_engine, apply Fill
   to state.cash and state.positions.
7. state.snapshot(dt, prices_today) recorded into the equity curve.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

import polars as pl

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import Universe
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.engine.m1_demo import asset_id_to_ticker
from pit_backtest.engine.state import PortfolioState
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import (
    CloseFillMatchingEngine,
    MarketState,
    MatchingEngine,
)
from pit_backtest.execution.orders import FillPriceModel, Order
from pit_backtest.policy.base import Policy
from pit_backtest.signal.base import PitView, Signal
from pit_backtest.utils.logging import get_logger
from pit_backtest.validation.confidence_tier import ConfidenceTier


_log = get_logger(__name__)


class _NoopPitView:
    """Stand-in PitView for M1.

    The constant-weight Signal does not consult historical data; pit_view
    is a no-op. M3's signals (e.g., Momentum12_1Signal) will receive a
    real PitView from BarLoop that enforces available_dt < dt.
    """

    def __call__(self, table_name: str) -> pl.LazyFrame:
        raise NotImplementedError(
            "M1 BarLoop's signals do not consume historical data via pit_view"
        )


class _NoopCostEstimator:
    """Stand-in PreTradeCostEstimator for M1.

    The constant-weight Policy needs the protocol shape but does not
    consult pre-trade costs (the demo runs with NoImpact). M2's policies
    will receive a real cost estimator.
    """

    def estimate(
        self,
        asset_id: AssetId,
        shares: Decimal,
        direction: str,
        dt: datetime,
    ) -> Decimal:
        return Decimal("0")


class BarLoop:
    """The per-bar dispatch driver for the M1 constant-weight demo."""

    def __init__(
        self,
        *,
        data_source: SharadarDataSource,
        universe: Universe,
        signal: Signal,
        policy: Policy,
        matching_engine: MatchingEngine,
        clock: TestClock,
        tickers: tuple[AssetId, ...],
        initial_capital: float,
    ) -> None:
        self._data_source = data_source
        self._universe = universe
        self._signal = signal
        self._policy = policy
        self._matching_engine = matching_engine
        self._clock = clock
        self._tickers = tuple(sorted(tickers))
        self._initial_capital = float(initial_capital)
        self._state = PortfolioState(
            cash=float(initial_capital),
            positions={ticker: 0.0 for ticker in self._tickers},
            initial_capital=float(initial_capital),
            realized_pnl=0.0,
        )
        self._pit_view = _NoopPitView()
        self._cost_estimator = _NoopCostEstimator()

    @property
    def state(self) -> PortfolioState:
        """Expose the live state for test assertions; mutate at your peril."""
        return self._state

    def run(self, start_dt: date, end_dt: date) -> ConstantWeightDemoResult:
        # Pre-load prices and dividends for all strategy tickers over the window.
        # Both engine and reference function read from these same eager Polars
        # frames (already sorted by dt by SharadarDataSource); no LazyFrame plan
        # re-ordering can drift the reductions.
        prices_by_asset: dict[AssetId, pl.DataFrame] = {}
        dividends_by_asset: dict[AssetId, pl.DataFrame] = {}
        for ticker in self._tickers:
            ticker_str = asset_id_to_ticker(ticker)
            prices_by_asset[ticker] = self._data_source.read_sep_prices(
                ticker=ticker_str, start_dt=start_dt, end_dt=end_dt
            )
            dividends_by_asset[ticker] = self._data_source.read_actions_dividends(
                ticker=ticker_str, start_dt=start_dt, end_dt=end_dt
            )

        # Build per-bar lookup indexes. Iteration over the resulting dicts is
        # always wrapped in sorted(...) at consumption time.
        price_at: dict[tuple[AssetId, date], float] = {}
        for ticker in sorted(prices_by_asset.keys()):
            frame = prices_by_asset[ticker]
            for row in frame.iter_rows(named=True):
                price_at[(ticker, row["dt"])] = float(row["closeunadj"])

        divs_at: dict[date, dict[AssetId, float]] = {}
        for ticker in sorted(dividends_by_asset.keys()):
            frame = dividends_by_asset[ticker]
            for row in frame.iter_rows(named=True):
                d = row["ex_date"]
                if d not in divs_at:
                    divs_at[d] = {}
                divs_at[d][ticker] = float(row["amount_per_share"])

        # Trading days in the window. Per ADR 0004, the policy carries its
        # own rebalance-date frozenset (computed once at Backtest.__init__);
        # the BarLoop just iterates trading days in sorted order.
        trading_days_in_window = tuple(
            d for d in self._clock.trading_days() if start_dt <= d <= end_dt
        )
        if not trading_days_in_window:
            raise ValueError(
                f"no NYSE trading days in window [{start_dt}, {end_dt}]; "
                f"check TestClock cache and window bounds"
            )

        _log.info(
            "bar_loop_begin",
            extra={
                "tickers": ",".join(asset_id_to_ticker(t) for t in self._tickers),
                "start_dt": start_dt.isoformat(),
                "end_dt": end_dt.isoformat(),
                "initial_capital": f"{self._initial_capital:.2f}",
                "n_trading_days": len(trading_days_in_window),
            },
        )

        equity_curve_rows: list[dict[str, object]] = []
        n_rebalances = 0

        for bar_dt in trading_days_in_window:
            self._clock.advance_to(bar_dt)
            now = self._clock.now()

            # Step 2: credit dividends from end-of-prior-bar shares.
            bar_divs = divs_at.get(bar_dt, {})
            for ticker in sorted(bar_divs.keys()):
                div = bar_divs[ticker]
                shares = self._state.positions.get(ticker, 0.0)
                if shares != 0.0:
                    self._state.cash += shares * div

            # Step 3: today's prices for this strategy's tickers.
            prices_today: dict[AssetId, float] = {}
            for ticker in self._tickers:
                key = (ticker, bar_dt)
                if key in price_at:
                    prices_today[ticker] = price_at[key]

            # Step 4: signal.
            signal_output = self._signal.compute(self._universe, now, self._pit_view)

            # Step 5: policy.
            targets = self._policy.target_positions(
                signal_output=signal_output,
                current_positions=self._state,
                cost_estimator=self._cost_estimator,
                dt=now,
            )

            # Step 6: convert targets into orders + fills + state updates.
            if targets.targets:
                n_rebalances += 1
                for ticker in sorted(targets.targets.keys()):
                    if ticker not in prices_today:
                        continue
                    target_dollars = float(targets.targets[ticker])
                    close_t = prices_today[ticker]
                    target_shares = target_dollars / close_t
                    current_shares = self._state.positions.get(ticker, 0.0)
                    qty = target_shares - current_shares
                    if qty == 0.0:
                        continue
                    order = Order(
                        order_id=f"{bar_dt.isoformat()}_{asset_id_to_ticker(ticker)}",
                        asset_id=ticker,
                        quantity=Decimal(repr(qty)),
                        fill_price_model=FillPriceModel.CLOSE,
                        submit_dt=now,
                    )
                    market_state = MarketState(
                        asset_id=ticker,
                        dt=now,
                        open=Decimal(repr(close_t)),
                        high=Decimal(repr(close_t)),
                        low=Decimal(repr(close_t)),
                        close=Decimal(repr(close_t)),
                        volume=0,
                    )
                    fills = self._matching_engine.submit(order, market_state)
                    for fill in fills:
                        fill_qty = float(fill.quantity)
                        fill_price = float(fill.fill_price)
                        self._state.cash -= fill_qty * fill_price
                        self._state.positions[fill.asset_id] = (
                            self._state.positions.get(fill.asset_id, 0.0) + fill_qty
                        )

            # Step 7: snapshot at today's close.
            nav_close = self._state.cash
            for ticker in sorted(self._state.positions.keys()):
                shares = self._state.positions[ticker]
                if shares != 0.0 and ticker in prices_today:
                    nav_close += shares * prices_today[ticker]

            curve_row: dict[str, object] = {
                "dt": bar_dt,
                "cash": self._state.cash,
                "nav": nav_close,
            }
            for ticker in self._tickers:
                curve_row[f"shares_{ticker}"] = self._state.positions.get(ticker, 0.0)
            equity_curve_rows.append(curve_row)

        equity_curve = pl.DataFrame(equity_curve_rows).sort("dt")
        final_nav = float(equity_curve["nav"][-1])
        final_pnl = final_nav - self._initial_capital

        result = ConstantWeightDemoResult(
            final_pnl=final_pnl,
            final_nav=final_nav,
            initial_capital=self._initial_capital,
            equity_curve=equity_curve,
            n_trading_days=len(trading_days_in_window),
            n_rebalances=n_rebalances,
            tickers=tuple(asset_id_to_ticker(t) for t in self._tickers),
            start_dt=start_dt,
            end_dt=end_dt,
            confidence_tier=ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
            sharadar_bundle=self._data_source.bundle_name,
        )

        _log.info(
            "bar_loop_complete",
            extra={
                "final_pnl": f"{final_pnl:+,.4f}",
                "final_nav": f"{final_nav:,.4f}",
                "n_rebalances": n_rebalances,
            },
        )
        return result
