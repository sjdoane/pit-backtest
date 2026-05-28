# Vectorbt Analysis

Repo analyzed: https://github.com/polakowo/vectorbt (shallow clone, commit b213e07, dated 2026-04-23)
Version: 1.0.0 (`vectorbt/_version.py:4`)
License: Apache 2.0 with Commons Clause (fair-code, not truly OSS for commercial resale)

---

## Executive Summary

- **Two-tier product**: vectorbt (OSS, Apache 2.0 + Commons Clause, frozen at v1.0.0 as of April 2026) is the community edition; vectorbtpro is a commercial subscription (~$20/month, invite-only) that extends the same codebase with futures support, simulation chaining, CLI tooling, and active development. The OSS repo received a single refactoring commit in 2026.
- **Architecture choice**: the library is NOT event-driven in the traditional sense. It accepts arrays of signals or orders and processes them inside Numba-JIT-compiled kernels that iterate row-by-row and column-by-column. The author himself clarified in the public GitHub discussion (#185): "vectorbt doesn't actually do any vectorized backtesting, it follows a sequential approach just like most other libraries." The "vectorized" label refers to the API surface (inputs are NumPy/Pandas arrays), not the simulation mechanics.
- **Biggest strength**: hyperparameter sweep performance. Running 10,000 MA-crossover configurations across multiple assets is measured in seconds rather than hours because the hot path is JIT-compiled via Numba (and optionally Rust) and the entire parameter space is packed into a single array traversal.
- **Biggest weakness**: the signal-based API (`Portfolio.from_signals`) requires the user to manually shift signals by one bar (`signals.vbt.fshift(1)`) to avoid lookahead bias. This is not enforced by the engine; the docs warn about it in two places, but the primary documentation examples omit the shift. The risk of silent overfitting is high.
- **Key lesson for our design**: use Numba-accelerated vectorized simulation as a fast parameter-sweep mode alongside, not instead of, an event-driven core. The two serve different purposes: sweep mode for alpha research, event-driven mode for production-realistic validation.

---

## Project Status and Maintenance

**Author**: Oleg Polakow ("polakowo"), based in Germany. Solo project until commercialization.

**OSS status**: The GitHub repo (https://github.com/polakowo/vectorbt) shows a single commit in 2026 (`b213e07`, a benchmarks refactor, dated 2026-04-23). All substantive feature development has moved to vectorbtpro. The README itself links vectorbt as "the open-source community edition of VectorBT PRO." The OSS version version number is 1.0.0 (`vectorbt/_version.py`), reflecting stabilization rather than growth.

**What is paywalled in vectorbtpro** (sourced from https://vectorbt.pro/features/portfolio/ and https://vectorbt.pro/documentation/fundamentals/):

- Futures contract multiplier support (not modeled in OSS)
- Negative price handling across the full pipeline (relevant after April 2020 WTI crude event)
- Simulation chaining (pass final state to next run, enabling realistic rolling-window or live-continuation backtests)
- Stop laddering (incremental position exit)
- Faster large-scale testing and private Slack support
- CLI tooling and local embedding support (added in v2026.4.7)
- Full release notes are subscriber-only; the OSS changelog is not maintained

The OSS version is genuinely useful for signal research and parameter sweeps. For anything involving futures, simulation continuity, or production-adjacent workflows, users must pay.

**Community**: The Gitter chat linked from the README is inactive. GitHub issues and discussions remain open but responses from the author are slow. The EliteTrader forum thread "Industry standard backtest tool is vectorbt pro?" (https://www.elitetrader.com/et/threads/industry-standard-backtest-tool-is-vectorbt-pro.370565/) reflects mixed sentiment: practitioners praise the speed, but serious quants move to vectorbtpro or NautilusTrader once realism matters.

---

## Architecture

### Vectorized API, Sequential Simulation Kernel

The library's tagline is "Thinks in matrices. Backtests at scale." This is accurate for the API layer: users pass Pandas DataFrames of prices and NumPy arrays of signals. Internally, however, the simulation is strictly sequential.

From `vectorbt/portfolio/nb.py:2966-2977` (the docstring for `simulate_nb`, the most general kernel):

```python
def simulate_nb(
    target_shape: tp.Shape,
    group_lens: tp.Array1d,
    init_cash: tp.Array1d,
    cash_sharing: bool,
    call_seq: tp.Array2d,
    ...
) -> tp.Tuple[tp.RecordArray, tp.RecordArray]:
    """Fill order and log records by iterating over a shape and calling a range
    of user-defined functions.

    Starting with initial cash `init_cash`, iterates over each group and column
    in `target_shape`, and for each data point, generates an order using
    `order_func_nb`. Tries then to fulfill that order. Upon success, updates the
    current state including the cash balance and the position.

    As opposed to `simulate_row_wise_nb`, order processing happens in
    column-major order. Column-major order means processing the entire
    column/group with all rows before moving to the next one.
    """
```

The inner loop (from `nb.py:3395-3530`) is a nested `for i in range(target_shape[0])` (time rows) containing `for k in range(group_len)` (assets in a group). At each cell, an `order_func_nb` callback produces an `Order` named tuple; `process_order_nb` fills or rejects it and mutates `last_cash`, `last_position`, `last_debt` scalar state arrays.

### Three Simulation Modes

**`Portfolio.from_orders`** (`base.py:1617`, kernel: `nb.simulate_from_orders_nb` at `nb.py:1282`): the fastest mode. Every cell of the input arrays is treated as a pre-specified order. No branching logic. Suitable for replaying known order sequences or studying cost sensitivity.

**`Portfolio.from_signals`** (`base.py:2041`, kernel: `nb.simulate_from_signals_nb` at `nb.py:1844`): adds an abstraction layer. Entry/exit boolean arrays drive position changes. The kernel resolves conflicts (simultaneous entry and exit), applies stop-loss and take-profit logic, and enforces the one-order-per-bar-per-asset constraint. Signature excerpt from `nb.py:1844-1895`:

```python
def simulate_from_signals_nb(
    target_shape: tp.Shape,
    group_lens: tp.Array1d,
    init_cash: tp.Array1d,
    call_seq: tp.Array2d,
    entries: tp.ArrayLike = np.asarray(False),
    exits: tp.ArrayLike = np.asarray(False),
    direction: tp.ArrayLike = np.asarray(Direction.LongOnly),
    long_entries: tp.ArrayLike = np.asarray(False),
    long_exits: tp.ArrayLike = np.asarray(False),
    short_entries: tp.ArrayLike = np.asarray(False),
    short_exits: tp.ArrayLike = np.asarray(False),
    size: tp.ArrayLike = np.asarray(np.inf),
    price: tp.ArrayLike = np.asarray(np.inf),
    ...
    sl_stop: tp.ArrayLike = np.asarray(np.nan),
    sl_trail: tp.ArrayLike = np.asarray(False),
    tp_stop: tp.ArrayLike = np.asarray(np.nan),
    ...
) -> tp.Tuple[tp.RecordArray, tp.RecordArray]:
```

**`Portfolio.from_order_func`** (`base.py:3287`, kernel: `nb.simulate_nb` at `nb.py:2933`): the most flexible mode. User supplies a Numba-compiled `order_func_nb` callback invoked per (row, column) cell, plus optional `pre_segment_func_nb` and `post_order_func_nb` hooks. This is the closest vectorbt gets to event-driven backtesting. The docs claim it offers "less risk of exposure to the look-ahead bias" (`base.py:186`), which is true in the sense that the callback receives only current and past state through the `OrderContext` named tuple, but accessing future rows of the input arrays (e.g. `close[i+1, col]`) is neither prevented nor warned about (`nb.py:3076-3080`):

```
!!! warning
    You can only safely access data of columns that are to the left of the current
    group and rows that are to the top of the current row within the same group.
    Other data points have not been processed yet and thus empty. Accessing them
    will not trigger any errors or warnings, but provide you with arbitrary data
    (see np.empty).
```

The library provides no guardrails. The kernel will not error if user code reads future data; it will silently return whatever is in the uninitialized NumPy buffer.

### What "Vectorized Event-Driven" Means in Vectorbtpro

Vectorbtpro's marketing uses the phrase "hybrid backtesting." Based on the portfolio features page (https://vectorbt.pro/features/portfolio/), this means the same row-by-row sequential kernel augmented with: richer context objects, simulation chaining (carry-forward of portfolio state between runs), and execution price options (`nextopen`, `nextclose`) that eliminate the need for manual array shifting. The core loop architecture is identical to OSS; the "event-driven" claim is about callback granularity and lifecycle hooks, not a true event queue.

### Numba Decoration

All simulation kernels are decorated with `@njit(cache=True)` (from `nb.py:65`). Numba compiles the Python bytecode to LLVM IR on first call and caches the artifact. Subsequent calls bypass Python entirely. This is the source of vectorbt's speed advantage. The optional Rust engine (`vectorbt[rust]`) provides an alternative pre-compiled path without the JIT warmup cost.

---

## Lookahead Bias Protection

### The Shift Discipline

For `Portfolio.from_signals`, the library's documentation instructs users to shift their signal arrays by one bar when signals are computed using closing prices. From `base.py:2297-2300`:

```
!!! hint
    If you generated signals using close price, don't forget to shift your signals
    by one tick forward, for example, with `signals.vbt.fshift(1)`. In general,
    make sure to use a price that comes after the signal.
```

The same advice appears in `base.py:241` inside the main portfolio example, where it is actually followed:

```python
>>> result = result.vbt.fshift(1)
```

This is the correct pattern: compute a signal on bar N's close, shift forward so it acts at bar N+1's open. Without the shift, the signal computed on bar N's close executes at bar N's close, the same close used to generate it, which is lookahead.

### How Users Get Bitten

The problem is that `fshift(1)` is advisory, not enforced. The engine will happily accept an unshifted signals array and produce optimistic-looking results. A user who writes:

```python
entries = fast_ma.ma_crossed_above(slow_ma)
exits  = fast_ma.ma_crossed_below(slow_ma)
pf = vbt.Portfolio.from_signals(price, entries, exits)
```

...is executing at the same close used to compute the crossover. The resulting Sharpe ratio will be inflated. GitHub issue #190 (https://github.com/polakowo/vectorbt/issues/190) documents exactly this: the issue reporter noticed that the primary documentation example omitted `fshift(1)` while the text demanded it. The inconsistency was never resolved by adding enforcement; the maintainer simply noted it is the user's responsibility.

### Execution Price Defaults

When `price=np.inf` (the default for `from_orders`) the kernel resolves execution price as the current bar's close (`nb.py:1377-1383`):

```python
_price = flex_select_auto_nb(price, i, col, flex_2d)
if np.isinf(_price):
    if _price > 0:
        _price = flex_select_auto_nb(close, i, col, flex_2d)  # upper bound is close
    elif i > 0:
        _price = flex_select_auto_nb(close, i - 1, col, flex_2d)  # lower bound is prev close
    else:
        _price = np.nan  # first timestamp has no prev close
```

So a signal at row `i` that uses `close[i]` to decide and executes at `close[i]` (the default) is definitionally lookahead if the signal was derived from the same close. Vectorbtpro addresses this by offering `price='nextopen'` and `price='nextclose'` string shortcuts that automatically index `i+1`, replacing the manual shift.

### Stop Order Lookahead

From `enums.py:317-325`:

```
!!! note
    We can execute only one signal per asset and bar. This means the following:

    1) Stop signal cannot be processed at the same bar as the entry signal.

    2) When dealing with stop orders, we have another signal - stop signal - that
    may be in a conflict with the signals placed by the user. To choose between
    both, we assume that any stop signal comes before any other signal in time.
    Thus, make sure to always execute ordinary signals using the closing price
    when using stop orders. Otherwise, you're looking into the future.
```

Intra-bar stop logic uses OHLC data to determine whether a stop was triggered within a bar. The engine checks whether the low/high crossed the stop level, then uses `StopExitPrice.StopLimit` (stop price as a limit, no slippage) or `StopExitPrice.Close` (closing price). The one-order-per-bar constraint means an entry and a stop-triggered exit cannot both fire on the same bar, which is directionally correct but still a simplification versus real execution where both can occur intrabar.

### Known Footguns (Summary)

1. `fshift(1)` is required but not enforced; omission produces silent overfitting.
2. Default price is `close[i]`, matching the bar used to compute signals, the most common accidental lookahead pattern.
3. `from_order_func` callback code can freely index future array rows; Numba returns garbage data without warning (`nb.py:3076-3080`).
4. `CallSeqType.Auto` (auto-sorted call sequence for multi-asset portfolios) assumes order prices are known before order placement (`base.py:1763-1768`), another lookahead vector in multi-asset cash-sharing configurations.
5. The `val_price` parameter used for portfolio valuation within a bar defaults to the current order price (`base.py:1717-1720`), which can introduce circular valuation if not explicitly set to previous close.

---

## Corporate Actions and Survivorship

Vectorbt has no built-in handling for corporate actions (splits, dividends, mergers, delistings) or point-in-time data. The library is purely a simulation engine: it takes whatever price arrays the user supplies and runs the kernel. If the user feeds adjusted close prices from Yahoo Finance (the default in most tutorials), survivorship bias and adjustment methodology are inherited from that data source.

From the maintainer's discussion post (#185) and community forums: vectorbt "doesn't actually do any vectorized backtesting, it follows a sequential approach" and "there is no built-in mechanism that would prevent you from cheating." This applies equally to data quality: survivorship bias, unadjusted prices, and missing corporate actions do not throw errors (https://sharpely.in/blog/bias-free-backtesting-explained).

Point-in-time index membership (i.e., only including stocks that were in the index on the simulation date, not just those that survived until today) is entirely out of scope. Users who want this must build or source a point-in-time universe themselves. The `vbt.YFData.download` helper in the examples pulls from Yahoo Finance, which excludes delisted equities and uses back-adjusted prices.

---

## Cost and Execution Modeling

### Fees

Two fee mechanisms exist (from `enums.py:1507-1509` and `base.py:1681-1684`):

- `fees`: proportional fee as a fraction of order value (e.g., `0.001` = 10 bps). Supports negative values (rebates).
- `fixed_fees`: flat fee per order (e.g., $1 per trade).

Both parameters are array-broadcastable and per-order. This is adequate for modeling exchange commissions at a simplistic level.

### Slippage

From `base.py:1685-1686`:

```
slippage (float or array_like): Slippage in percentage of price.
    See vectorbt.portfolio.enums.Order.slippage. Will broadcast.
```

Slippage is applied as a flat percentage of price in the buy/sell kernels (`nb.py:90`, `nb.py:233`):

```python
# buy_nb:
adj_price = price * (1 + slippage)

# sell_nb:
adj_price = price * (1 - slippage)
```

This is a single symmetric scalar. There is no bid-ask spread model, no volume-sensitive market impact model, no tick-size rounding, and no intrabar price path model. The slippage parameter is a blunt instrument: it gives the user a lever to widen simulated execution costs but does not model the mechanics by which slippage actually arises in live markets.

### Order Types

The `OrderStatus` enum (`enums.py:460-467`) has three states: `Filled`, `Ignored`, `Rejected`. The `OrderStatusInfo` enum (`enums.py:~540`) has rejection reasons including `NoCashLong`, `NoCashShort`, `MinSizeNotReached`, `MaxSizeExceeded`, `PartialFill`, and `CantCoverFees`. The only supported order types are:

- **Market order**: execute at the specified price (close by default), adjusted by slippage.
- **Stop-limit / stop-market** (within `from_signals` stop logic): triggered when OHLC data crosses a threshold; executed at `StopExitPrice` choice. This is an approximation using bar-level data.

There are no native limit orders placed into a queue, no iceberg orders, no TWAP/VWAP execution, and no order book modeling. The maintainer confirmed in discussion #185: "Orders execute immediately like market orders with no queue system... there is only one order command permitted within a bar."

### Partial Fills

The `allow_partial` parameter (`base.py:1699-1702`) defaults to `True`. When cash is insufficient for the full order, the kernel computes the maximum affordable size and fills that fraction. This is not the same as realistic partial fills from a limit order sitting in a queue; it is simply "buy as much as cash allows."

### Reject Probability

The `reject_prob` parameter (`base.py:1695-1696`) allows injecting a random order rejection rate. This is a crude proxy for execution uncertainty, not a model of market microstructure.

---

## Performance and Scaling

### Numba Acceleration

The `@njit(cache=True)` decoration on all kernels (example: `nb.py:65`) means the first call triggers LLVM compilation; subsequent calls run compiled machine code. Vectorbt's own benchmarks (in the `benchmarks/` directory of the repo) show 1,000,000 orders processed in 42-53ms depending on whether the Numba or Rust engine is used.

The README (https://github.com/polakowo/vectorbt) claims "the fastest backtesting engine in open source" and gives a rolling z-score benchmark: 14x faster than native pandas (33ms vs 482ms). These numbers are for the generic array operations, not full portfolio simulation, but the pattern generalizes.

For parameter sweeps, the performance gain is multiplicative. When testing 10,000 MA-window combinations (`vbt.MA.run_combs(price, window=windows, r=2)`), all combinations are batched into a single array and processed by one compiled kernel call. The overhead of Python function calls and data copies occurs once rather than 10,000 times.

### Memory Model

Vectorbt is DataFrame-heavy. Indicator results for large parameter sweeps are stored as wide DataFrames with a MultiIndex on columns. For 10,000 configurations across 500 days, the signal array is 500 x 10,000 booleans = 5 million cells. This is manageable in RAM. At minute-level data with thousands of configurations, memory can become the binding constraint before CPU does.

Order records are stored as structured NumPy record arrays (`order_dt` defined in `enums.py:1780`), which are compact. The `Portfolio` object holds references to these arrays and constructs derived statistics lazily.

### The Killer Feature: Parameter Sweeps

The canonical vectorbt workflow for research is:

```python
windows = np.arange(2, 101)
fast_ma, slow_ma = vbt.MA.run_combs(price, window=windows, r=2,
                                     short_names=["fast", "slow"])
entries = fast_ma.ma_crossed_above(slow_ma)
exits   = fast_ma.ma_crossed_below(slow_ma)
pf = vbt.Portfolio.from_signals(price, entries, exits, init_cash=100)
pf.total_return()  # returns a Series of 4851 parameter combinations
```

This replaces what would be a Python `for` loop over thousands of backtest runs with a single vectorized call. The output is a Pandas Series indexed by (fast_window, slow_window) pairs, directly suitable for heatmap visualization. No other open-source backtesting library does this as cleanly or as fast.

---

## Strengths

**Hyperparameter exploration**: the combination of array broadcasting, `run_combs`, and Numba-compiled kernels makes vectorbt the best open-source tool for signal research at scale. Screening 5,000 parameter combinations takes seconds, not hours.

**Numba speed**: the JIT-compiled kernels process millions of bar-events per second. For the specific task of iterating a fixed-logic simulation over historical data, this is close to optimal.

**API expressiveness for signal research**: the `from_signals` interface is clean. Expressing a moving-average crossover strategy in five lines of code, then sweeping 10,000 configurations, is genuinely productive for early-stage research.

**Rich analytics**: the `Portfolio` object exposes Sharpe ratio, Sortino ratio, max drawdown, trade-level statistics, and QuantStats integration out of the box. This is more comprehensive than most event-driven backtesting frameworks.

**Stop orders**: native support for stop-loss and take-profit with trailing stops in `from_signals`, including the ability to adjust stops dynamically via a Numba callback (`adjust_sl_func_nb`).

**Multi-asset cash sharing**: the `cash_sharing` parameter and `group_by` interface allow simulating portfolio-level capital allocation across assets within a single simulation run, which is complex to implement in event-driven frameworks.

---

## Weaknesses, Footguns, Critiques

### Not Realistic for Execution

The fundamental architectural choice, one market order per bar per asset, price equals bar close (or a shifted close), flat slippage, means vectorbt results cannot be directly trusted for production deployment. The gap between "simulate 1000 strategies in 2 seconds" and "this strategy will make money in live trading" is wide. Autotradelab's comparison frames this as "Blazing fast research that hits a production wall, build your alpha here and execute it elsewhere." (https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded)

The maintainer acknowledged this directly in discussion #185: "there is no order management, once you issue an order command, it gets executed/rejected immediately with state-less execution, and there is no order queue."

### The Shift Discipline is Brittle

The `fshift(1)` requirement is the single most dangerous property of vectorbt for unsophisticated users. It is:

- Not enforced by the engine
- Inconsistently demonstrated in the docs (issue #190)
- Easy to miss when composing complex multi-indicator signals where some inputs are computed on close and others on open

The result is silent overfit. A strategy that looks like it has a 2.0 Sharpe ratio may have a 0.3 Sharpe ratio once the shift is applied correctly.

### Documentation Gaps

The docs at https://vectorbt.dev/ are extensive but uneven. The `from_order_func` API is powerful but the learning curve is steep: users must understand Numba's type system, the context named-tuple hierarchy (`SimulationContext`, `GroupContext`, `SegmentContext`, `OrderContext`, `PostOrderContext`), and the column-major vs. row-major iteration distinction. The docs warn about this (`base.py:193-196`) but the barrier is real.

The docs do not clearly state "default execution price is close[i], which is lookahead if your signal is computed from close[i]." This should be on the front page of `from_signals` documentation in bold.

### One Order Per Bar Per Asset

The constraint is stated explicitly in the docs and by the maintainer. While `flex_simulate_nb` (`nb.py:4425`) relaxes this for `from_order_func` by allowing multiple orders per segment, the standard `from_signals` and `from_orders` interfaces enforce one order per bar. This prevents modeling: scaling into positions across multiple fills, partial-fill cascades, and realistic order queue dynamics.

### No Partial Fill Realism

`allow_partial=True` fills the affordable fraction of an order. This is not the same as an exchange's partial fill behavior, where a limit order rests in the book and gets filled incrementally as contra-side orders arrive. The vectorbt model assumes instant fill at a uniform price, which overstates execution quality for large orders.

### Commercial Split Frustration

The OSS version is frozen. Any user who needs simulation chaining, futures support, or `nextopen`/`nextclose` price semantics must pay for vectorbtpro. The Commons Clause in the license prevents building commercial products on top of the OSS code without a commercial license. The community is effectively being channeled toward the subscription tier, and the OSS repo's single 2026 commit reflects this.

---

## Lessons for Our Design

### Things to Copy

**Numba acceleration as a first-class mode**: vectorbt proves that a Numba-compiled sequential kernel is the right tool for parameter sweeps. For our backtester's "research mode", screening thousands of configurations to identify promising parameter regions, we should provide a separate Numba-accelerated path that accepts pre-computed signal arrays and produces aggregate statistics without the overhead of a full event queue. This mode is honest about its approximations: market orders at close, flat slippage, one order per bar.

**Record arrays over object lists**: storing fills as structured NumPy record arrays (the `order_dt` pattern in `enums.py:1780`) is memory-efficient and directly analyzable with NumPy/Pandas. Our event-driven engine should emit fills into a similar compact record format.

**Broadcasting for multi-asset parameter sweeps**: the ability to pass a 2-D signal array (rows = time, columns = parameter configurations) to a single kernel call is the key productivity multiplier. Our sweep mode should support this pattern.

### Things to Avoid

**Making vectorized simulation the primary model for production-realistic validation**: the one-order-per-bar, market-at-close model is correct for fast screening, but a strategy that passes screening must be re-validated in a full event-driven simulation with a realistic order queue, intrabar price path, and partial-fill mechanics before any capital is committed.

**Implicit lookahead via price defaults**: our API should make execution price explicit and should default to the next bar's open (or require the user to state an execution price explicitly) rather than silently using the signal bar's close. The `fshift(1)` footgun is entirely an API design failure; it could be prevented by separating "signal timestamp" from "execution timestamp" at the API level.

**Undocumented future-data access in callbacks**: our event-driven callback interface should make it structurally impossible (or at least loudly warned) to read data from future bars. One approach: pass a read-only view of the historical data array sliced up to `i` rather than the full array.

### Open Question: Should We Provide a Vectorized Signal-Research Mode?

Yes, with caveats. A Numba-accelerated sweep mode alongside our event-driven core is the right architecture for a production-grade backtester. The two modes serve different purposes:

- **Sweep mode**: "Does this signal class have edge across thousands of parameter configurations?" Fast, approximate, intentionally simplified execution model. Results are directional indicators, not production estimates.
- **Event-driven mode**: "Does this specific strategy configuration produce realistic results under realistic execution?" Slow (per-configuration), but accurate: full order queue, intrabar fill logic, spread modeling, partial fills, corporate action handling.

The key design requirement is that the two modes be clearly labeled and that results from sweep mode be explicitly marked as research estimates, not production estimates. Vectorbt's failure is not building sweep mode; it is presenting sweep-mode results as if they are sufficient for strategy validation.

---

## Sources

1. https://github.com/polakowo/vectorbt, Official OSS repository. Commit b213e07 (2026-04-23), version 1.0.0, Apache 2.0 + Commons Clause license.
2. C:/temp/vectorbt/vectorbt/portfolio/nb.py, Core Numba-compiled simulation kernels: `simulate_nb` (line 2933), `simulate_from_orders_nb` (line 1282), `simulate_from_signals_nb` (line 1844), `flex_simulate_nb` (line 4425), `buy_nb` (line 72), `sell_nb` (line 214).
3. C:/temp/vectorbt/vectorbt/portfolio/base.py, `Portfolio` class with `from_orders` (line 1617), `from_signals` (line 2041), `from_order_func` (line 3287). Contains the shift(1) hint (line 2298) and lookahead warnings (lines 186, 2641).
4. C:/temp/vectorbt/vectorbt/portfolio/enums.py, All named tuples and enums: `Order` (line 1505), `SizeType` (line 396), `StopExitPrice` (line 277), `OrderStatus` (line 460), `order_dt` record layout (line 1780).
5. https://github.com/polakowo/vectorbt/discussions/185, Maintainer's own words: "vectorbt doesn't actually do any vectorized backtesting, it follows a sequential approach just like most other libraries." Key discussion of order management limitations and one-order-per-bar constraint.
6. https://github.com/polakowo/vectorbt/issues/190, User report of `fshift(1)` inconsistency between documentation text and code examples. Demonstrates the lookahead footgun as a real, documented community-reported problem.
7. https://vectorbt.pro/features/portfolio/, VectorBT PRO paywalled features: simulation chaining, futures support, stop laddering, negative price handling, `nextopen`/`nextclose` price semantics.
8. https://vectorbt.pro/documentation/fundamentals/, PRO fundamentals: confirms same core vectorized architecture as OSS; PRO additions are lifecycle hooks and execution price options, not a different simulation paradigm.
9. https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded, Comparison of vectorbt, Backtrader, NautilusTrader, Zipline. Key quote: "Build your alpha here. Execute it elsewhere" and "Blazing fast research that hits a production wall."
10. https://www.elitetrader.com/et/threads/industry-standard-backtest-tool-is-vectorbt-pro.370565/, Practitioner forum discussion on vectorbtpro as industry standard; mixed sentiment on commercial split.
11. https://medium.com/@trading.dude/battle-tested-backtesters-comparing-vectorbt-zipline-and-backtrader-for-financial-strategy-dee33d33a9e0, Speed benchmarks comparison: vectorbt processes millions of trades per second vs Zipline's hours-scale runs at minute data.
12. https://vectorbt.dev/getting-started/features/, OSS feature list and performance claims: 1,000,000 orders in 42-53ms, 14x faster than pandas for rolling operations.
13. https://vectorbt.dev/getting-started/usage/, Official usage examples demonstrating parameter sweep workflow (`run_combs`) and the `fshift(1)` pattern.
14. https://sharpely.in/blog/bias-free-backtesting-explained, External analysis of survivorship bias and point-in-time data: notes vectorbt-style frameworks inherit data quality problems from their input sources; no built-in protection.
