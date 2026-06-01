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

Per ADR 0009 lock #2, #5, #6, #10, the M2 wiring additions are:
- impacted_source: ImpactedPriceSource | None = None. When non-None,
  MarketState fields are routed through adjust_price() at construction
  time and (optionally) at snapshot time. The matcher updates the
  decorator's register after each Fill.
- cost_estimator: PreTradeCostEstimator | None = None. When non-None,
  replaces the _NoopCostEstimator passed to Policy.target_positions(...)
  so the policy can opt out of trades whose expected cost exceeds the
  alpha (ADR 0003 decision 4).
- apply_permanent_impact_to_valuation: bool = True. The snapshot/MTM
  step routes prices through adjust_price() when both this flag is True
  and impacted_source is not None.
- matching_engine.on_bar_start(bar_dt) is called unconditionally at the
  top of each per-bar iteration so the matcher can reset per-bar state
  (the one-fill-per-(asset, dt) dedup set in M2). Per ADR 0009 lock #6
  the Protocol extension makes this unconditional; M1's
  CloseFillMatchingEngine implements a no-op.

Per ADR 0009 lock #4 the matcher supports FillPriceModel.OPEN/CLOSE/
ARRIVAL. The BarLoop now reads the full OHLC tuple from SEP for each
bar (M1 read only closeunadj) and populates the MarketState's open /
high / low / close fields accordingly. The unadjusted close (closeunadj)
remains the price used for cash-flow accounting at the CLOSE fill model
to preserve M1's existing behavior; M3 corporate-action work will
re-do the split-adjustment story properly.

The policy's price_lookup (used inside EqualWeightMonthlyRebalancePolicy
to compute NAV pre-trade) reads raw closeunadj at M2. M3 will wire the
policy's price_lookup through ImpactedPriceSource for impact-aware NAV.
At M2 with NoImpact (Layer 2 invariant) the two NAVs agree to 1e-10.

Per-bar sequence:
1. clock.advance_to(bar_dt) (16:00 ET).
2. matching_engine.on_bar_start(bar_dt) (per ADR 0009 lock #6).
3. Credit dividends from end-of-prior-bar shares.
4. Fetch today's prices for the strategy's tickers.
5. signal.compute(universe, dt, pit_view) -> dict[AssetId, float].
6. policy.target_positions(signal, state, cost_estimator, dt) -> TargetPositions.
7. For each target: construct Order, construct MarketState (with real
   OHLC, prior_close, optionally impact-adjusted), submit to
   matching_engine, apply Fill to state.cash (now subtracts commission
   per ADR 0009 lock #9 cash-flow semantics) and state.positions.
8. state.snapshot(dt, prices_today) recorded into the equity curve;
   prices routed through adjust_price() per the valuation policy.
9. Update last_close_raw_by_ticker for the next bar's prior_close.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Mapping

import polars as pl

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import Universe
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.engine.m1_demo import (
    asset_id_to_ticker as _default_asset_id_to_ticker,
)
from pit_backtest.engine.state import PortfolioState
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.base import Direction, PreTradeCostEstimator
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
    """Stand-in PitView for the constant-weight signal.

    The constant-weight Signal does not consult historical data; pit_view
    is a no-op. It stays the BarLoop default (`use_real_pit_view=False`) so
    the M1/M2 demos pay nothing and the SPY/AGG/GLD bundles that lack a
    TICKERS table do not need one. History-consuming signals (e.g.,
    Momentum12_1Signal) set `use_real_pit_view=True`; the BarLoop then
    rebuilds a real per-bar PitView via `_build_pit_view` that slices each
    served table to available_dt < the bar date (ADR 0016 M5 PR 2b).
    """

    def __call__(self, table_name: str) -> pl.LazyFrame:
        raise NotImplementedError(
            "M1 BarLoop's signals do not consume historical data via pit_view"
        )


class _NoopCostEstimator:
    """Stand-in PreTradeCostEstimator for M1.

    The constant-weight Policy needs the protocol shape but does not
    consult pre-trade costs (the demo runs with NoImpact). Per ADR 0009
    lock #5 the BarLoop ctor now accepts an explicit `cost_estimator`
    keyword that replaces this stand-in when the caller has a real cost
    estimator to wire through; the M1 demos that omit it keep the no-op.
    """

    def estimate(
        self,
        asset_id: AssetId,
        shares: Decimal,
        direction: Direction,
        dt: datetime,
    ) -> Decimal:
        return Decimal("0")


class BarLoop:
    """The per-bar dispatch driver for the M1 constant-weight demo,
    extended in M2 PR B to wire ImpactedPriceSource, the real cost
    estimator into the policy, and the matcher's on_bar_start hook.
    """

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
        impacted_source: ImpactedPriceSource | None = None,
        cost_estimator: PreTradeCostEstimator | None = None,
        apply_permanent_impact_to_valuation: bool = True,
        enable_timing: bool = False,
        use_real_pit_view: bool = False,
        asset_id_to_ticker: Callable[[AssetId], str] | None = None,
    ) -> None:
        self._data_source = data_source
        self._universe = universe
        self._signal = signal
        self._policy = policy
        self._matching_engine = matching_engine
        self._clock = clock
        # AssetId -> ticker resolver. Default is the M1 demo's three-name map
        # (SPY/AGG/GLD), which keeps the constant-weight reconciliation
        # byte-identical; the M5 momentum study injects a resolver-backed
        # callable so the BarLoop can run an S&P 500 universe. Replacing the
        # M1 hardcoded map by an injected callable (rather than rewriting the
        # loop to a dynamic universe) is the surgical fix per ADR 0016 PR 2b.
        self._asset_id_to_ticker: Callable[[AssetId], str] = (
            asset_id_to_ticker
            if asset_id_to_ticker is not None
            else _default_asset_id_to_ticker
        )
        self._tickers = tuple(sorted(tickers))
        self._initial_capital = float(initial_capital)
        self._state = PortfolioState(
            cash=float(initial_capital),
            positions={ticker: 0.0 for ticker in self._tickers},
            initial_capital=float(initial_capital),
            realized_pnl=0.0,
        )
        # PitView wiring. The M1/M2 constant-weight signal ignores pit_view,
        # so the default is the no-op stand-in (and the M1 SPY/AGG/GLD bundles
        # lack a TICKERS table the real view would need). The M5 momentum
        # study sets use_real_pit_view=True; run() then rebuilds a real
        # per-bar PitView before each signal.compute call (ADR 0016 PR 2b).
        self._use_real_pit_view = use_real_pit_view
        self._pit_view: PitView = _NoopPitView()
        self._cost_estimator: PreTradeCostEstimator
        if cost_estimator is None:
            self._cost_estimator = _NoopCostEstimator()
        else:
            self._cost_estimator = cost_estimator
        self._impacted_source = impacted_source
        self._apply_impact_to_valuation = apply_permanent_impact_to_valuation
        # Per ADR 0005 lock #12 + ADR 0012 lock #7 the timing instrumentation
        # is opt-in via `enable_timing: bool = False` so production backtests
        # pay zero cost. When enabled, per-step `time.perf_counter()` deltas
        # accumulate into a dict; `timing_breakdown()` returns the dict
        # sorted by step name. Per ADR 0012 lock #7 timing values are
        # explicitly OUT of the determinism invariant (timing != bit-
        # identical outputs); the sorted-list return value preserves the
        # sorted-iteration discipline from `docs/methodology/determinism.md`.
        self._enable_timing = enable_timing
        self._timing: dict[str, float] = {}

    @property
    def state(self) -> PortfolioState:
        """Expose the live state for test assertions; mutate at your peril."""
        return self._state

    def timing_breakdown(self) -> list[tuple[str, float]]:
        """Per-step timing accumulator as a sorted list.

        Per ADR 0005 lock #12 and ADR 0012 lock #7, this method is
        meaningful only when the BarLoop was constructed with
        `enable_timing=True`; otherwise the accumulator is empty and
        an empty list is returned.

        Returns a list of (step_name, total_seconds) tuples sorted by
        step name. Sorted-list (not insertion-ordered dict) preserves
        the determinism doc's sorted-iteration convention; timing
        values themselves are explicitly OUT of the Requirement 5
        bit-identical-output invariant per ADR 0012 lock #7.

        Step buckets at v1:
        - "preload": initial data load + per-bar index building
        - "signal": Signal.compute aggregated across bars
        - "policy": Policy.target_positions aggregated across bars
        - "matcher": MatchingEngine.submit aggregated across bars
        - "snapshot": per-bar mark-to-market + equity-curve build

        The buckets do not partition the per-bar sequence completely;
        clock advancement, dividend crediting, and price fetches are
        not bucketed at v1 (they are sub-millisecond per bar on the M2
        constant-weight demo). A future revision may add finer-grained
        buckets behind a separate flag.
        """
        return sorted(self._timing.items())

    def run(self, start_dt: date, end_dt: date) -> ConstantWeightDemoResult:
        # Per ADR 0012 lock #7 timing instrumentation is opt-in via
        # self._enable_timing. Default-off paths use direct `if ...`
        # guards (not context managers) so the disabled path is a
        # single attribute access + boolean test per instrumented
        # site, effectively zero cost on production backtests.
        if self._enable_timing:
            _t_preload = time.perf_counter()
        # Pre-load prices and dividends for all strategy tickers over the window.
        # Both engine and reference function read from these same eager Polars
        # frames (already sorted by dt by SharadarDataSource); no LazyFrame plan
        # re-ordering can drift the reductions.
        prices_by_asset: dict[AssetId, pl.DataFrame] = {}
        dividends_by_asset: dict[AssetId, pl.DataFrame] = {}
        for ticker in self._tickers:
            ticker_str = self._asset_id_to_ticker(ticker)
            prices_by_asset[ticker] = self._data_source.read_sep_prices(
                ticker=ticker_str, start_dt=start_dt, end_dt=end_dt
            )
            dividends_by_asset[ticker] = self._data_source.read_actions_dividends(
                ticker=ticker_str, start_dt=start_dt, end_dt=end_dt
            )

        # Build per-bar lookup indexes. Iteration over the resulting dicts is
        # always wrapped in sorted(...) at consumption time.
        # price_at retains M1's closeunadj-based valuation/fill price for
        # CLOSE-model orders; bar_at carries the full OHLCV tuple per ADR
        # 0009 lock #5 so the matcher can resolve OPEN/CLOSE/ARRIVAL
        # arrivals against the real bar values.
        price_at: dict[tuple[AssetId, date], float] = {}
        bar_at: dict[
            tuple[AssetId, date],
            tuple[float, float, float, float, float, int],
        ] = {}
        for ticker in sorted(prices_by_asset.keys()):
            frame = prices_by_asset[ticker]
            for row in frame.iter_rows(named=True):
                price_at[(ticker, row["dt"])] = float(row["closeunadj"])
                bar_at[(ticker, row["dt"])] = (
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["closeunadj"]),
                    int(row["volume"]),
                )

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
                "tickers": ",".join(
                    self._asset_id_to_ticker(t) for t in self._tickers
                ),
                "start_dt": start_dt.isoformat(),
                "end_dt": end_dt.isoformat(),
                "initial_capital": f"{self._initial_capital:.2f}",
                "n_trading_days": len(trading_days_in_window),
                "impact_aware_valuation": str(
                    self._impacted_source is not None
                    and self._apply_impact_to_valuation
                ),
            },
        )

        equity_curve_rows: list[dict[str, object]] = []
        n_rebalances = 0
        # last_close_raw_by_ticker tracks the previous bar's RAW closeunadj
        # per ticker. The next bar's MarketState.prior_close is derived
        # from this (impact-adjusted at MarketState construction time if
        # impacted_source is not None). Empty on the first bar; first-bar
        # ARRIVAL fills are not supported per ADR 0009 lock #4.
        last_close_raw_by_ticker: dict[AssetId, float] = {}

        if self._enable_timing:
            self._timing["preload"] = time.perf_counter() - _t_preload

        for bar_dt in trading_days_in_window:
            self._clock.advance_to(bar_dt)
            now = self._clock.now()

            # Per ADR 0009 lock #6 the matcher's per-bar reset hook fires
            # immediately after clock advancement so the matcher's _now
            # accessors see the advanced clock.
            self._matching_engine.on_bar_start(bar_dt)

            # Step 2: credit dividends from end-of-prior-bar shares.
            bar_divs = divs_at.get(bar_dt, {})
            for ticker in sorted(bar_divs.keys()):
                div = bar_divs[ticker]
                shares = self._state.positions.get(ticker, 0.0)
                if shares != 0.0:
                    self._state.cash += shares * div

            # Step 3: today's prices for this strategy's tickers (raw
            # closeunadj per M1 convention).
            prices_today: dict[AssetId, float] = {}
            for ticker in self._tickers:
                key = (ticker, bar_dt)
                if key in price_at:
                    prices_today[ticker] = price_at[key]

            # Rebuild the per-bar PitView for signals that consume history
            # (ADR 0016 PR 2b). The closure captures bar_dt and slices each
            # served table to available_dt < bar_dt (strict). The no-op
            # stand-in is kept for the constant-weight signal that ignores
            # pit_view, so the M1/M2 demos pay nothing.
            if self._use_real_pit_view:
                self._pit_view = self._build_pit_view(bar_dt)

            # Step 4: signal.
            if self._enable_timing:
                _t_step = time.perf_counter()
            signal_output = self._signal.compute(self._universe, now, self._pit_view)
            if self._enable_timing:
                self._timing["signal"] = self._timing.get("signal", 0.0) + (
                    time.perf_counter() - _t_step
                )

            # Step 5: policy. Per ADR 0009 lock #5 the cost_estimator is
            # the real cost model when the BarLoop was constructed with
            # one; otherwise the no-op stand-in.
            if self._enable_timing:
                _t_step = time.perf_counter()
            targets = self._policy.target_positions(
                signal_output=signal_output,
                current_positions=self._state,
                cost_estimator=self._cost_estimator,
                dt=now,
            )
            if self._enable_timing:
                self._timing["policy"] = self._timing.get("policy", 0.0) + (
                    time.perf_counter() - _t_step
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
                        order_id=(
                            f"{bar_dt.isoformat()}_"
                            f"{self._asset_id_to_ticker(ticker)}"
                        ),
                        asset_id=ticker,
                        quantity=Decimal(repr(qty)),
                        fill_price_model=FillPriceModel.CLOSE,
                        submit_dt=now,
                    )

                    market_state = self._build_market_state(
                        ticker=ticker,
                        bar_dt=bar_dt,
                        now=now,
                        bar_at=bar_at,
                        last_close_raw_by_ticker=last_close_raw_by_ticker,
                    )
                    if market_state is None:
                        continue
                    if self._enable_timing:
                        _t_step = time.perf_counter()
                    fills = self._matching_engine.submit(order, market_state)
                    if self._enable_timing:
                        self._timing["matcher"] = self._timing.get("matcher", 0.0) + (
                            time.perf_counter() - _t_step
                        )
                    for fill in fills:
                        fill_qty = float(fill.quantity)
                        fill_price = float(fill.fill_price)
                        commission_dollars = float(fill.commission)
                        # Cash flow per ADR 0009 lock #9: shares * fill_price
                        # plus commission (always positive). The matcher's
                        # signed-share convention is preserved so a sell
                        # with negative qty produces a positive cash inflow
                        # via the qty * price term (offset by the positive
                        # commission outflow).
                        self._state.cash -= fill_qty * fill_price + commission_dollars
                        self._state.positions[fill.asset_id] = (
                            self._state.positions.get(fill.asset_id, 0.0) + fill_qty
                        )

            # Step 7: snapshot at today's close. Per ADR 0009 lock #2 the
            # snapshot routes prices through adjust_price when the impact
            # source is wired AND the valuation policy is ON (default).
            if self._enable_timing:
                _t_step = time.perf_counter()
            nav_close = self._state.cash
            for ticker in sorted(self._state.positions.keys()):
                shares = self._state.positions[ticker]
                if shares == 0.0 or ticker not in prices_today:
                    continue
                raw_close = prices_today[ticker]
                if (
                    self._impacted_source is not None
                    and self._apply_impact_to_valuation
                ):
                    impacted_close_decimal = self._impacted_source.adjust_price(
                        ticker, Decimal(repr(raw_close))
                    )
                    nav_close += shares * float(impacted_close_decimal)
                else:
                    nav_close += shares * raw_close

            curve_row: dict[str, object] = {
                "dt": bar_dt,
                "cash": self._state.cash,
                "nav": nav_close,
            }
            for ticker in self._tickers:
                curve_row[f"shares_{ticker}"] = self._state.positions.get(ticker, 0.0)
            equity_curve_rows.append(curve_row)
            if self._enable_timing:
                self._timing["snapshot"] = self._timing.get("snapshot", 0.0) + (
                    time.perf_counter() - _t_step
                )

            # Step 9: track the raw closeunadj per ticker for the next
            # bar's prior_close population.
            for ticker in self._tickers:
                key = (ticker, bar_dt)
                if key in price_at:
                    last_close_raw_by_ticker[ticker] = price_at[key]

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
            tickers=tuple(self._asset_id_to_ticker(t) for t in self._tickers),
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

    def _build_market_state(
        self,
        *,
        ticker: AssetId,
        bar_dt: date,
        now: datetime,
        bar_at: Mapping[
            tuple[AssetId, date],
            tuple[float, float, float, float, float, int],
        ],
        last_close_raw_by_ticker: Mapping[AssetId, float],
    ) -> MarketState | None:
        """Construct the MarketState for a (ticker, bar_dt) pair.

        Returns None if the bar is missing from the SEP frame (the BarLoop
        skips orders for missing bars in step 6). Routes open/high/low/
        close through adjust_price when impacted_source is wired so the
        matcher sees the impacted view of the bar.

        Per ADR 0009 lock #14 construction is keyword-only.
        """
        bar_tuple = bar_at.get((ticker, bar_dt))
        if bar_tuple is None:
            return None
        raw_open, raw_high, raw_low, _raw_close, raw_closeunadj, raw_volume = bar_tuple

        # M1 uses closeunadj for the close field; M2 preserves this so the
        # Layer 2 zero-cost invariant equals the M1 baseline at 1e-10. The
        # split-adjusted SEP open/high/low fields are passed through as-is
        # at M2; for the M2 demo universe (SPY/AGG/GLD, no splits in the
        # observed windows) split-adjusted equals unadjusted within sub-bp
        # precision. M3 will refine the split-adjustment story properly.
        open_d = Decimal(repr(raw_open))
        high_d = Decimal(repr(raw_high))
        low_d = Decimal(repr(raw_low))
        close_d = Decimal(repr(raw_closeunadj))

        if self._impacted_source is not None:
            open_d = self._impacted_source.adjust_price(ticker, open_d)
            high_d = self._impacted_source.adjust_price(ticker, high_d)
            low_d = self._impacted_source.adjust_price(ticker, low_d)
            close_d = self._impacted_source.adjust_price(ticker, close_d)

        prior_close_d: Decimal | None
        if ticker in last_close_raw_by_ticker:
            prior_close_raw_d = Decimal(repr(last_close_raw_by_ticker[ticker]))
            if self._impacted_source is not None:
                prior_close_d = self._impacted_source.adjust_price(
                    ticker, prior_close_raw_d
                )
            else:
                prior_close_d = prior_close_raw_d
        else:
            prior_close_d = None

        return MarketState(
            asset_id=ticker,
            dt=now,
            open=open_d,
            high=high_d,
            low=low_d,
            close=close_d,
            volume=raw_volume,
            prior_close=prior_close_d,
        )

    def _build_pit_view(self, bar_dt: date) -> PitView:
        """Build the per-bar PitView the M5 momentum signal consumes.

        Serves three tables, each sliced to available_dt < bar_dt (strict),
        matching `signal/base.py`'s "available_dt < dt" contract and what
        `Momentum12_1Signal.compute` reads:
        - `sep`: the SEP `date` (the bar date) is the available_dt; aliased to
          `dt` and cast to `pl.Date`; carries `closeunadj` + `ticker`.
        - `actions`: the ACTIONS `date` (the ex-date) is the available_dt;
          served as the RAW vendor columns `ticker`/`date`/`action`/`value`
          (NOT the `read_actions_dividends` ex_date/amount projection).
        - `tickers`: identifier reference data with no per-row available_dt,
          so it is served as the full table (the signal's own
          firstpricedate <= dt <= lastpricedate interval test is the gate).
          `firstpricedate` and `lastpricedate` MUST be cast to `pl.Date`: the
          real bundle stores them as Datetime[ns], and the signal's row
          iteration compares them to a `date`, which would raise TypeError on
          a datetime (a real-bundle defect synthetic fixtures with pl.Date
          columns would mask).

        Cast every date column to `pl.Date` BEFORE the `< bar_dt` filter
        (project rule 12; the silent-empty Datetime[ns]-vs-date trap). The
        closure is rebuilt per bar (Determinism Requirement 3); it captures
        `bar_dt` by value.
        """
        sep_lf = self._data_source.get_table("sep").with_columns(
            pl.col("date").cast(pl.Date)
        )
        actions_lf = self._data_source.get_table("actions").with_columns(
            pl.col("date").cast(pl.Date)
        )
        tickers_lf = self._data_source.get_table("tickers").with_columns(
            pl.col("firstpricedate").cast(pl.Date),
            pl.col("lastpricedate").cast(pl.Date),
        )

        def pit_view(table_name: str) -> pl.LazyFrame:
            if table_name == "sep":
                return sep_lf.filter(pl.col("date") < bar_dt).select(
                    pl.col("date").alias("dt"),
                    pl.col("closeunadj").cast(pl.Float64),
                    pl.col("ticker"),
                )
            if table_name == "actions":
                return actions_lf.filter(pl.col("date") < bar_dt).select(
                    pl.col("ticker"),
                    pl.col("date"),
                    pl.col("action"),
                    pl.col("value").cast(pl.Float64),
                )
            if table_name == "tickers":
                return tickers_lf.select(
                    pl.col("permaticker").cast(pl.Int64),
                    pl.col("ticker"),
                    pl.col("firstpricedate"),
                    pl.col("lastpricedate"),
                )
            raise KeyError(
                f"unknown pit_view table {table_name!r}; the momentum signal "
                f"consumes only 'sep', 'actions', 'tickers'"
            )

        return pit_view
