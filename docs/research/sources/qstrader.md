# QSTrader Analysis

Repository: https://github.com/mhallsmoore/qstrader  
Cloned commit: `4c59e15` (v0.3.0, shallow clone, 2026-05-28)  
Source tree root: `C:/temp/qstrader/`  

---

## Executive Summary

- **Maintenance state**: Nominally active (v0.3.0 released with numpy 2.0 compatibility), but the commit history is extremely thin (one commit visible in the shallow clone). Changes since 2022 have been almost entirely dependency-bump housekeeping. The framework is best treated as a stable reference implementation, not a living project with responsive maintainers.
- **Textbook event architecture**: QSTrader is the canonical Python implementation of the four-component event-driven backtest loop described in the QuantStart blog series (MarketEvent, SignalEvent, OrderEvent, FillEvent). The current codebase has evolved toward a schedule-driven, weight-based approach, but the structural separation of DataHandler, AlphaModel, PortfolioConstructionModel, and ExecutionHandler maps directly onto the literature.
- **Biggest strength**: Conceptual clarity. Every component has a single, narrow responsibility and a documented interface. Reading the source alongside the QuantStart article series is one of the fastest ways to understand event-driven backtesting architecture from first principles.
- **Biggest weakness**: The cost and execution models are explicitly incomplete stubs. `slippage_model` and `market_impact_model` are hard-coded to `None` with `TODO: Implement` comments (`simulated_broker.py:67-68`). The bid/ask spread is set to zero because the bar data gives a single mid price (`daily_bar_csv.py:175-176`). Running a real strategy through this without adding your own models will systematically overstate returns.
- **Key lessons for a production system**: Adopt the four-handler separation and the schedule-based sim engine concept; reject the single-threaded Python loop as a performance model; and treat every unimplemented `TODO` in QSTrader as a flag that marks a real problem your design must solve explicitly.

---

## Project Status and Maintenance

QSTrader is authored by Michael Halls-Moore, founder of QuantStart. The repo is hosted at `github.com/mhallsmoore/qstrader`. A mirror also exists at `github.com/quantstart/qstrader`.

The changelog (`CHANGELOG.md`) shows a clear trajectory:

- `v0.1.x` through `v0.2.x`: structural work (long/short support added in `0.2.0`, burn-in period, tearsheet saves, Hatchling build backend)
- `v0.2.4` through `v0.3.0`: almost entirely dependency and Python-version compatibility updates

The `git log` on the shallow clone shows a single commit (`4c59e15`). There is no evidence of significant architectural work since the `0.2.0` rewrite that added long/short portfolios.

The README states: "The software is currently under active development." Treat this as aspirational. The framework is mature and stable for its intended scope (daily-bar equity and ETF strategy research), but it is not actively extended by a large team. Contributions come from community volunteers.

This is primarily an educational and reference implementation. The QuantStart blog series "Event-Driven Backtesting with Python" (Parts I-VI) and the "Advanced Algorithmic Trading" ebook are the primary consumers of this codebase. Production trading firms do not deploy QSTrader directly.

---

## Architecture

### The Event Model: From Classic Four-Event to Schedule-Driven Weights

The QuantStart blog series described the canonical four-event model:

1. **MarketEvent**: fired when new bar data arrives; triggers Strategy
2. **SignalEvent**: fired by Strategy to indicate LONG/SHORT intent
3. **OrderEvent**: fired by Portfolio, includes sizing
4. **FillEvent**: fired by ExecutionHandler after order is transacted

The current QSTrader codebase has evolved away from an explicit in-memory event queue for the strategy loop. Instead it uses a **schedule-driven, weight-based** approach. The `SimulationEvent` object (`simulation/event.py`) carries only a timestamp and an event type string such as `"pre_market"`, `"market_open"`, or `"market_close"`. There is no `SignalEvent` or `FillEvent` class in the current source tree. The portfolio construction and execution layers communicate through Python function calls and return values, not a shared queue.

```python
# qstrader/simulation/event.py:1-16
class SimulationEvent(object):
    """
    Stores a timestamp and event type string associated with
    a simulation event.
    """
    def __init__(self, ts, event_type):
        self.ts = ts
        self.event_type = event_type
```

The `SimulationEngine` abstract base class (`simulation/sim_engine.py:4-28`) defines `__iter__` as the contract. Subclasses yield `SimulationEvent` objects, making the engine a Python generator. The main backtest loop consumes these events in a `for` loop.

### Queue-Based Dispatch: The Sim Engine Generator

The simulation engine for daily data (`simulation/daily_bday.py`) is a generator that produces four events per business day (pre-market, open, close, post-market). The `BacktestTradingSession.run()` method is the central event loop:

```python
# qstrader/trading/backtest.py:384-415
for event in self.sim_engine:
    dt = event.ts

    # Update the simulated broker
    self.broker.update(dt)

    # Update any signals on a daily basis
    if self.signals is not None and event.event_type == "market_close":
        self.signals.update(dt)

    # If we have hit a rebalance time then carry
    # out a full run of the quant trading system
    if self.burn_in_dt is not None:
        if dt >= self.burn_in_dt:
            if self._is_rebalance_event(dt):
                self.qts(dt, stats=stats)
    else:
        if self._is_rebalance_event(dt):
            self.qts(dt, stats=stats)

    # Out of market hours we want a daily performance update
    if event.event_type == "market_close":
        if self.burn_in_dt is not None:
            if dt >= self.burn_in_dt:
                self._update_equity_curve(dt)
        else:
            self._update_equity_curve(dt)
```

This is the entire dispatch mechanism. There is no Python `queue.Queue` in the hot path. The `SimulatedBroker` does maintain a `queue.Queue` per portfolio for pending orders (`simulated_broker.py:339`), but that queue is drained synchronously within `broker.update(dt)` during the same bar iteration.

### AlphaModel / PortfolioConstructionModel / ExecutionHandler Separation

The `QuantTradingSystem` (`system/qts.py`) is the facade that wires together three major components:

**AlphaModel** (`alpha_model/alpha_model.py:4-25`): a single `__call__(dt)` method returning a dict of `{asset: scalar_signal}`. The scalar is a raw forecast weight, not a direction. This is a cleaner abstraction than the original blog-series `Strategy` class that fired explicit `SignalEvent` objects.

**PortfolioConstructionModel** (`portcon/pcm.py`): takes alpha signals, runs them through an optional risk model and optimiser, converts target weights to integer share quantities via an `OrderSizer`, diffs against current holdings, and returns a list of `Order` objects.

```python
# qstrader/portcon/pcm.py:259-302
def __call__(self, dt, stats=None):
    if self.alpha_model:
        weights = self.alpha_model(dt)
    else:
        weights = self._create_zero_target_weights_vector(dt)

    if self.risk_model:
        weights = self.risk_model(dt, weights)

    optimised_weights = self.optimiser(dt, initial_weights=weights)

    full_assets = self._obtain_full_asset_list(dt)
    full_zero_weights = self._create_zero_target_weight_vector(full_assets)
    full_weights = self._create_full_asset_weight_vector(
        full_zero_weights, optimised_weights
    )

    target_portfolio = self._generate_target_portfolio(dt, full_weights)
    current_portfolio = self._obtain_current_portfolio()
    rebalance_orders = self._generate_rebalance_orders(
        dt, target_portfolio, current_portfolio
    )
    # TODO: Implement cost model
    return rebalance_orders
```

Note the explicit `# TODO: Implement cost model` at line 300. Transaction cost is not factored into order sizing decisions. This is a real gap for any strategy sensitive to trading costs.

**ExecutionHandler** (`execution/execution_handler.py`): receives the list of `Order` objects and forwards them to the `SimulatedBroker`. The only built-in execution algorithm is `MarketOrderExecutionAlgorithm` (`execution/execution_algo/market_order.py`), which simply passes orders through unchanged. There is no TWAP, VWAP, or limit-order simulation.

```python
# qstrader/execution/execution_algo/market_order.py:10-26
class MarketOrderExecutionAlgorithm(ExecutionAlgorithm):
    """
    Simple execution algorithm that creates an unmodified list
    of market Orders from the rebalance Orders.
    """
    def __call__(self, dt, initial_orders):
        return initial_orders
```

The `QuantTradingSystem.__call__` method (`system/qts.py:154-175`) is the glue:

```python
def __call__(self, dt, stats=None):
    rebalance_orders = self.portfolio_construction_model(dt, stats=stats)
    self.execution_handler(dt, rebalance_orders)
```

Clean, but notice that execution is synchronous and immediate. There is no overnight order queue, no partial fill logic, no order rejection.

---

## Lookahead Bias Protection

### Event Timing Semantics

The `DailyBusinessDaySimulationEngine` yields events in the following sequence for each business day (all UTC):

```
pre_market    00:00
market_open   14:30  (09:30 ET)
market_close  21:00  (16:00 ET)
post_market   23:59
```

(`simulation/daily_bday.py:76-107`)

In `BacktestTradingSession`, the `signals.update(dt)` call is gated to `event.event_type == "market_close"` (`backtest.py:394-395`). This is the intended sequence: signals see a bar only after its close timestamp has been reached. Correct in principle.

**The critical weakness** is in how bar prices are served. The `CSVDailyBarDataSource` ingests OHLCV CSV data, then produces a bid/ask frame with two rows per calendar day: one at 14:30 (open price) and one at 21:00 (close price) (`daily_bar_csv.py:170-171`). Price lookup uses pandas `get_indexer` with `method='pad'` (forward-fill):

```python
# qstrader/data/daily_bar_csv.py:217-218
bid_ask_df = self.asset_bid_ask_frames[asset]
bid_series = bid_ask_df.iloc[bid_ask_df.index.get_indexer([dt], method='pad')]['Bid']
```

Forward-fill means: "give me the last known price at or before this timestamp." When `broker.update(dt)` is called with `dt = market_open timestamp (14:30)`, positions are marked to the open price. When the strategy fires at the rebalance timestamp (which by default is a `market_close` timestamp), it prices orders at the close. This is an "open-to-close" execution model with no explicit protection against trading on the same bar whose close price you used to generate the signal. Whether that constitutes lookahead bias depends on your rebalance frequency and strategy logic. For daily strategies that generate signals on the close and execute on the next open, QSTrader does not natively enforce the next-bar execution constraint.

**The bid/ask spread is zero by design**:

```python
# qstrader/data/daily_bar_csv.py:173-176
# TODO: Unable to distinguish between Bid/Ask, implement later
dp_df['Bid'] = dp_df['Price']
dp_df['Ask'] = dp_df['Price']
```

Buy and sell execution both see the same mid price. For large-cap ETFs with tight spreads this is a minor issue. For small-caps or any fixed-income instrument it is a meaningful cost omission.

**Holiday calendar**: The `SimulatedExchange` hardcodes NYSE hours (14:30-21:00) but does not implement any holiday calendar (`exchange/simulated_exchange.py:24-52`). US federal holidays are not skipped. Backtests covering periods with market closures will attempt to execute on holidays.

```python
# qstrader/exchange/simulated_exchange.py:24-25
# TODO: Eliminate hardcoding of NYSE
# TODO: Make these timezone-aware
```

---

## Corporate Actions and Survivorship

QSTrader's corporate action handling is entirely delegated to the data provider. The `CSVDailyBarDataSource` does apply a price adjustment when an `Adj Close` column is present (`daily_bar_csv.py:149-162`):

```python
if self.adjust_prices:
    if 'Adj Close' not in bar_df.columns:
        raise ValueError(
            "Unable to locate Adjusted Close pricing column in CSV data file. "
            "Prices cannot be adjusted. Exiting."
        )
    oc_df['Adj Open'] = (oc_df['Adj Close'] / oc_df['Close']) * oc_df['Open']
```

This correctly back-adjusts the open price using the close/adj-close ratio. Split and dividend adjustments that are embedded in the Yahoo Finance CSV format will flow through.

**What is not handled**:

- Spin-offs, mergers, delistings: if a ticker disappears from the CSV, it simply stops pricing. There is no delisting event, no forced liquidation at a final price.
- Survivorship bias: QSTrader has no concept of a historical universe. The `StaticUniverse` contains whatever tickers you pass in at construction time (`asset/universe/static.py`). If you build a universe of current S&P 500 members and backtest to 2005, you are running a survivorship-biased test. The `DynamicUniverse` exists but carries a comment: "TODO: This does not currently support removal of assets" (`asset/universe/dynamic.py:9`).
- Dividends as cash events: dividend adjustment is folded into price. There is no separate cash dividend receipt event, which matters for strategies that explicitly model income.

---

## Cost and Execution Modeling

### FeeModel Abstraction

The `FeeModel` abstract base class (`broker/fee_model/fee_model.py`) defines three abstract methods: `_calc_commission`, `_calc_tax`, `calc_total_cost`. Two concrete implementations are provided:

- `ZeroFeeModel`: returns zero for all costs. This is the **default** in `BacktestTradingSession` (`backtest.py:82`).
- `PercentFeeModel`: applies a percentage commission and tax to the consideration (`broker/fee_model/percent_fee_model.py`). The formula is `commission_pct * abs(consideration)`, which is a round-trip percentage model, correct for markets that charge per-notional (UK stamp duty, for example). For US equities with a per-share commission schedule, you would need to subclass `FeeModel`.

```python
# qstrader/broker/fee_model/percent_fee_model.py:44-45
def _calc_commission(self, asset, quantity, consideration, broker=None):
    return self.commission_pct * abs(consideration)
```

### Slippage and Market Impact: Stubs

Both are declared as accepted parameters of `SimulatedBroker.__init__` but immediately discarded:

```python
# qstrader/broker/simulated_broker.py:67-68
self.slippage_model = None  # TODO: Implement
self.market_impact_model = None  # TODO: Implement
```

No slippage model of any kind (fixed spread, volume-proportional, square-root) is applied. Orders always execute at the exact current mid price. For a daily ETF momentum strategy with low turnover and liquid instruments, this is a minor flaw. For anything with meaningful turnover or smaller-cap exposure, the omission is fatal to result validity.

### Order Types

Only market orders are implemented (`execution/execution_algo/market_order.py`). Limit orders, stop orders, and IOC orders are not modeled. The `Order` class (`execution/order.py`) has `quantity` and `direction` but no `order_type` field and no price limit field. The execution algo directory contains only `market_order.py`:

```
qstrader/execution/execution_algo/
  execution_algo.py   (abstract base)
  market_order.py     (only concrete implementation)
```

### Order Sizing

Two order sizers are provided:

- `DollarWeightedCashBufferedOrderSizer`: for long-only portfolios, converts target weights to integer share counts with a configurable cash buffer percentage.
- `LongShortLeveragedOrderSizer`: for long/short portfolios, uses a gross leverage constraint to scale weights.

Both compute integer share quantities by dividing dollar allocation by latest mid price. Neither accounts for lot sizes, margin requirements, or borrowing costs on short positions.

---

## Performance and Scaling

### Single-Process Python Loop

The backtest loop is a synchronous Python `for` loop over a generator. Every business day calls:

1. `broker.update(dt)`: marks-to-market all positions by iterating over every asset in every portfolio
2. `signals.update(dt)`: appends prices to deque buffers
3. `qts(dt)`: runs AlphaModel, Optimiser, OrderSizer, diffs portfolio, submits orders, drains order queue

All of this happens sequentially, in one thread, in one process. There is no parallelism, no vectorized bar computation. For a 20-year daily backtest over 20 ETFs, this is fast enough (seconds). For 500 stocks over minute bars, it would be prohibitively slow.

The `CSVDailyBarDataSource` uses `@functools.lru_cache(maxsize=1024 * 1024)` on individual price lookups (`daily_bar_csv.py:200`), which avoids repeated DataFrame index operations but does not change the fundamental O(n_assets x n_bars) complexity.

### Data Loading

All CSV data is loaded and converted to bid/ask frames at construction time (`daily_bar_csv.py:41-42`). For large universes this can consume substantial memory. There is no lazy loading, no chunked reading, and no database backend.

### No Intraday Support in the Current Architecture

The `BacktestTradingSession._create_simulation_engine()` is hardcoded to `DailyBusinessDaySimulationEngine` with a `TODO: Currently hardcoded to daily events` note (`backtest.py:222`). Sub-daily backtesting requires writing a new `SimulationEngine` subclass and likely a new `DataHandler`.

---

## Strengths

**Conceptual fidelity**: The four-component separation (alpha, portfolio construction, execution, broker) maps precisely onto the academic and practitioner literature on systematic trading systems. Reading this code alongside Halls-Moore's "Advanced Algorithmic Trading" ebook is genuinely educational. The original QuantStart blog series "Event-Driven Backtesting with Python" remains one of the clearest expositions of the event loop architecture.

**Rebalance schedule as a first-class citizen**: The `_create_rebalance_event_times()` factory (`backtest.py:233-259`) supports buy-and-hold, daily, weekly, and end-of-month schedules out of the box. This is the right abstraction for systematic equity strategies.

**Price adjustment is on by default**: `adjust_prices=True` in `CSVDailyBarDataSource` means corporate actions are handled automatically if the data provider supplies an `Adj Close` column.

**Burn-in period**: The `burn_in_dt` parameter (`backtest.py:399`) lets signals warm up before the strategy starts trading and before equity tracking begins. This is a correct design that prevents cold-start artifacts in momentum-type signals.

**Clean abstract interfaces**: `AlphaModel`, `FeeModel`, `ExecutionAlgorithm`, `SimulationEngine` are all abstract base classes with narrow contracts. Subclassing any of them is straightforward.

**MIT license**: No restrictions on commercial use or modification.

---

## Weaknesses, Footguns, and Critiques

**1. Slippage and market impact are not implemented.** This is the most consequential gap. The comment `self.slippage_model = None # TODO: Implement` (`simulated_broker.py:67`) has been in the codebase through multiple version bumps. Any backtest result from vanilla QSTrader is an upper bound on real-world performance, not a realistic estimate. For a strategy with 5% monthly turnover on liquid ETFs the gap may be small. For anything else, it is not.

**2. Zero bid/ask spread.** `daily_bar_csv.py:175-176` sets `Bid = Ask = Price`. Buys and sells both execute at the same mid. This is not the real world.

**3. Transaction cost model is disconnected from portfolio construction.** The `PortfolioConstructionModel.__call__` ends with `# TODO: Implement cost model` (`pcm.py:300`). Target weights are computed ignoring trading costs. A strategy with high turnover will be ordered to trade even when the net benefit of rebalancing is negative after costs. Correct systems fold an expected cost estimate into the optimisation objective.

**4. No holiday calendar.** The `SimulatedExchange` treats every Monday-Friday as a trading day. NYSE is closed roughly 9 days per year for federal holidays. A 20-year backtest will attempt to trade on approximately 180 non-trading days. The exchange's `is_open_at_datetime` check (`exchange/simulated_exchange.py:29-52`) will correctly block order execution during those periods (since the exchange is "closed"), but signals and broker mark-to-market still fire, producing spurious entries in the equity curve.

**5. Survivorship bias in the universe model.** `DynamicUniverse` explicitly does not support asset removal (`asset/universe/dynamic.py:9`). Any historical backtest is likely to contain tickers that survived to the present day, systematically biasing results upward.

**6. Only daily bar data, only equities, only CSV.** The data handler comment says it plainly: `# TODO: Only equities are supported by QSTrader for now.` (`backtest.py:187`). There is no built-in support for options, futures, fixed income, FX, or tick data through any production data vendor API.

**7. Only market orders.** The execution algo layer has a single implementation. Realistic simulation of open orders, limit fills, partial fills, and order cancellation is absent.

**8. Maintenance is effectively stalled.** With one commit in the shallow clone and a changelog showing only dependency bumps since `0.2.0`, the probability of significant new features is low. Open issues on GitHub are unlikely to receive responses quickly.

**9. The classic blog-series version (the `advanced-algorithmic-trading` branch) is architecturally different.** Many tutorials and Stack Overflow answers describe the older `queue.Queue`-based four-event architecture. The current `master` branch uses a completely different schedule-driven design. This is a source of real confusion for learners.

---

## Lessons for Our Design

### Things to Copy

**The four-component separation.** AlphaModel, PortfolioConstructionModel, ExecutionHandler, and Broker are the right conceptual seams. Each can be tested in isolation. Each can be swapped without touching the others. This separation directly mirrors the academic pipeline (signal generation, portfolio optimisation, execution, accounting).

**The SimulationEngine generator pattern.** Yielding typed `SimulationEvent` objects from a generator is a clean contract. Our system should similarly separate the "clock" (what events happen when) from the "handlers" (what each component does when it sees an event). Events should be strongly typed, not strings.

**Explicit rebalance schedules.** The `_create_rebalance_event_times()` factory separates "when does the strategy fire" from "what does the strategy do." This prevents a common bug where strategies silently fire on every bar.

**Burn-in period as a first-class concept.** Every signal-based strategy needs a warm-up period. Treating it as a parameter of the session rather than a magic constant in the strategy code is correct.

**Adjusted prices on by default.** Our system should default to price-adjusted data and require an explicit opt-out. The QSTrader implementation of ratio-adjusting the open price from the adj-close column (`daily_bar_csv.py:159-161`) is a useful reference.

### Things to Avoid

**Slippage as a TODO.** Do not ship without at least a configurable fixed-spread model and a half-spread model. Make the slippage model a required parameter with no `None` default.

**Zero bid/ask spread as the default.** The default execution price should include at minimum a configurable half-spread.

**A single-threaded Python loop as the performance model.** For production use with more than a few hundred assets or sub-daily bars, the main loop needs to be vectorized or parallelized. Consider vectorizing the mark-to-market step and the signal computation separately from the event dispatch.

**Hard-coded NYSE calendar.** Use `pandas_market_calendars` or `exchange_calendars` from the start. Retroactively adding calendar awareness is painful.

**String event types.** QSTrader uses `event.event_type == "market_close"` string comparisons throughout the dispatch logic (`backtest.py:394`). Use an `Enum` or a class hierarchy to make event dispatch type-safe and refactor-safe.

**Disconnected cost model.** The portfolio construction layer must receive a cost estimate when computing target weights. Separating "what do I want to hold" from "what does it cost to get there" produces systematically biased turnover decisions.

### Open Questions

- What is the correct lookahead-bias protection for strategies that generate signals on the close and should execute on the next open? QSTrader does not enforce this; our design must decide explicitly and enforce it through the simulation engine event sequence.
- For a production system targeting daily equities plus futures, how do we handle multiple calendar overlaps (NYSE + CME, for example) within the same event loop?
- Should the `AlphaModel` output raw forecasts (which the optimiser then converts to weights) or target weights directly? QSTrader conflates these in `FixedSignalsAlphaModel`. A cleaner separation would reserve the alpha layer for forecasts and put all weight math in the optimiser.

---

## Sources

1. **QSTrader GitHub repository (main)**: https://github.com/mhallsmoore/qstrader  
   Primary source. All file:line citations refer to the `4c59e15` shallow clone.

2. **QSTrader documentation page (QuantStart)**: https://www.quantstart.com/qstrader/  
   Official feature overview and design goals. Light on detail but describes target use cases.

3. **Event-Driven Backtesting with Python, Part I (QuantStart)**: https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-I/  
   Introduces the four-event model (MarketEvent, SignalEvent, OrderEvent, FillEvent) and the nested while-loop queue. The classic reference for understanding QSTrader's conceptual lineage.

4. **Event-Driven Backtesting with Python, Part II (QuantStart)**: https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-II/  
   Defines the four event classes and quotes the queue dispatch loop. Explains how MarketEvent timing works relative to bar arrival.

5. **Event-Driven Backtesting with Python, Part IV (QuantStart)**: https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-IV/  
   Portfolio handler class. Shows how SignalEvent is converted to OrderEvent with position sizing.

6. **Event-Driven Backtesting with Python, Part V (QuantStart)**: https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-V/  
   Execution handler and FillEvent. Describes commission and slippage hooks.

7. **Event-Driven Backtesting with Python, Part VI (QuantStart)**: https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-VI/  
   Performance statistics and tearsheet generation.

8. **Advanced Trading Infrastructure: Portfolio Handler Class (QuantStart)**: https://www.quantstart.com/articles/Advanced-Trading-Infrastructure-Portfolio-Handler-Class/  
   Describes the updated portfolio handler design used in the `advanced-algorithmic-trading` branch, which predates the current schedule-driven architecture.

9. **Backtesting Systematic Trading Strategies in Python: Considerations and Open Source Frameworks (QuantStart)**: https://www.quantstart.com/articles/backtesting-systematic-trading-strategies-in-python-considerations-and-open-source-frameworks/  
   Comparison article positioning QSTrader alongside Zipline and Backtrader. Notes event-driven slowness vs. vectorised approaches.

10. **QSTrader GitHub Issues**: https://github.com/mhallsmoore/qstrader/issues  
    Community-reported bugs and feature requests. Useful for understanding known limitations and maintenance responsiveness.

11. **QSTrader v0.2.6 Release Notes (QuantStart)**: https://www.quantstart.com/articles/qstrader-v026-released/  
    Changelog context for recent maintenance activity.

12. **Backtrader vs Zipline vs QuantConnect: Python Backtesting Platform Comparison 2026**: https://alphagaindaily.com/en/blog/backtrader-vs-zipline-vs-quantconnect  
    Third-party comparison article; confirms that event-driven per-bar Python loops are the primary performance bottleneck for all frameworks in this class.
