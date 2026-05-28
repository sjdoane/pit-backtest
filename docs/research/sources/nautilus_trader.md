# nautilus_trader Analysis

Version analyzed: v1.228.0 (released May 2026)
Repository: https://github.com/nautechsystems/nautilus_trader
Cloned at: `C:/temp/nautilus` (shallow clone, depth 1)

---

## Executive Summary

- **Actively maintained production system.** Nautech Systems backs nautilus_trader commercially, shipping weekly releases. v1.228.0 is current as of May 2026. The project has an active contributor community, CI benchmarks on a dedicated nightly branch, and a structured issue/PR workflow. This is not an abandoned academic tool.

- **Rust core, Python strategy layer.** All hot paths live in Rust crates under `crates/`. Python strategies are authored using Cython wrappers (being migrated to PyO3). The boundary is explicit: typed `Handler<T>` zero-cost dispatch in Rust, `&dyn Any` dispatch for Python callbacks. The msgbus design document quotes typed routing as "~10x faster for handler dispatch (noop)" vs Any-based routing (`crates/common/src/msgbus/core.rs:53`).

- **Biggest strength: backtest and live share the same kernel.** `BacktestEngine::new` instantiates a `NautilusKernel` (`crates/backtest/src/engine.rs:136`). Live trading also uses `NautilusKernel`. Strategies deploy from research to production with zero code changes. This is the most important design decision in the entire project.

- **Biggest weakness: no corporate actions, no survivorship bias handling.** This is confirmed as an open GitHub issue (#3307). For equity mean-reversion or factor strategies where splits, dividends, and index reconstitution matter, users must pre-process data themselves. The engine has no primitives for this.

- **Key lessons for our design:** (1) Separate the clock into TestClock vs. LiveClock, enforced at construction time. (2) Model latency explicitly using a priority queue of inflight commands with timestamps. (3) Use message bus publish/subscribe as the central event spine rather than direct function calls. (4) Use Rust for hot paths only if the project will be used in live trading; for a portfolio backtest project, the dual-language build complexity is not worth it.

---

## Project Status and Maintenance

nautilus_trader is version 1.228.0 as of late May 2026. The RELEASES.md file shows continuous weekly or biweekly releases. The 1.228.0 changelog lists dozens of enhancements spanning new exchange adapters (Derive, Hyperliquid, Coinbase, Deribit combos), Rust core improvements to the DataEngine, plugin architecture for loading cdylibs at runtime, and numerous bugfixes (`RELEASES.md:1-80`).

The project is backed by Nautech Systems Pty Ltd (Australian company), which appears to use it for internal trading. Copyright headers read "2015-2026" (`crates/common/src/msgbus/core.rs:2`), indicating the core has been evolving for over a decade. CI runs Rust benchmarks via Criterion and Python benchmarks via CodSpeed on a `nightly` branch. The benchmarking philosophy is explicit: "Don't claim a win without a bench" (`BENCHMARKING.md:61`).

Supported platforms: 64-bit Linux (Ubuntu 22.04+), macOS 15.0+, Windows Server 2022+, Python 3.12-3.14.

---

## Architecture

### Message Bus Pattern

The central nervous system is a Rust message bus implemented in `crates/common/src/msgbus/`. The architecture uses **two routing mechanisms in one bus** (`crates/common/src/msgbus/core.rs:19-84`):

**Typed routing** (hot path):
```rust
// crates/common/src/msgbus/mod.rs:106-111
thread_local! {
    pub(super) static QUOTE_HANDLERS: RefCell<SmallVec<[TypedHandler<QuoteTick>; HANDLER_BUFFER_CAP]>> =
        RefCell::new(SmallVec::new());
    pub(super) static TRADE_HANDLERS: RefCell<SmallVec<[TypedHandler<TradeTick>; HANDLER_BUFFER_CAP]>> =
        RefCell::new(SmallVec::new());
    pub(super) static BAR_HANDLERS: RefCell<SmallVec<[TypedHandler<Bar>; HANDLER_BUFFER_CAP]>> =
        RefCell::new(SmallVec::new());
```

Handlers receive `&T` directly with no runtime type checking, enabling static dispatch and inlining. For `QuoteTick`, `TradeTick`, and `Bar` (which are `Copy` types), delivery to N handlers costs zero per-message allocation.

**Any-based routing** (Python callbacks and custom data):
```rust
// crates/common/src/msgbus/mod.rs:94-95
thread_local! {
    pub(super) static MESSAGE_BUS: RefCell<Option<Rc<RefCell<MessageBus>>>> = const { RefCell::new(None) };
```

The bus lives in thread-local storage, which eliminates synchronization overhead. The design doc states this explicitly: "The bus uses thread-local storage for single-threaded async runtimes. Each thread gets its own `MessageBus` instance, avoiding synchronization overhead" (`crates/common/src/msgbus/mod.rs:27-29`).

A `BusTap` interface observes all dispatched traffic before delivery and writes to the durable event store (`crates/common/src/msgbus/mod.rs:181-190`). This is how replay works: every event that passed through the bus was recorded.

**Critical footgun documented in the source:** "Typed and Any-based routing use separate data structures. Publishers and subscribers must use matching APIs. Mixing them causes silent message loss" (`crates/common/src/msgbus/core.rs:65-68`).

### Four Core Engines + Kernel

The `NautilusKernel` owns:
- `DataEngine` (market data processing, routing to Cache then MessageBus)
- `ExecutionEngine` (order lifecycle, routing to adapters)
- `RiskEngine` (pre-trade validation)
- `Portfolio` (position and P&L tracking)
- `Cache` (in-memory store for instruments, orders, positions)
- `MessageBus` (all inter-component comms)
- `Clock` (TestClock in backtest, LiveClock in live)

This is the payload of "backtest/live share the same kernel." The kernel is the unit of reuse. Both `BacktestEngine` and `LiveNode` instantiate a `NautilusKernel`.

### Backtest Engine and Live Engine Share Components

```rust
// crates/backtest/src/engine.rs:86-112
pub struct BacktestEngine {
    instance_id: UUID4,
    config: BacktestEngineConfig,
    kernel: NautilusKernel,           // <-- same kernel as live
    accumulator: TimeEventAccumulator, // <-- backtest-specific timer heap
    venues: AHashMap<Venue, Rc<RefCell<SimulatedExchange>>>, // <-- backtest-specific
    data_iterator: BacktestDataIterator, // <-- backtest-specific
    ...
}
```

The backtest engine adds a `TimeEventAccumulator` (a `BinaryHeap<Reverse<TimeEventHandler>>` at `crates/backtest/src/accumulator.rs:29`) and a `BacktestDataIterator` over historical data. Everything else is the shared kernel.

### The Backtest Main Loop

The key loop in `BacktestEngine::run_impl` (`crates/backtest/src/engine.rs:705-769`) is:

```rust
// crates/backtest/src/engine.rs:705-769
loop {
    // 1. Advance clock to next data timestamp
    if ts_init > self.last_ns {
        self.last_ns = ts_init;
        self.advance_time_impl(ts_init, &clocks);
    }

    // 2. Route data to simulated exchange (order matching)
    self.route_data_to_exchange(d);

    // 3. Process through DataEngine (fires strategy on_quote_tick etc.)
    self.kernel.data_engine.borrow_mut().process_data(d.clone());

    // 4. Drain commands queued by strategy, settle exchanges
    self.drain_command_queues();
    self.settle_venues(ts_init);

    data = self.data_iterator.next();

    // 5. Flush accumulated timer events, run venue modules
    if data.is_none() || data.as_ref().unwrap().ts_init() > prev_last_ns {
        self.flush_accumulator_events(&clocks, prev_last_ns);
        self.run_venue_modules(prev_last_ns);
    }
}
```

The ordering is deterministic: advance clock, match orders, fire strategy callbacks, drain commands, settle. Timer events fire at the correct temporal position relative to data. This is lookahead-bias-safe by construction because the clock never moves backward and strategies only observe data after it has been published at its `ts_init` timestamp.

### Rust Core vs Python Wrappers

Crates under `crates/` are pure Rust. Python-facing code uses Cython `.pyx` files (legacy) and PyO3 bindings (migration underway). The component pattern: strategy authors write Python inheriting from `Strategy` (Cython) or the upcoming PyO3 equivalent; all hot paths (matching, message dispatch, clock) are Rust.

The migration is ongoing: `component.pyx` (Python entry point for components) imports from `nautilus_trader.core.rust.common` via Cython cimports (`nautilus_trader/common/component.pyx:61-100`), showing the hybrid at work.

---

## Lookahead Bias Protection

### Temporal Ordering

Data is stored in the `BacktestDataIterator`, which requires sorting before `run()`. The engine enforces this at the call site:

```rust
// crates/backtest/src/engine.rs:595-599
anyhow::ensure!(
    self.sorted,
    "Data has been added but not sorted, call `engine.sort_data()` or use \
     `engine.add_data(..., sort=true)` before running"
);
```

Within the loop, data is processed at its `ts_init` timestamp. The clock is advanced to that timestamp before any strategy callback fires. Strategies cannot observe a price that has not yet been published.

### Clock Abstraction

The `Clock` trait is defined in `crates/common/src/clock.rs`:

```rust
// crates/common/src/clock.rs:38-45
pub trait Clock: Debug + Any {
    fn timestamp_ns(&self) -> UnixNanos;
    fn timestamp_us(&self) -> u64;
    fn timestamp_ms(&self) -> u64;
    fn timestamp(&self) -> f64;
    fn timer_names(&self) -> Vec<&str>;
    fn timer_count(&self) -> usize;
    ...
}
```

`TestClock` is the backtest implementation. It does not call `SystemTime::now()`; instead it returns whatever timestamp the engine has advanced it to. `LiveClock` calls the system clock for real-time operation. The switchover happens at construction time via the config:

```rust
// crates/backtest/src/engine.rs:664-665
logging_clock_set_static_mode();
logging_clock_set_static_time(start_ns.as_u64());
```

Even the logger uses the simulated clock in backtest, so log timestamps are deterministic and match the event timestamps.

### Can Strategies Query Future Data?

No, by design. Strategies call `self.cache.quote(instrument_id)` or similar, and the Cache only holds what has been published by the DataEngine up to the current clock time. Data is added to the Cache as the DataEngine processes each event. Future data in the iterator has not been ingested yet.

### Known Footguns

1. **Bar `ts_init` must be the close time**, not the open. If a bar with `ts_init = bar_open` is loaded, the strategy fires at bar open time with OHLC data that includes prices the market has not reached yet. The docs call this out explicitly: "Bar initialization timestamp (`ts_init`) must represent the close time to prevent look-ahead bias." nautilus_trader does not validate this; it trusts the user's data.

2. **Silent message loss from mixing typed and Any-based routing.** Documented in source (`crates/common/src/msgbus/core.rs:65-68`). A strategy subscribing to a topic with `subscribe_any` will miss messages published with `publish_quote`. No runtime error, just silence.

3. **Data added without `sort=True` will silently process out of order** unless `sort_data()` is called manually. The guard at `run_impl` will raise, but only at run time, not at `add_data` time.

---

## Corporate Actions and Survivorship

### Support Level

**Effectively none.** The engine has no built-in handling for stock splits, dividends, or ticker name changes. GitHub issue #3307 ("Add support for ticker name changes, stock splits and corporate actions in general") is open with no assignee and no planned timeline. The requester cited the Facebook-to-Meta renaming as a motivating example.

The documentation's "Key Limitations" section confirms: "Survivorship bias: Backtests assume historical data integrity; delisted instruments ignored. Corporate actions: Stock splits, dividends not modeled" (nautilustrader.io/docs/latest/concepts/backtesting/).

### Point-in-Time Index Membership

Not supported. There is no concept of a universe or dynamic instrument set. The user must supply the instruments and data themselves. If you want point-in-time S&P 500 membership, you handle that in your data pipeline before loading into nautilus_trader.

### Delistings

Handled indirectly via `InstrumentStatus` events (there is an `InstrumentClose` type in the model, `crates/backtest/src/exchange.rs:45`), but the user must inject these events manually. The engine does fire expiration timers for futures and options. For equities, there is no automatic delisting mechanism.

**Design implication for pit-backtest:** If we are building a U.S. equity backtester, corporate action support and point-in-time index membership are requirements we must build ourselves. nautilus_trader confirms this is the hardest unsolved problem in open-source backtesting.

---

## Cost and Execution Modeling

### SimulatedExchange and OrderMatchingEngine

Each venue in a backtest is a `SimulatedExchange` (`crates/backtest/src/exchange.rs:115-159`). It owns:
- A `FeeModelAny`, `FillModelAny`, and optional `LatencyModel`
- A `BinaryHeap<InflightCommand>` for latency-deferred orders (`crates/backtest/src/exchange.rs:140`)
- One `OrderMatchingEngine` per instrument

The matching engine (`crates/execution/src/matching_engine/engine.rs:75-129`) owns the order book, the fill/fee models, and handles the matching logic.

### Fill Models

There are 10 built-in fill models in `crates/execution/src/models/fill.rs`:

| Model | Behavior |
|---|---|
| `DefaultFillModel` | Probabilistic limit fill, probabilistic slippage. Returns `None` book (uses standard matching logic) |
| `BestPriceFillModel` | Unlimited liquidity at best bid/ask; fills limit orders inside the spread |
| `OneTickSlippageFillModel` | All fills one tick worse than best; simulates consistent adverse selection |
| `ProbabilisticFillModel` | 50/50 chance between best price and one tick slippage |
| `TwoTierFillModel` | 10 contracts at best, unlimited at one tick worse |
| `ThreeTierFillModel` | 50 at best, 30 at +1 tick, 20 at +2 ticks |
| `LimitOrderPartialFillModel` | 5 at best, unlimited at one tick worse (partial fill simulation) |
| `SizeAwareFillModel` | Small orders (<= 10) get 50 at best; large orders get price impact |
| `CompetitionAwareFillModel` | Reduces available liquidity by a `liquidity_factor` (simulates competing traders) |
| `VolumeSensitiveFillModel` | Adjusts liquidity to 25% of `recent_volume`; caller updates volume externally |
| `MarketHoursFillModel` | Different liquidity in low vs. normal sessions; caller toggles `is_low_liquidity` |

Each model implements the `FillModel` trait:

```rust
// crates/execution/src/models/fill.rs:43-72
pub trait FillModel {
    fn is_limit_filled(&mut self) -> bool;
    fn is_slipped(&mut self) -> bool;
    fn fill_limit_inside_spread(&self) -> bool { false }
    fn get_orderbook_for_fill_simulation(
        &mut self,
        instrument: &InstrumentAny,
        order: &OrderAny,
        best_bid: Price,
        best_ask: Price,
    ) -> Option<OrderBook>;
}
```

The `get_orderbook_for_fill_simulation` method returns a synthetic `OrderBook` that the matching engine uses instead of the real book. This is the extension point for custom slippage models: return a custom book with whatever depth profile you want. `DefaultFillModel` returns `None`, meaning the real historical book (if L2/L3 data is loaded) is used directly.

The `DefaultFillModel` has two parameters:
- `prob_fill_on_limit` (0.0-1.0): probability a limit order gets filled when price touches it
- `prob_slippage` (0.0-1.0): probability of one-tick slippage per fill (L1 data only)

Both use a seeded `StdRng` for deterministic replay (`crates/execution/src/models/fill.rs:96`).

### Latency Modeling

```rust
// crates/execution/src/models/latency.rs:24-36
pub trait LatencyModel: Debug {
    fn get_insert_latency(&self) -> UnixNanos;
    fn get_update_latency(&self) -> UnixNanos;
    fn get_delete_latency(&self) -> UnixNanos;
    fn get_base_latency(&self) -> UnixNanos;
}
```

Currently only `StaticLatencyModel` is implemented (`crates/execution/src/models/latency.rs:102-158`). It takes four nanosecond values: `base_latency`, `insert_latency`, `update_latency`, `delete_latency`. The base is automatically added to each operation latency.

When a latency model is configured, the `SimulatedExchange` wraps each incoming command in an `InflightCommand` with `timestamp = current_time + operation_latency`, inserts it into the `BinaryHeap<InflightCommand>` (`crates/backtest/src/exchange.rs:64-97`), and only processes it when `current_time >= inflight.timestamp`. This means order submissions are delayed by the modeled network round trip before they reach the matching engine, and the strategy experiences the correct event sequence: order not yet acknowledged until simulated latency has elapsed.

This is a feature most backtesters omit entirely. Zipline, backtrader, and bt all assume instantaneous order acknowledgment.

### Fee Models

Three built-in fee models (`crates/execution/src/models/fee.rs:40-44`):

- `FixedFeeModel`: flat dollar commission per fill, optionally charged only once per order
- `MakerTakerFeeModel`: reads maker/taker rates from the instrument definition (the instrument must carry them)
- `PerContractFeeModel`: fee per contract/share

The `FeeModel` trait is:

```rust
// crates/execution/src/models/fee.rs:24-37
pub trait FeeModel {
    fn get_commission(
        &self,
        order: &OrderAny,
        fill_quantity: Quantity,
        fill_px: Price,
        instrument: &InstrumentAny,
    ) -> anyhow::Result<Money>;
}
```

### Bid-Ask Spread

For L1 data (quote ticks), the spread is the difference between `best_bid` and `best_ask` in each `QuoteTick`. Market orders fill at the relevant side. The spread is implicitly modeled through the data.

For bar data, there is no spread modeling at all. A market buy fills at the bar's open price (or the configured OHLC path price). This is a significant limitation for bar-level backtesting.

### Borrow Costs and Short Sale Fees

There is no built-in borrow cost or securities lending fee model. The config has `allow_cash_borrowing: bool` (`crates/backtest/src/config.rs:306`) which permits negative cash balances (e.g., for spot FX), but this is not the same as a short-sale borrow fee. Searching the codebase for `borrow_rate`, `hard_to_borrow`, and `securities_lending` returns zero matches in the backtest/execution crates. Users must implement this themselves, typically via a `SimulationModule` that debits the account at each time step.

### Partial Fill Semantics

Fully supported. The matching engine tracks `leaves_qty` and fires multiple `OrderFilled` events when an order fills across multiple price levels. The `LimitOrderPartialFillModel` specifically demonstrates partial fill simulation (5 units at best, unlimited at one tick worse). When L2/L3 order book data is available, the engine walks the actual book levels and issues partial fills at each level as the order consumes liquidity.

### Order Types

Supported order types (from `crates/model/src/enums.rs` and docs):
- MARKET, LIMIT, STOP_MARKET, STOP_LIMIT
- MARKET_TO_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED
- TRAILING_STOP_MARKET, TRAILING_STOP_LIMIT

Time-in-force values: GTC, IOC, FOK, GTD, DAY, AT_THE_OPEN (ATO), AT_THE_CLOSE (ATC).

**Critical finding:** AT_THE_OPEN and AT_THE_CLOSE are defined in the enum (`crates/model/src/enums.rs:1889-1891`) but are explicitly **rejected** by the matching engine at simulation time:

```rust
// crates/execution/src/matching_engine/engine.rs:2996-3008
fn process_market_order(&mut self, order: &OrderAny) {
    if order.time_in_force() == TimeInForce::AtTheOpen
        || order.time_in_force() == TimeInForce::AtTheClose
    {
        self.generate_order_rejected(
            order,
            format!("time in force {} is not currently supported", ...),
        );
        return;
    }
```

The same rejection fires for limit orders (`engine.rs:3039-3044`). MOO and MOC orders are not actually simulated in the backtest engine. They exist in the type system for live order submission to venues that support them, but the simulation rejects them.

Iceberg orders (display quantity / `display_qty`) are supported at the live adapter level (Binance spot adapter uses them at `crates/adapters/binance/src/spot/execution.rs:241`). Support in the `OrderMatchingEngine` simulation is not confirmed from the source read; the matching engine struct has no `display_qty` handling visible in the first 215 lines read.

---

## Performance and Scaling

### Rust Hot Path Speed

The message bus documentation quotes typed vs. Any-based routing benchmarks (AMD Ryzen 9 7950X):

| Scenario | Typed vs Any |
|---|---|
| Handler dispatch (noop) | ~10x faster |
| Router with 5 subscribers | ~3.5x faster |
| Router with 10 subscribers | ~2x faster |
| High volume (1M messages) | ~7% faster |

(`crates/common/src/msgbus/core.rs:53-63`)

The website claims "stream up to 5 million rows per second, handling more data than available RAM" (nautilustrader.io). This figure is not independently verifiable from the source code alone; it likely refers to the data catalog streaming pipeline where data is read from Parquet files without loading the whole dataset into memory.

### Memory Model

All prices and quantities use fixed-point integer representation (`FIXED_SCALAR` in `crates/model/src/types/fixed.rs`). There are no floating-point arithmetic paths in price/quantity calculations. The system explicitly panics on NaN, Infinity, and arithmetic overflow, as stated in the architecture docs: "strict fail-fast principles, panicking on arithmetic overflow, invalid deserialization (NaN/Infinity), type conversion failures."

The `Cache` uses `Rc<RefCell<Cache>>` (not `Arc<Mutex<Cache>>`), which is single-threaded by design. No lock contention on hot paths.

### Benchmarking Infrastructure

Criterion (wall-clock) and iai (instruction-counting) are both used. Hot-path benchmarks include:
- `crates/execution/benches/matching_core.rs`: `OrderMatchingCore` add/delete/lookup/iterate
- `crates/common/benches/msgbus.rs`: message bus throughput
- `crates/common/benches/cache/orders.rs`: order cache query and ingest

CI runs these on pushes to the `nightly` branch via GitHub Actions on a fixed self-hosted machine. Python performance tests run through CodSpeed. Absolute numbers are machine-specific and not quoted in checked-in files per policy (`BENCHMARKING.md:53-56`).

### Scaling

The single-threaded kernel design means a single backtest run is not parallelized. Multiple runs are parallelized by running multiple processes (e.g., via the `BacktestNode` which manages multiple `BacktestEngine` instances across separate runs/configs). This is the standard design for a Monte Carlo parameter sweep.

---

## Strengths

**1. The kernel is shared between backtest and live.** This is the defining differentiator. Strategy code written for backtest runs in live without modification. The `TestClock` / `LiveClock` substitution, same MessageBus, same DataEngine and ExecutionEngine, same order lifecycle model. The most common failure mode in real-world trading (backtest-live divergence) is structurally prevented.

**2. Deterministic replay.** Every event passes through the `BusTap` before delivery, which writes it to a durable event store. Any session can be replayed bit-for-bit. This enables debugging of live issues using recorded data.

**3. Latency modeling is a first-class concept.** The `LatencyModel` trait, the `BinaryHeap<InflightCommand>`, and the nanosecond-resolution `UnixNanos` timestamps all work together to simulate network round-trip delay. No other open-source backtester we know of does this correctly. Zipline, bt, and backtrader all assume zero-latency order submission.

**4. Rich fill model library.** 10 built-in fill models from `BestPriceFillModel` to `SizeAwareFillModel` to `CompetitionAwareFillModel`. The `FillModel` trait lets you inject a custom order book for fill simulation, making the model extensible without modifying engine code.

**5. L1 / L2 / L3 book fidelity tiers.** The venue `book_type` configuration determines how much fidelity the matching engine uses. With L2 tick data, partial fills walk actual price levels. With L1 bar data, users get probabilistic slippage. The system is honest about what each data tier can model.

**6. Nanosecond-resolution clock throughout.** `UnixNanos` is a newtype over `u64` carrying nanoseconds. Timestamps in data, orders, fills, timers, and logs all share the same unit. There is no float-based time representation that would cause comparison instability.

**7. Fail-fast data integrity.** Fixed-point arithmetic, panic on NaN/overflow, and precision mismatch errors at data load time rather than silent corruption. A strategy cannot unknowingly compute P&L on a NaN fill price.

**8. Streaming backtest for large datasets.** The `run(streaming=True)` / `clear_data()` / `add_data()` pattern allows processing datasets larger than RAM by chunking them. This is documented and has a specific API contract.

**9. Per-instrument order matching engines.** Each instrument gets its own `OrderMatchingEngine` instance inside the `SimulatedExchange`. Matching is fully isolated; a fill on SPY does not share state with a fill on AAPL.

**10. Active adapter ecosystem.** As of v1.228.0, adapters exist for Binance (spot and futures), Databento, Interactive Brokers, Coinbase, OKX, Deribit, Kraken, Hyperliquid, and others. For live trading, the platform covers most liquid markets.

---

## Weaknesses, Footguns, and Critiques

**1. Complexity and learning curve.** This is a production-grade, company-backed system with 5000+ files in the repo. The core abstractions (NautilusKernel, MessageBus, Cache, DataEngine, ExecutionEngine) must all be understood before writing a working strategy. Compare to bt (a single Python class) or vectorbt (NumPy arrays). The documentation is improving but still has gaps, particularly in the Rust API layer.

**2. Dual-language build complexity.** Building from source requires Rust toolchain, maturin, Cython, and coordination between the two. pip-installable wheels exist for supported platforms, but any custom modification to the Rust layer requires a full Rust build. For a portfolio project, this is a significant barrier: reviewers cannot easily run the system without installing the full toolchain.

**3. No corporate actions.** Confirmed gap (GitHub issue #3307). Equity research strategies that span multi-year periods are unreliable without split and dividend adjustment. The engine provides no facility for this; it is entirely the user's responsibility to supply adjusted data.

**4. No survivorship bias tooling.** The system has no mechanism for point-in-time index membership or delisting handling. This is consistent with its origin as a crypto/futures backtester where survivorship bias is less severe. For U.S. equity factor strategies, this is a research-quality limitation.

**5. AT_THE_OPEN and AT_THE_CLOSE orders are rejected in simulation.** The `TimeInForce::AtTheOpen` and `TimeInForce::AtTheClose` enum variants exist but generate `OrderRejected` events in the `SimulatedExchange`. Strategies testing MOO/MOC fills cannot use nautilus_trader's simulation layer out of the box (`crates/execution/src/matching_engine/engine.rs:2996-3008`).

**6. Borrow costs for short selling are not modeled.** There is no `BorrowCostModel` trait or built-in mechanism. A strategy that short-sells stocks will not pay any financing cost in simulation unless the user writes a custom `SimulationModule` that debits the account each period.

**7. Bar-level backtesting has inherent limitations.** With OHLC bars, the engine must guess the intrabar price path (adaptive or fixed OHLC ordering). The system acknowledges this: "~75-85% accuracy" on adaptive ordering. A strategy with both take-profit and stop-loss within a single bar cannot be correctly simulated from bars alone.

**8. API churn.** The version is 1.228.0, not 1.0. Cython is being migrated to PyO3. Breaking changes appear in every release (see `RELEASES.md:56-70`). A project built on nautilus_trader today will need ongoing maintenance as the API evolves. The team does not appear to maintain a stable LTS branch.

**9. Python callback overhead on the hot path.** The Any-based routing path for Python callbacks ("N handlers = N downcasts + N potential clones", `crates/common/src/msgbus/core.rs:47-49`) is materially slower than typed Rust dispatch. A strategy with many subscriptions and complex Python logic will saturate the Python GIL even if the Rust bus itself is fast.

**10. No short-rate or dividend cash flow modeling.** There is no facility for modeling the cash flow effects of dividend payments or stock lending rebates. These matter for total-return backtests spanning multiple years.

---

## Lessons for Our Design

### Things to Copy

**1. Explicit clock abstraction at construction time.** Instantiate the engine with a `TestClock` or a `LiveClock` passed in at construction, not switched at runtime. This makes it structurally impossible to call `SystemTime::now()` in strategy code during a backtest. Every call to `clock.timestamp_ns()` returns simulated time in backtest and real time in live.

**2. The shared kernel pattern.** Do not write a `BacktestStrategy` base class and a `LiveStrategy` base class. Write one `Strategy` base class. The engine underneath (test vs live) provides the clock, data feed, and order routing. This is the single most important architecture decision in nautilus_trader.

**3. The message bus as the event spine.** Components should not call each other directly. A `QuoteTick` arrives, the DataEngine publishes it to the bus, the strategy's handler fires. Order commands go via the bus to the ExecutionEngine. This enforces temporal ordering and makes replay natural: just replay the bus messages.

**4. Latency model via a priority queue of inflight commands.** An `InflightCommand` struct with a delivery timestamp, stored in a `BinaryHeap`. At each time step, drain all commands whose timestamp has been reached. This correctly models the effect of network round-trip on fill prices and sequencing. Even a 100 microsecond static latency model changes outcomes in high-frequency strategies.

**5. Fixed-point arithmetic for prices.** Do not use `f64` for prices, quantities, or P&L. Use integer representations (e.g., price * 10^precision). Eliminates NaN bugs, floating-point accumulation error, and non-determinism from float comparison.

**6. Two-level data granularity contract.** Be explicit about what the system can simulate at each data tier (L1 quote, L2 depth, bars). When bar data is loaded, document what the fill assumptions are and do not claim tick-level accuracy.

**7. `TimeEventAccumulator` for strategy timers.** Strategy timers (e.g., `set_timer("rebalance_daily", ...)`) should be accumulated in a heap during clock advance, not executed immediately. The accumulator ensures timers fire in the correct temporal order relative to market data events.

**8. The `BusTap` for event recording.** Install a tap on the message bus that writes every dispatched message to a log. This is the foundation for both debugging and live-to-backtest replay. It costs one function call per dispatch on the hot path, which is negligible.

### Things to Avoid

**1. The Rust + Python dual-language build.** For a portfolio project where the audience is evaluating engineering judgment, a Python-only implementation with numpy/numba for hot paths is simpler to audit, deploy, and modify. The dual-language build is justified in production (Nautech Systems uses this live) but adds significant barrier to entry for a portfolio reviewer who wants to run the code.

**2. Cython.** The ongoing migration from Cython to PyO3 is costly and creates a period where documentation, type stubs, and examples are inconsistent. For a new project, pick one binding approach and stay with it. If pure Python with selective Cython/numba acceleration is sufficient, use that.

**3. The Any-based routing path for high-frequency data.** The 10x overhead of `&dyn Any` dispatch vs typed dispatch (`crates/common/src/msgbus/core.rs:53`) is significant in a tight backtest loop. If implementing a message bus, prefer typed dispatch for known market data types, even if that means less runtime flexibility.

### Open Questions

**Is matching this level of completeness realistic for a solo portfolio project?** Honest answer: not fully. The 10-model fill library, three-tier fee system, latency model, queue position tracking, and streaming large-dataset support in nautilus_trader represent years of compound work by a team. A portfolio project should identify which subset matters for the intended strategy domain and implement that subset deeply rather than implementing all features shallowly.

For a mean-reversion equity strategy backtester, the critical features are: correct lookahead-bias protection, corporate actions, commission modeling, and basic slippage. The latency model and L2 order book matching are secondary. For an HFT/crypto strategy, the priority order reverses.

The strongest differentiation a portfolio project can achieve is not "more fill models" (nautilus_trader already wins that) but rather "first-class corporate action support and point-in-time index membership for U.S. equities." That is the gap nautilus_trader has explicitly declined to fill (issue #3307), and it is something a focused equity backtester can do well.

---

## Sources

1. **nautilus_trader GitHub repository** (https://github.com/nautechsystems/nautilus_trader) - Primary source. All file:line citations reference the shallow clone at `C:/temp/nautilus`, commit corresponding to v1.228.0.

2. **nautilustrader.io homepage** (https://nautilustrader.io/) - Performance claims (5M rows/second), architecture overview, feature list.

3. **Architecture concept page** (https://nautilustrader.io/docs/latest/concepts/architecture/) - Component diagram, kernel description, event flow, threading model.

4. **Backtesting concept page** (https://nautilustrader.io/docs/latest/concepts/backtesting/) - Fill models, data hierarchy, bar execution semantics, limitations table, streaming mode docs.

5. **Orders concept page** (https://nautilustrader.io/docs/latest/concepts/orders/) - Full order type list, time-in-force options, execution instructions.

6. **RELEASES.md in repo** (`C:/temp/nautilus/RELEASES.md`) - v1.228.0 changelog, maintenance cadence evidence.

7. **BENCHMARKING.md in repo** (`C:/temp/nautilus/BENCHMARKING.md`) - Benchmarking philosophy, tooling (Criterion, iai, CodSpeed), policy on quoting numbers.

8. **`crates/common/src/msgbus/core.rs`** - Typed vs Any-based routing design decision, performance rationale, quoted benchmarks.

9. **`crates/common/src/msgbus/mod.rs`** - Thread-local storage architecture, handler buffers, BusTap interface.

10. **`crates/backtest/src/engine.rs`** - BacktestEngine struct, `run_impl` main loop, kernel instantiation pattern.

11. **`crates/backtest/src/exchange.rs`** - SimulatedExchange struct, InflightCommand priority queue, latency model wiring.

12. **`crates/execution/src/models/fill.rs`** - All 10 fill model implementations, FillModel trait, `get_orderbook_for_fill_simulation` extension point.

13. **`crates/execution/src/models/latency.rs`** - LatencyModel trait, StaticLatencyModel implementation.

14. **`crates/execution/src/models/fee.rs`** - FeeModel trait, FixedFeeModel, MakerTakerFeeModel, PerContractFeeModel.

15. **`crates/execution/src/matching_engine/engine.rs`** - OrderMatchingEngine struct, `process_market_order` showing AT_THE_OPEN/CLOSE rejection.

16. **`crates/common/src/clock.rs`** - Clock trait, TestClock vs LiveClock abstraction.

17. **GitHub issue #3307** (https://github.com/nautechsystems/nautilus_trader/issues/3307) - Corporate actions support request, confirmed open with no planned timeline.

18. **autotradelab.com framework comparison** (https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded) - Third-party assessment of learning curve, strengths, weaknesses.

19. **deepwiki.com nautilus_trader page** (https://deepwiki.com/nautechsystems/nautilus_trader) - Architecture overview, hybrid Rust/Python model description.
