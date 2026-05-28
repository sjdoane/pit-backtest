# Zipline Analysis

Covers: quantopian/zipline (archived original) and stefan-jansen/zipline-reloaded (maintained fork, v3.1.1 as of July 2025).
All code citations are from the `C:/temp/zipline-reloaded` shallow clone unless otherwise noted.

---

## Executive Summary

- **Maintenance state.** The original Quantopian repo is archived and receives no commits; zipline-reloaded (maintained by Stefan Jansen) is the only production-viable fork, currently at v3.1.1 with Python >= 3.10 and pandas >= 1.3, < 3.0 support.
- **Architecture choice.** Zipline is a fully event-driven, bar-by-bar simulator. Its core loop (in Cython) ticks through sessions and minutes one at a time, passing a `BarData` snapshot to user code. This is sound for correctness but slow for large universes.
- **Biggest strength.** The Pipeline API is the best-designed cross-sectional computation layer in any open-source backtester. Its `window_safe` flag and topological term graph make lookahead in factor computation structurally difficult, not just conventionally avoided.
- **Biggest weakness.** Zipline does not enforce point-in-time index membership out of the box. Survivorship bias in the universe is the user's problem; the framework provides no primitives for it. Separately, order fills in daily mode use the **close** price of the next bar, which is a subtle timing bias that the framework does not warn about.
- **Key lessons for our design.** Copy the Pipeline concept (compute factors across a 2D date x asset matrix in bulk, with explicit window guards). Avoid the bcolz-backed bundle system (bespoke, opaque, hard to populate). Avoid mandatory benchmark dependency in the simulation core.

---

## Project Status and Maintenance

**Original repo (quantopian/zipline):** Archived on GitHub. Last commit was November 2020, coinciding with Quantopian's closure. As of May 2026 the repo shows 1,965+ open issues with no triage. It is Python 3.5/3.6 vintage and is incompatible with pandas >= 1.0 without patches. [1]

**zipline-reloaded (stefan-jansen/zipline-reloaded):** Active fork. Version 3.1.1 was released July 23, 2025. Stefan Jansen (author of "Machine Learning for Algorithmic Trading") maintains it to support his book's code examples and the broader Python algotrading community. As of May 2026 the repo shows approximately 25 open issues and 1,800+ GitHub stars. [2]

Key modernizations in zipline-reloaded relative to the original:

- Migrated from `trading-calendars` to `exchange-calendars >= 4.2` (actively maintained).
- Pandas compatibility extended from 1.x lock-in to `>=1.3.0, <3.0`.
- NumPy 2.0 compatibility added in release 3.05, requiring pandas >= 2.2.2.
- SQLAlchemy > 2.0 support added.
- Python minimum raised to >= 3.10 in current builds.

Why it persists despite Quantopian's closure: the Pipeline abstraction is genuinely difficult to replicate, and Jansen's textbook creates ongoing demand. No other open-source tool has matched Pipeline's cross-sectional correctness guarantees.

---

## Architecture

### Event-Driven vs Vectorized

Zipline is event-driven. Its simulation core iterates bar by bar; it does not operate on pre-loaded numpy arrays the way VectorBT does. Two simulation modes exist:

- **Daily mode:** one `BAR` event per trading session. Orders placed in `handle_data` fill at the **close** of the *next* session.
- **Minute mode:** one `BAR` event per market minute (typically 390 per day for US equities). Orders fill within the same minute at the close price plus slippage.

The event type enumeration is defined in Cython:

```python
# src/zipline/gens/sim_engine.pyx, lines 25-30
cpdef enum:
    BAR = 0
    SESSION_START = 1
    SESSION_END = 2
    MINUTE_END = 3
    BEFORE_TRADING_START_BAR = 4
```

### The Central Simulation Loop

`AlgorithmSimulator.transform()` in `src/zipline/gens/tradesimulation.py` is the main simulation generator. It iterates over the clock (a `MinuteSimulationClock` Cython object) and dispatches to handlers:

```python
# src/zipline/gens/tradesimulation.py, lines 223-251
for dt, action in self.clock:
    if action == BAR:
        for capital_change_packet in every_bar(dt):
            yield capital_change_packet
    elif action == SESSION_START:
        for capital_change_packet in once_a_day(dt):
            yield capital_change_packet
    elif action == SESSION_END:
        positions = metrics_tracker.positions
        position_assets = algo.asset_finder.retrieve_all(positions)
        self._cleanup_expired_assets(dt, position_assets)
        execute_order_cancellation_policy()
        algo.validate_account_controls()
        yield self._get_daily_message(dt, algo, metrics_tracker)
    elif action == BEFORE_TRADING_START_BAR:
        self.simulation_dt = dt
        algo.on_dt_changed(dt)
        algo.before_trading_start(self.current_data)
    elif action == MINUTE_END:
        yield self._get_minute_message(dt, algo, metrics_tracker)
```

Within each `BAR` event, `every_bar()` processes pending fills from the blotter first, then calls `handle_data` (lines 107-151, tradesimulation.py). This ordering is important: orders from the *prior* bar are filled at the current bar's price before user code runs.

### The MinuteSimulationClock

The clock is a Cython iterator that yields `(timestamp, event_type)` pairs for every market minute, bracketed by `SESSION_START` and `SESSION_END`, with `BEFORE_TRADING_START_BAR` inserted before the open:

```python
# src/zipline/gens/sim_engine.pyx, lines 73-113
def __iter__(self):
    for idx, session_nano in enumerate(self.sessions_nanos):
        yield pd.Timestamp(session_nano, tz='UTC'), SESSION_START
        bts_minute = pd.Timestamp(self.bts_nanos[idx], tz='UTC')
        regular_minutes = self.minutes_by_session[session_nano]
        # ... emits BEFORE_TRADING_START_BAR, then BAR for each minute
        yield regular_minutes[-1], SESSION_END
```

### The Pipeline Abstraction

Pipeline is zipline's distinguishing design contribution. Rather than computing factors inside `handle_data` (where the user could accidentally reference future data through `data.history()`), Pipeline runs as a separate engine. The `SimplePipelineEngine.run_pipeline()` method (engine.py) does the following:

1. Determines a "lifetimes matrix" (assets x dates boolean frame) from `AssetFinder.lifetimes()`, which uses each asset's `start_date` and `end_date` to mask out periods when the asset was not traded.
2. Builds a dependency graph (`TermGraph`) of all factors, filters, and classifiers.
3. Topologically sorts the graph and computes each term in order, storing results in a workspace dictionary with reference counting to bound memory.
4. Slices out only the rows for the requested date range and returns a narrow DataFrame.

Pipeline results are delivered to the algorithm via `algo.pipeline_output(name)`, which is callable inside `before_trading_start`. This placement is deliberate: `before_trading_start` runs *before* market open on each day, so factors are computed on yesterday's close data. The clock confirms this ordering (sim_engine.pyx lines 95-110).

### BarReader, DataPortal

`BarReader` (data/bar_reader.py, lines 44-153) is an abstract base class with two abstract methods that all concrete readers must implement:

- `load_raw_arrays(columns, start_date, end_date, assets)` returns a list of numpy ndarrays of shape `(bars_in_range, n_assets)`.
- `get_value(sid, dt, field)` returns a single scalar.

The concrete implementation for daily data is `BcolzDailyBarReader` (data/bcolz_daily_bars.py), which reads from bcolz ctables on disk. This is the chief data format complaint against zipline: bcolz is a legacy columnar format largely superseded by Zarr and Parquet. The comment at line 361 of bcolz_daily_bars.py reads: "all of the data for all assets into memory and then indexing into that" for session data, which is the memory model in practice.

`DataPortal` (data/data_portal.py) wraps one or more `BarReader` instances plus a `SQLiteAdjustmentReader` and serves as the single point of access for all historical and current price data. It also handles adjustment application (splits, dividends, mergers) through `get_adjustments()` and `get_adjusted_value()`.

### Threading Model

Zipline is single-threaded. The simulation loop is a Python generator; Pipeline computation is also single-threaded numpy operations. There is no async infrastructure and no use of `threading.Thread` or `multiprocessing` in the core. The Cython extensions (`sim_engine.pyx`, `_protocol.pyx`, `_finance_ext.pyx`) release the GIL in some inner loops, but the overall simulation is sequential.

---

## Lookahead Bias Protection

### The Pipeline API: Structural Prevention

Pipeline's `Term` class enforces correctness through the `window_safe` attribute (pipeline/term.py, line 96, default `False`). Any term with `window_length > 1` checks that all its inputs are `window_safe`:

```python
# src/zipline/pipeline/term.py, lines 612-615
if self.window_length > 1:
    for child in self.inputs:
        if not child.window_safe:
            raise NonWindowSafeInput(parent=self, child=child)
```

This raises a hard error at Pipeline construction time, not at runtime. A factor that is not marked `window_safe` cannot be composed into a longer-window computation without explicit acknowledgment. This makes an entire class of temporal errors a build-time failure rather than a silent runtime bias.

The execution plan also tracks "extra input rows": for a factor with `window_length=20`, the engine automatically fetches 19 extra leading rows so that the first output row has a full window of data, without ever giving the factor a view past the current date. See `ComputableTerm.dependencies` (term.py, line 662-673):

```python
@lazyval
def dependencies(self):
    extra_input_rows = max(0, self.window_length - 1)
    out = {}
    for term in self.inputs:
        out[term] = extra_input_rows
    out[self.mask] = 0
    return out
```

### Where the Engine Still Trusts the User

`BarData.current()` and `BarData.history()` (defined in `_protocol.pyx`, lines 191 and 537) are available inside `handle_data` and `before_trading_start`. These methods call `DataPortal.get_spot_value()` or `DataPortal.get_adjusted_value()` with the current simulation timestamp. The enforcement is temporal, not structural: the timestamp is controlled by the simulator, so as long as the user calls `data.current()` or `data.history()` normally, they get point-in-time data.

However, a user can introduce lookahead by:

1. Calling `data.history(asset, 'close', bar_count=N, frequency='1d')` with a large `bar_count` that spans the current session, then manually indexing the last row (which is the current bar's *close*, available only at session end but accessible any time in daily mode).
2. Using `context` variables populated by external pandas operations outside the zipline API that reference the full price matrix.
3. Mixing Pipeline outputs with `data.history()` calls in ways that defeat the temporal boundary Pipeline enforces.

The framework does not (and cannot) detect arbitrary Python that touches external data.

### Known Footguns from GitHub Issues

**Fill price in daily mode uses next-bar close.** Issue #2011 on the original repo identifies that in `VolumeShareSlippage.process_order`, orders placed in `handle_data` on day T are filled on day T+1 at the *closing* price of T+1, not the opening price. This means simulated fills used data that did not exist at the time the fill would have executed. The issue remained unresolved. [3]

**Scheduled functions and market open execution.** Issue #2364 documents that `schedule_function` with `time_rules.market_open()` in daily mode still fills orders at session close, not open. A user's custom `InstantSlippage` workaround using `data.current(asset, 'open')` is the community answer. [4]

---

## Corporate Actions and Survivorship

### Splits

Splits are stored in the SQLite adjustments database managed by `SQLiteAdjustmentWriter`. At the start of each session, `once_a_day()` in tradesimulation.py calls `data_portal.get_splits(assets_we_care_about, midnight_dt)` (line 179) and then `algo.blotter.process_splits(splits)` and `metrics_tracker.handle_splits(splits)`. This adjusts both open position share counts and order quantities.

Historical prices are adjusted retroactively via `DataPortal.get_adjustments()` (data_portal.py, lines 553-617). When `get_adjusted_value()` is called, adjustments between the data timestamp and the perspective timestamp are multiplied in. Volume gets the *inverse* of the split ratio (line 580). This is standard backward-adjusted pricing, which is correct for historical returns but means raw price levels are fictional for absolute-price strategies.

### Dividends

Cash dividends are handled as price adjustments in the adjusted price series. `get_adjustments()` retrieves them from the `DIVIDENDS` table using the same `dt < adj_dt <= perspective_dt` window logic as splits (data_portal.py, lines 603-610). Stock dividends (share distributions) are handled separately in `DataPortal.get_stock_dividends()` (line 1116), which queries a `stock_dividend_payouts` table by ex_date and pay_date.

The critical limitation: the default Quandl/WIKI bundle and most community bundles provide *adjusted* end-of-day prices. When you call `data.current(asset, 'close')`, you get the backward-adjusted close. This is fine for return computation but means the absolute price is synthetic. If you are modeling options, absolute price levels are wrong. If your strategy conditions on price crossing a threshold, you need to ensure the threshold itself was similarly adjusted.

### Point-in-Time Index Membership and Survivorship

This is zipline's most significant unaddressed blind spot. The `AssetFinder.lifetimes()` method (assets.py, lines 1419-1474) produces a boolean frame of asset existence based on `start_date` and `end_date` columns in the asset metadata. Pipeline uses this matrix as its root mask: an asset only appears in Pipeline output on dates when it was alive.

However, "alive" in zipline means "existed as a tradable security," not "was a member of a specific index on that date." Index composition changes are not modeled at all. A GitHub issue (#2641) requesting S&P 500 membership data and market cap filtering received no maintainer response and was never resolved. [5]

The practical consequence: if you populate a bundle with the current S&P 500 constituents and backtest over 10 years, you get catastrophic survivorship bias. Only securities that survived to today are in your universe for all 10 years. Community solutions involve using third-party point-in-time data providers (Norgate, Sharadar) and building custom bundles. The framework provides no native primitives.

### Delistings

`_cleanup_expired_assets()` in tradesimulation.py (lines 258-303) handles delistings via the `auto_close_date` field on each `Asset`. When a session date exceeds an asset's `auto_close_date`, the simulator force-closes any positions and cancels open orders. The delisting price used is whatever the last available price is in the bar reader, which may not be the actual delisting price (often distorted by illiquidity in the final days). The system does not model delisting proceeds, trading halts, or OTC transition.

---

## Cost and Execution Modeling

### Slippage Models

All slippage models inherit from `SlippageModel` (slippage.py, lines 81-209). The abstract method is `process_order(data, order)`, which returns `(execution_price, execution_volume)`.

**NoSlippage** (line 212): fills immediately at current close price. For testing only.

**FixedSlippage** (lines 334-365): fixed half-spread added to buy, subtracted from sell:
```python
# src/zipline/finance/slippage.py, lines 362-365
def process_order(self, data, order):
    price = data.current(order.asset, "close")
    return (price + (self.spread / 2.0 * order.direction), order.amount)
```
No volume cap. An unlimited order on a penny stock fills in full at the same spread as a liquid large-cap. This is the primary footgun for new users.

**VolumeShareSlippage** (lines 241-331): models price impact as a quadratic function of volume participation. Default `volume_limit=0.025` (2.5% of bar volume), `price_impact=0.1`. Fill price formula:
```
price * (1 + price_impact * (volume_share ** 2))   [for buys]
price * (1 - price_impact * (volume_share ** 2))   [for sells]
```
This is the standard Almgren-style impact model. It is volume-capped and fills spread across bars if the order is large relative to volume.

**FixedBasisPointsSlippage** (lines 603+): a fixed percentage of price, with a volume cap. This is the default for equities in `SimulationBlotter`:
```python
# src/zipline/finance/blotter/simulation_blotter.py, lines 63-64
self.slippage_models = {
    Equity: equity_slippage or FixedBasisPointsSlippage(),
```

**VolatilityVolumeShare** (lines 520-601): a futures-specific model using the formula `MI = eta * sigma * sqrt(psi)` where sigma is 20-day realized volatility and psi is volume fraction. Default for futures.

**Missing models:** There is no bid-ask spread model that uses actual quoted spreads (LOB data is not a supported input format). There is no borrow cost or short locate fee model anywhere in the codebase; the comment at ledger.py line 196 references dividend obligations for borrowed stock but there is no fee rate. Hard-to-borrow names are treated as freely shortable.

### Commission Models

All commission models are in commission.py. The concrete classes are:

- `PerShare` (line 144): default for equities. `$0.001/share` with optional minimum. Default minimum is `$0.00`.
- `PerTrade` (line 283): flat per-order cost. Charged to first fill; subsequent partial fills are free.
- `PerDollar` (line 362): `$0.0015/dollar` traded (0.15 bps). Applied per transaction.
- `PerContract` (line 191): for futures. Accepts float or dict mapping root symbols to per-contract costs.
- `PerFutureTrade` (line 326): flat per-trade cost for futures, charged via the `exchange_fee` slot of `PerContract`.

### Order Types

`ExecutionStyle` subclasses (execution.py, lines 24-229): `MarketOrder`, `LimitOrder`, `StopOrder`, `StopLimitOrder`. No `MOO` (Market-on-Open) or `MOC` (Market-on-Close) order types exist. All simulated fills use bar close prices; opening auction prices are not accessible as a fill price.

### Partial Fills

`VolumeShareSlippage.simulate()` (slippage.py, lines 160-206) loops over orders for an asset and fills up to the volume cap per bar. If the volume cap is hit mid-order, it raises `LiquidityExceeded` and the remainder carries to the next bar. The blotter logs partial fill warnings in simulation_blotter.py (lines 195-264). This is correct in principle but does not model time priority or queue position within a bar.

---

## Performance and Scaling

Zipline's event loop is single-threaded Python with Cython inner loops. The `MinuteSimulationClock.__iter__` is compiled Cython that yields timestamps via numpy nanosecond arrays (sim_engine.pyx, lines 53-71), which is fast. The bottleneck is the Python-level `handle_data` dispatch and any pandas operations inside user code.

**Cross-sectional scaling:** Pipeline is the correct approach for cross-sectional strategies. It computes factors across all assets simultaneously using numpy operations on `(dates, assets)` matrices. A 20-day momentum factor on 3,000 assets runs as a single numpy operation, not 3,000 serial calls. However, the results must be converted back to per-asset actions inside `handle_data`, which is serial.

**Data layer:** The bcolz daily bar reader loads entire columns into memory (bcolz_daily_bars.py, line 564: "Get the colname from daily_bar_table and read all of it into memory"). For a 20-year history of 5,000 assets, this is roughly 5,000 * 252 * 20 * 5 fields * 8 bytes = ~10 GB of raw data. In practice bcolz applies chunked compression, but the working set can be large.

**Minute-bar backtests** are substantially slower. External benchmarks from the community (cited in [7]) report multi-hour runtimes for 5+ year minute-bar backtests across thousands of assets. This is a fundamental constraint of the event-driven architecture.

**No benchmarks in the codebase.** The zipline-reloaded repository contains no performance benchmarks. Speed claims must be taken from community sources.

---

## Strengths

**Pipeline is the gold standard for cross-sectional factor research.** The combination of: (a) computes all assets simultaneously in bulk, (b) enforces `window_safe` at construction time, (c) uses the lifetimes matrix to mask non-traded periods, (d) delivers results before market open via `before_trading_start`, gives Pipeline correctness properties that `handle_data`-based approaches structurally cannot match. The design acknowledges that "looks like a stock" is a separate concern from "was a member of the index," even if it does not solve the latter.

**Adjustment handling is correct.** The `dt` / `perspective_dt` paradigm in `get_adjustments()` (data_portal.py, lines 553-617) is the right abstraction: adjustments are applied only for corporate actions that occurred between the data point's date and the observation date. This is how adjusted pricing should work.

**Asset lifecycle enforcement.** `_cleanup_expired_assets()` (tradesimulation.py, lines 258-303) automatically closes positions and cancels orders for delisted assets, preventing zombie positions that silently sit at stale prices.

**Pluggable models.** Slippage, commission, and blotter are all abstract base classes. Writing a custom slippage model requires implementing one method. The `register(Blotter, "default")` decorator pattern (simulation_blotter.py, line 40) allows replacing the entire execution layer.

**Cython core.** The hot paths (clock iteration, BarData access) are compiled Cython, not pure Python. This keeps per-bar overhead manageable even for large minute-bar runs.

---

## Weaknesses, Footguns, and Critiques

### Close-Price Fill Timing

The most architecturally significant problem: in daily mode, orders placed at bar T are filled at the *close price* of bar T+1. This is documented behavior but it means simulated fills have implicit access to T+1's close, which is not available when the fill actually executes intraday on T+1. In reality, a market order placed before open on T+1 fills at or near the open of T+1, not the close. The delta between open and close can be 1-3% on volatile names. The framework does not warn the user. Issue #2011 on the original repo raised this concern and it remained open. [3]

### No MOO/MOC Order Types

Real equity trading strategies frequently use opening and closing auctions. Zipline supports only continuous session prices. A user who wants to model opening auction fills must write a custom slippage model that reads the `open` field instead of `close`, as shown in issue #2364. [4] This is undocumented and non-obvious.

### Survivorship Bias is the User's Entire Problem

Zipline provides the mechanism (`auto_close_date`, lifetimes matrix) but not the data. Building a survivorship-bias-free bundle requires sourcing point-in-time constituent lists, routing delisted tickers into the asset metadata, and populating price history including delisting prices. No out-of-the-box bundle supports this. The quantopian issue tracker has open feature requests for S&P 500 membership primitives with no resolution. [5]

### Mandatory Benchmark

The original zipline required a benchmark for every simulation: the `NoBenchmark` exception reads "Must specify either benchmark_sid or benchmark_returns." This blocked offline use, caused spurious failures when IEX's API changed (issue #2627), and created a hard dependency on a US-centric SPY default that made international strategy development more painful. [6] zipline-reloaded has partially addressed this by accepting `benchmark_returns` as an explicit parameter, but the benchmark remains a required field in `TradingAlgorithm.__init__` (algorithm.py, line 229-230); passing `None` raises.

### Opaque Bundle System

Data ingestion uses `zipline ingest -b <bundle_name>` CLI commands that write to a bcolz on-disk format. The bundle format is not documented at the format level; to add a new data source you must implement a Python function that calls internal `BcolzDailyBarWriter` and `SQLiteAdjustmentWriter` APIs. There is no standard way to point zipline at a Parquet file or a database. This is a significant onboarding barrier.

### API Friction and Quantopian Design Assumptions

The API was designed for Quantopian's cloud IDE, where users could not import arbitrary modules and all data access went through zipline's vetted interfaces. In standalone use, this produces friction: the `ZiplineAPI` context manager (tradesimulation.py, line 193) monkey-patches the algorithm namespace to expose the zipline API globally, which is unusual Python design and makes static analysis tools unreliable.

The `TradingAlgorithm.__init__` comment at line 249 ("XXX: This is kind of a mess") and line 274 ("XXX: This is also a mess") in algorithm.py are honest acknowledgments from the original authors.

### No Borrow Cost or Hard-to-Borrow Modeling

There is no facility for modeling short borrow rates or locates. A strategy that heavily shorts illiquid or high-fee-to-borrow names will have fantasy-level P&L in a zipline backtest. The ledger mentions dividend obligations on borrowed stock (ledger.py, line 196) but there is no fee rate structure.

### Performance at Scale

External comparisons [7][8] confirm that minute-bar backtests across large universes are slow (hours). The event-driven architecture is correct but not fast. VectorBT is 10-100x faster on equivalent strategies because it operates on pre-loaded numpy arrays.

### Why Quantopian Shut Down

Quantopian closed in November 2020 after returning investor capital in February 2020. The core business problem was that crowdsourced strategies from a public platform were either too similar to each other (insufficient diversification) or too overfit to historical data. The platform's backtesting tooling, including zipline, could not prevent the fundamental problem: with thousands of users running backtests and submitting only winners, the selected strategies had optimistically biased backtests. Zipline's correctness guarantees apply within a single backtest; they do not address cross-validation or selection bias across many strategies. [9]

---

## Lessons for Our Design

### Things to Copy

**The Pipeline concept.** A separate, bulk-computation path for cross-sectional factors that (a) operates on a 2D `(dates, assets)` matrix, (b) has explicit `window_length` and `window_safe` semantics enforced at construction time, (c) uses an asset lifetimes mask to zero out invalid periods, and (d) delivers outputs before market open rather than inside the bar handler. This is the right architecture for any strategy that ranks or filters across a large universe.

**The `dt` / `perspective_dt` adjustment paradigm.** Every historical price lookup should carry two timestamps: when the price occurred and when it is being observed from. Adjustments applied only in the window `(dt, perspective_dt]` are the correct semantics. Encode this into your data layer from the start.

**Pluggable execution models with a strict interface.** Zipline's `SlippageModel.process_order(data, order) -> (price, volume)` is a clean, minimal interface. Separate commission from slippage. Make both swappable per asset class.

**Automatic position cleanup on delisting.** The `_cleanup_expired_assets` pattern (tradesimulation.py lines 258-303) is correct and should be built in, not left to the user.

### Things to Avoid

**The bcolz bundle system.** It is opaque, uses a legacy format, and creates a high-friction onboarding wall. Use standard formats (Parquet, Arrow) that users can populate with standard tools.

**Mandatory benchmark in the simulation core.** The benchmark is a *reporting* concern, not a simulation concern. Alpha and beta calculations can be done in post-processing. Requiring a benchmark in `TradingAlgorithm.__init__` is an architectural mistake that blocks offline use and non-equity simulations.

**Close-price fills in daily mode.** Model fills at the *open* of the next bar for market orders placed after close, with the option for custom timing. The default of using next-bar close is a systematic bias that inflates or deflates P&L depending on overnight drift.

**Survivorship-bias-blind universe management.** Do not treat `auto_close_date` as a sufficient solution. Build point-in-time index membership as a first-class data primitive. Require the user to explicitly provide a point-in-time universe or warn loudly when they do not.

**Single-threaded everything.** Factor computation across a large universe can be parallelized. The Pipeline engine's topological sort is inherently sequential, but individual term computations are embarrassingly parallel. Consider `numba` JIT or `numpy` vectorization with explicit parallelism for large-universe factor research.

### Open Questions for Our Design

- Should our Pipeline equivalent operate on adjusted or unadjusted prices? (Zipline uses adjusted; this is correct for return computation but wrong for absolute-price signals.)
- How do we model opening auction fills without requiring per-bar OHLCV breakdowns?
- How do we encode point-in-time index membership as a first-class concept without requiring a proprietary data vendor?
- What is the right granularity for the adjustment database? (Zipline uses SQLite with timestamps at second resolution; this is sufficient for daily but may need refinement for tick-level work.)

---

## Sources

1. [quantopian/zipline - GitHub (archived original)](https://github.com/quantopian/zipline) - Original Quantopian repo, archived Nov 2020, 1,965+ open issues, Python 3.5/3.6 vintage.

2. [stefan-jansen/zipline-reloaded - GitHub](https://github.com/stefan-jansen/zipline-reloaded) - Maintained fork, v3.1.1 (Jul 2025), Python >= 3.10, pandas >= 1.3, < 3.0.

3. [Issue #2011: Why use close price for VolumeShareSlippage? (quantopian/zipline)](https://github.com/quantopian/zipline/issues/2011) - Unresolved question about close-price fill timing creating implicit next-bar lookahead.

4. [Issue #2364: Instant execution at market open, in daily mode (quantopian/zipline)](https://github.com/quantopian/zipline/issues/2364) - Documents that `schedule_function` with `market_open()` still fills at session close in daily mode.

5. [Issue #2641: Creating bundle with S&P members and market cap (quantopian/zipline)](https://github.com/quantopian/zipline/issues/2641) - Feature request for point-in-time index membership, unresolved, no maintainer response.

6. [Issue #2627: Remove Implicit Dependency on Benchmarks and Treasury Returns (quantopian/zipline)](https://github.com/quantopian/zipline/issues/2627) - Documents how mandatory benchmark fetching blocked offline use and caused IEX API failures.

7. [Battle-Tested Backtesters: VectorBT, Zipline, and Backtrader (Medium/Trading Dude)](https://medium.com/@trading.dude/battle-tested-backtesters-comparing-vectorbt-zipline-and-backtrader-for-financial-strategy-dee33d33a9e0) - Confirms zipline's slow performance on large minute-bar datasets; notes setup friction.

8. [Backtrader vs NautilusTrader vs VectorBT vs Zipline-reloaded (autotradelab.com)](https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded) - Positions zipline-reloaded as research-only; critiques lack of live trading and development lag.

9. [3 Takeaways from Quantopian Shutting Down (quantrocket.com)](https://www.quantrocket.com/blog/quantopian-shutting-down/) - Post-mortem on Quantopian's business failure and its implications for crowdsourced strategy selection.

10. [Lookahead bias in backtesting? (ml4trading.io exchange)](https://exchange.ml4trading.io/t/lookahead-bias-in-backtesting/1537) - Community discussion on how zipline's stream-based design prevents some lookahead categories.

11. [Zipline Pipeline Workflow (Medium/Maximilian Wimmer)](https://mw96.medium.com/zipline-pipeline-6723632824) - User-perspective walkthrough of the Pipeline API and its cross-sectional computation model.

12. [How to set up Norgate Data for Zipline Reloaded (pyquantnews.com)](https://www.pyquantnews.com/free-python-resources/how-to-set-up-norgate-data-for-zipline-reloaded) - Third-party workaround for survivorship-bias-free data; underscores that zipline provides no native solution.

13. [Issue #1965: Benchmark downloading is broken (quantopian/zipline)](https://github.com/quantopian/zipline/issues/1965) - Documents IEX API breakage caused by mandatory benchmark download dependency.
