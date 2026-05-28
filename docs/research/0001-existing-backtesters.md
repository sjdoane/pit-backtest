# 0001: Existing open-source backtesters, comparative analysis

Status: draft, research phase 1.

## 1. Purpose and method

The goal of this survey is to understand the design choices, structural correctness properties, execution realism, and failure modes of the major open-source Python backtesting libraries before we design our own. The audience is a quant researcher or recruiter who wants to know that the design choices we eventually make were made after reading what already exists, not in isolation.

Six libraries are covered: zipline (the original Quantopian codebase and the actively-maintained `zipline-reloaded` fork), backtrader, vectorbt (open-source edition, with notes on the `vectorbtpro` commercial fork), bt, QSTrader, and nautilus_trader. For each library, a separate analysis document under [`sources/`](sources/) reads the actual source code, the official documentation, the issue tracker, and third-party critiques, and produces a structured technical writeup with file-and-line citations. This document is a synthesis across the six; it cites the per-library documents for detail.

Selection criteria: each library is either the canonical reference for an architectural style (QSTrader for textbook event-driven, zipline's Pipeline for cross-sectional factor research, vectorbt for vectorized parameter sweeps, nautilus_trader for production-grade event-driven, bt for portfolio composition, backtrader for indicator chaining), or has enough installs and visibility that a quant researcher would expect us to have read it.

What this survey is not: a feature-comparison checklist that scores each library on a points basis. The interesting question is which structural and correctness properties each library guarantees, and where each one silently bites the user.

## 2. The libraries at a glance

| Library | Architecture | Lookahead protection | Corp actions | Execution realism | Maintenance |
|---|---|---|---|---|---|
| zipline-reloaded | Event-driven, daily/minute, Cython core | Strong (Pipeline + `window_safe`); weak in `handle_data` | Splits, cash divs adjusted; no point-in-time membership | Multi-model slippage, commissions; no MOO/MOC, no borrow | Active fork (Jansen) |
| backtrader | Event-driven, Lines ring buffer, Python | Strong at indicator chain (`_minperiod`); leaks via positive index | None native; user supplies adjusted prices | Two slippage params, percent/fixed commission, partial fills via fillers | Soft-abandoned since 2023 |
| vectorbt (OSS) | Vectorized API, sequential Numba kernel | Convention only (`fshift(1)` discipline) | None | Flat bps slippage, one order per bar | OSS frozen at v1.0.0; PRO is paid |
| bt | Tree composition, AlgoStack, daily | `target.now` clock; same-bar fill is a footgun | v1.2.0 adds `CorporateActions` algo; no PIT membership | Commission functions and nonlinear `CostModel`; no order queue | Active one-maintainer |
| QSTrader | Event-driven, schedule-based, Python | Event-timing intended; not enforced | Adjusts open from `Adj Close`; no PIT, no delistings | Slippage and market impact are TODO stubs; only market orders | Minimal commits since 0.2.0 |
| nautilus_trader | Event-driven, Rust core, Python strategies | Structural via clock injection + msgbus | None (issue #3307 open) | 10 fill models, latency queue, partial fills | Very active, weekly releases |

(Detail and citations for every cell are in the per-library files. References to specific file paths and line numbers throughout this document refer to those source files unless explicitly attributed otherwise.)

The first observation worth stating bluntly: no library in this set handles a survivorship-bias-free U.S. equity universe correctly out of the box. That is the most consistent gap across the field, and we return to it in Section 5.

## 3. Architectural taxonomy

The six libraries occupy two and a half clusters.

**Pure event-driven (zipline-reloaded, backtrader, bt, QSTrader, nautilus_trader).** Each iterates over time and dispatches per-bar handlers. The differences are in granularity (zipline's daily/minute clock; QSTrader's four-event-per-day schedule; nautilus_trader's nanosecond clock with arbitrary events) and in what the "event" is. zipline and QSTrader use a clock generator that yields typed events; nautilus_trader uses a message bus with publish/subscribe; backtrader and bt collapse the dispatch into property accessors and a `_next` walk.

The event-driven cluster splits further on a single design decision: does the backtest share the same execution machinery as a live trading deployment? Only nautilus_trader answers yes. Its `NautilusKernel` is instantiated by both the `BacktestEngine` and the live `LiveNode`; the only swap is `TestClock` versus `LiveClock`, both implementing the same `Clock` trait. zipline and QSTrader are research-only; their broker objects are simulation-only and there is no documented path to live trading without rewriting the strategy. backtrader has live broker adapters but they were always second-class and are now unmaintained. bt is strictly research.

This kernel-sharing property is the single most important architectural decision in nautilus_trader. The most common failure mode in real-world quantitative trading is backtest-live divergence, and the only structural defense is to use the exact same execution code in both. We should expect to copy this pattern even though we do not (yet) target live trading: it imposes discipline on the API surface, forcing every component to operate against an injected clock and an explicit data interface, neither of which depends on whether the data is real or simulated.

**Vectorized with sequential kernel (vectorbt).** The library presents arrays as inputs and uses Numba-compiled kernels to iterate column-major or row-major over them. The author himself stated in discussion #185 that "vectorbt doesn't actually do any vectorized backtesting, it follows a sequential approach just like most other libraries"; "vectorized" refers to the input contract, not the simulation semantics. The architectural identity is closer to "fast parameter sweep" than "fast backtest"; vectorbt's killer feature is taking 10,000 hyperparameter configurations as a 2-D array and producing 10,000 backtest results in seconds because the per-configuration overhead has been moved into a single compiled kernel call.

**Hybrid (vectorbtpro, conceptually).** The commercial fork claims "hybrid backtesting" with richer callbacks, lifecycle hooks, and `nextopen`/`nextclose` price semantics. Based on public documentation it is the same Numba kernel with more API surface; the simulation paradigm is identical to the OSS. We do not have source access. The label matters only because it confirms that even the author of the most successful vectorized backtester eventually built event-driven semantics on top.

**Implication for our design.** Event-driven is correct for production-realistic simulation; vectorized is correct for parameter screening. These serve different decision contexts and should not be the same code path. A solid design provides both as explicit modes with different semantics and different reported confidence in the result. The most dangerous failure mode is to present sweep-mode results as if they were production-validated results, which is what most of vectorbt's tutorial material implicitly does.

## 4. Lookahead bias protection: structural versus convention

Lookahead bias protection is the cleanest axis for separating the libraries by quality. Three categories emerge.

**Structural at the cross-section: zipline's Pipeline.** Pipeline operates on a `(dates, assets)` matrix produced by a separate engine that runs before market open. Each `Term` has a `window_length` and a `window_safe` flag (default `False`). If a term with `window_length > 1` references an input that is not `window_safe`, Pipeline raises `NonWindowSafeInput` at construction time, not at runtime:

```python
# zipline-reloaded src/zipline/pipeline/term.py:612-615
if self.window_length > 1:
    for child in self.inputs:
        if not child.window_safe:
            raise NonWindowSafeInput(parent=self, child=child)
```

The Pipeline engine fetches `extra_input_rows` so the first output row of a 20-day moving average has 19 leading rows without ever giving the factor visibility into the current observation date. Outputs are delivered to the algorithm via `pipeline_output()` in `before_trading_start`, which runs before market open, structurally pre-emptying the bar handler. This is the gold standard for cross-sectional factor computation; no other library in the survey approaches it.

**Structural at the indicator chain: backtrader's `_minperiod`.** Every indicator declares its lookback through `addminperiod(N)`. The metaclass propagates `max(child._minperiod)` up the dependency graph at construction time, and the engine refuses to call `strategy.next()` until every registered indicator is warm. For a 20-day SMA feeding a 14-day RSI, the framework computes the composite warmup of 33 bars without any user code. No other Python backtester does this compositionally. The mechanism is unique to backtrader and is its strongest technical contribution.

**Structural at the clock: nautilus_trader's clock injection.** The `Clock` trait is implemented by `TestClock` (returns simulated time) and `LiveClock` (returns wall clock). Construction-time injection means it is structurally impossible for a strategy to read the real system clock during a backtest. The `DataEngine` advances the clock before publishing each event; the cache only contains data that has been published; strategies querying the cache cannot see future data. This is the right pattern when "time" is the lookahead axis, complementing Pipeline (which addresses "what data is available at this date") and `_minperiod` (which addresses "is this indicator warm").

**Convention only: vectorbt and bt and QSTrader (and zipline's `handle_data`, partially).** vectorbt's `Portfolio.from_signals` requires `signals.vbt.fshift(1)` to avoid lookahead, and the documentation is inconsistent about whether the example code actually applies the shift (see vectorbt issue #190). The default execution price is `close[i]`, meaning a signal computed on bar N's close executes on bar N's close: a textbook lookahead unless the user explicitly shifts. bt does the same thing implicitly; orders fill at `target.now`'s close, which is the same close the algo used to compute weights, with no warning and no default lag. QSTrader intends to enforce the convention through its event sequence (signals update at `market_close`, rebalance fires at the configured schedule), but does not validate that user data carries the correct `ts_init` semantics, and the schedule-driven design allows configurations that effectively short-circuit the timing protection.

zipline's `handle_data` sits between these categories. The simulation clock controls what `data.current()` and `data.history()` can return, which is correct, but a sufficiently determined user can call `data.history(asset, "close", bar_count=N, frequency="1d")` with a large `bar_count` and index manually into the array. More importantly, zipline's daily-mode market orders fill at the next session's *close* price, not the next session's *open* (per quantopian issue #2011). This is a subtle bias: the simulator gives the fill access to the close price of the next day, which the strategy could not have seen at the time the fill would have executed. The error is small per trade and easy to miss across an aggregate, which is the worst kind of bias to ship into a research environment.

**The taxonomy and the lesson.** Structural protection is two orders of magnitude more valuable than convention because the audience that builds backtesters is not always the audience that uses them. Pipeline plus `_minperiod` plus clock injection plus a typed API around data access (where the data interface only exposes time-sliced views) covers the vector space. Our design must enforce lookahead protection through API and types, not through example code. If we keep one feature from each of the structural-protection libraries, we keep these three.

## 5. Corporate actions and survivorship: the systemic blind spot

Across all six libraries, this is the dimension where the field is collectively worst. The findings:

- **zipline** handles splits and cash dividends correctly through the `SQLiteAdjustmentReader` with proper `dt` / `perspective_dt` semantics, and provides `auto_close_date` for delisting cleanup, but provides no point-in-time index membership data. Quantopian issue #2641 explicitly requested S&P 500 membership primitives and was never resolved.
- **backtrader** has zero corporate-action handling. The user supplies pre-adjusted data and the framework provides no warning if they do not.
- **vectorbt** has zero corporate-action handling. Survivorship bias and adjustment methodology are inherited entirely from the input arrays.
- **bt** added a `CorporateActions` algo and `CloseDead` handler in v1.2.0 (April 2026), making it the most recent addition to the field; this handles splits and cash dividends on ex-date. No point-in-time membership; the user supplies the universe.
- **QSTrader** ratio-adjusts the open price from `Adj Close` when present, which is correct for daily research, but the `DynamicUniverse` "does not currently support removal of assets" per the source comment. Backtests using current-day S&P 500 members will silently produce survivorship-biased results.
- **nautilus_trader** explicitly punts the entire area. Issue #3307 ("Add support for ticker name changes, stock splits and corporate actions in general") is open with no assignee. The official documentation reads: "Corporate actions: Stock splits, dividends not modeled."

The combined message is striking. The field's most rigorous production-grade event-driven backtester (nautilus_trader) does not handle corporate actions, and the next-most-rigorous research framework (zipline) handles the math correctly but lacks the data layer. Every library expects the user to source point-in-time index membership from a paid third-party vendor (Norgate, Sharadar) and to encode it into the framework's universe abstraction by hand.

This gap is the clearest opportunity for the design we are building. A focused U.S. equity backtester that treats point-in-time index membership as a first-class data type, validates universe specifications against price availability and corporate-action ex-dates at backtest-construction time, and represents delistings as explicit events with last-trade prices, would solve the single problem that the existing landscape collectively gets wrong. The design implication is that our data layer must have an opinionated representation of: (a) asset lifetimes (zipline-style), (b) index membership as a `(date, asset) -> bool` mask separate from existence, (c) split and dividend adjustments through a `dt` / `perspective_dt` interface (zipline-style), and (d) delisting events with last-trade prices. None of these is novel individually; the contribution is integrating them coherently in one open-source library.

## 6. Execution and cost modeling

The field spans roughly four orders of magnitude in execution realism between the worst and best in this survey.

**Worst: QSTrader.** `simulated_broker.py:67-68` reads `self.slippage_model = None  # TODO: Implement` and `self.market_impact_model = None  # TODO: Implement`. Bid and ask are hardcoded to the mid (`daily_bar_csv.py:175-176`). Only market orders are implemented. There is no holiday calendar. Every backtest result is an upper bound on real performance, not an estimate of it.

**Almost as weak: vectorbt and bt.** Both apply a flat-percentage slippage parameter to a single mid price; neither models bid-ask spread as a function of the actual quote (when L1 data is provided) and neither models market impact as a function of bar volume by default. vectorbt has no order queue (per discussion #185: "there is no order management, once you issue an order command, it gets executed/rejected immediately"); bt has no order queue either (signal and fill collapse into the same `allocate()` call). bt's v1.2.0 (April 2026) added Almgren-Chriss and square-root `CostModel` classes, which is a step up from a flat parameter, but the framework still fills at the bar close where the signal was computed.

**Middle: backtrader.** Two slippage parameters (`slip_perc`, `slip_fixed`), capped at the bar high/low. Partial fills via a pluggable `Filler` (but no filler is active by default). Borrow/interest via an `interest` parameter. Order types include MarketIfTouched and stop-trail variants. Backtrader is the only library in the survey where the default execution semantics are correct (market order placed on bar N fills at bar N+1's open), which makes it the best baseline for daily-bar correctness despite the architecture's other limitations.

**Strong: zipline-reloaded.** Multiple slippage models including `VolumeShareSlippage` with quadratic impact (`price_impact * volume_share^2`), `FixedBasisPointsSlippage` as default for equities with a volume cap, `VolatilityVolumeShare` for futures (Almgren-style `eta * sigma * sqrt(psi)`). Commission models for per-share, per-trade, per-dollar, per-contract. Pluggable everything via abstract base classes. The gap from "good" to "production-grade" is the daily-mode close-price fill timing (next-bar close, not next-bar open) and the absence of any borrow-cost model.

**Best: nautilus_trader.** Ten built-in fill models including tier-based ones (`TwoTierFillModel`, `ThreeTierFillModel`), liquidity-aware ones (`CompetitionAwareFillModel`, `VolumeSensitiveFillModel`), and a `LimitOrderPartialFillModel` that walks order book levels. The `FillModel` trait's `get_orderbook_for_fill_simulation` method lets users inject a synthetic book with any depth profile. Latency is modeled explicitly via `StaticLatencyModel` and an `InflightCommand` priority queue: order submissions are delayed by the modeled round-trip time before reaching the matching engine, changing both the fill price and the event sequencing. Partial fills walk actual book levels when L2/L3 data is available. Time-in-force includes AT_THE_OPEN and AT_THE_CLOSE as enum variants, although the simulation explicitly rejects both with `OrderRejected` (`matching_engine/engine.rs:2996-3008`), making them live-only as currently implemented.

**The pattern and the lesson.** Execution realism is unevenly distributed and most libraries get it wrong by underspecifying. The "best" library in the survey explicitly punts on MOO and MOC simulation, even though those order types are economically central for cash equities (closing auctions are 8 to 10 percent of daily volume in U.S. large-caps). The "worst" library leaves the slippage model as `None  # TODO: Implement`.

The design implications are direct. Our slippage and commission models must be required constructor parameters of the simulation engine with no zero-default escape hatch; calling code that fails to specify them should not compile or should fail at startup. We should support at least: a fixed-bps model, a volume-participation model with quadratic impact (Almgren-style), and a square-root market-impact model with a volume-sensitive coefficient (Bouchaud-Almgren style). MOO and MOC should be first-class order types with explicit fill semantics, not aliases for market orders. Borrow costs for short positions should be a separate first-class cost component, not a hidden constant. Latency modeling can be deferred but the API should not foreclose it; if we are clever about the order-submission interface (queued commands rather than synchronous calls), latency can be added later as a single component without re-architecting.

## 7. Performance and scaling

Performance is bimodal. Vectorbt and nautilus_trader both ship fast hot paths (Numba and Rust respectively). The other four are pure Python (with thin Cython or Numba touchups for inner loops) and reach the Python dispatch ceiling on large minute-bar universes.

The relevant numbers are reported only sporadically, and benchmarks across libraries are not comparable because no two use the same dataset and configuration. From the per-library documents:

- **vectorbt** processes 1 million orders in 42 to 53 milliseconds per its own benchmarks, and is 14x faster than native pandas for rolling z-score operations. The killer use case is testing 10,000 MA-window combinations simultaneously through `vbt.MA.run_combs` because all combinations share one kernel call.
- **backtrader** maintainer benchmark on 100 stocks and 2 million candles: 348 MB and 135 seconds in default mode, 49 MB and 67 seconds in `exactbars` (ring-buffer) mode. PyPy reduces wall time to 57 seconds.
- **zipline** Cython core makes the clock tick fast, but `handle_data` user code dispatches once per bar, and minute-bar backtests over 5+ years across thousands of assets are reported as multi-hour runs. Pipeline computes factors across all assets in a single numpy call, which is the correct architecture for cross-sectional scale.
- **nautilus_trader** typed-handler dispatch is roughly 10x faster than `&dyn Any` dispatch for noop handlers (`crates/common/src/msgbus/core.rs:53`); the project policy is "don't claim a win without a bench" and they maintain Criterion-driven nightly CI. The website's "5 million rows per second" claim refers to the streaming data catalog and is not strictly comparable to per-bar simulation throughput.

**Scaling architectures.** Three distinct approaches are visible: vectorbt's "pack all parameter configurations into a single 2-D kernel call" approach, zipline's "compute all assets simultaneously through a numpy-based Pipeline engine" approach, and nautilus_trader's "single-threaded fast kernel per backtest, parallelize across runs via separate processes" approach. The three are not mutually exclusive but they address different scaling axes (parameter sweep, cross-section, replay throughput).

The relevant question for our design is which scaling axis matters most. For a portfolio project oriented toward correct U.S. equity research, the cross-sectional axis matters most: a Pipeline-equivalent for factor computation across 1000 to 5000 names. Numba-accelerated parameter sweep mode comes second. Pure replay throughput matters least because the limit on research is researcher attention, not CPU cycles. Our memory model should be streaming-friendly (process data without loading the entire 20-year history into RAM for thousands of assets) but should not optimize for sub-millisecond per-bar dispatch.

## 8. Maintenance and ecosystem signals

A short pass on maintenance, because we should not learn from libraries that are silently rotting.

- **zipline-reloaded**: actively maintained by Stefan Jansen, v3.1.1 (July 2025), tied to his "Machine Learning for Algorithmic Trading" textbook. The original Quantopian repo is archived. Active fork is the only viable path.
- **backtrader**: last release April 2023, maintainer essentially absent. Two community forks (`backtrader2`, `cloudQuant/backtrader`) carry on, neither with official standing. Any new build on backtrader inherits the risk that Python 3.13+ compatibility lapses.
- **vectorbt**: OSS frozen at v1.0.0; the same author maintains vectorbtpro behind a $20/month paywall. The OSS repo received one commit in 2026. Practitioners who need features beyond signal research are channeled to the subscription.
- **bt**: actively maintained by Philippe Morissette, v1.2.0 (April 2026). One-maintainer risk. ffn dependency is tightly coupled.
- **QSTrader**: stable but minimal commits since v0.2.0. Mostly dependency-version housekeeping. Educational, not production.
- **nautilus_trader**: Nautech Systems-backed, v1.228.0 (May 2026), weekly to biweekly releases, Criterion-driven nightly CI, active adapter ecosystem. The project policy and structure read like a serious commercial product with an open-source surface.

**The ecosystem signal.** Two libraries (nautilus_trader and zipline-reloaded) are genuinely active, but they target different design problems: nautilus_trader targets production multi-asset event-driven simulation with strong execution modeling; zipline-reloaded targets U.S. equity factor research with strong cross-sectional correctness. The intersection (production-grade U.S. equity factor research with corporate-action correctness) is uncovered by both. That intersection is the design space we are entering.

## 9. Aggregated lessons

What to copy, with attribution.

1. **Pipeline-style cross-sectional engine with `window_safe` enforced at construction (zipline)**. A separate path from the bar handler, operating on a `(dates, assets)` matrix, with explicit lookback semantics and an asset-lifetimes mask. This is the cleanest published mechanism for cross-sectional factor research and we should treat it as table stakes.
2. **`_minperiod` propagation for indicator chains (backtrader)**. Composable indicator lookback, automatically computed through the dependency graph, with `next()` suppressed until all warmups complete. Necessary for any indicator-based strategy authoring.
3. **Clock injection at construction (nautilus_trader)**. Single `Strategy` base class. The clock and the data feed are construction-time arguments to the engine. No `BacktestStrategy` vs `LiveStrategy` split. This forces the API to be agnostic to where the data is coming from.
4. **Message bus or generator-of-typed-events for the dispatch spine (nautilus_trader, QSTrader)**. Components do not call each other directly; they publish and subscribe through a common bus. The bus's tap interface enables replay. Even if we do not target live trading, the discipline of writing everything against a bus protects us from the silent coupling that bt's tree composition and backtrader's metaclass machinery both exhibit.
5. **AlgoStack composition for portfolio policy (bt)**. A pipeline of typed algos with Select / Weigh / Constrain / Rebalance phases, communicating through a typed context object, short-circuiting on `False`. This is the right abstraction for portfolio-construction logic that needs to be testable in isolation.
6. **Latency modeled as an `InflightCommand` priority queue (nautilus_trader)**. Even a 100-microsecond static latency model changes outcomes for high-frequency strategies, and the architecture allows it to be added without re-architecting the order-submission flow.
7. **The `dt` / `perspective_dt` adjustment paradigm (zipline)**. Every historical price carries two timestamps: when the price occurred and when it is being observed from. Adjustments apply only in the half-open window. Encode this into the data layer from the start.
8. **Fixed-point arithmetic for prices and quantities (nautilus_trader)**. No `f64`. Eliminates NaN and float-accumulation bugs. Fail fast on overflow.
9. **Pluggable execution models with a tight interface (zipline `SlippageModel`, nautilus `FillModel`)**. A small abstract base class with one or two methods, registered per asset class. The pluggability is what allowed nautilus_trader to ship ten built-in fill models.
10. **Numba-accelerated sweep mode as a separate path from event-driven (vectorbt)**. A research tool with explicit approximations: market orders at close, flat slippage, one order per bar per asset. Results are explicitly labeled as research estimates, not production estimates.

What to avoid, with attribution.

1. **Same-bar fill at signal close as default (bt, vectorbt, QSTrader for some paths)**. The default for any market-style order must be the next bar's open, with same-bar close as an explicit opt-in. This is the single most common source of silent overfitting in the surveyed libraries.
2. **Daily-mode fills at next-bar close (zipline)**. A subtle bias that survived a decade of issues. Default to next-bar open. Configure intrabar timing explicitly.
3. **Convention-based lookahead protection (vectorbt's `fshift(1)`)**. The shift is brittle, inconsistent across documentation, and silent on failure. Make execution timing structural through API types.
4. **Tree composition as the primary engine architecture (bt)**. The tree is right for hierarchical portfolios but wrong for order routing, partial fills, and cross-strategy state. Keep the tree as an optional composition metaphor; do not let it own the engine spine.
5. **Inverted indexing (`[0]` = current, `[-1]` = past, backtrader)**. Non-Pythonic, surprising, and the source of most user confusion in the most technically interesting library in the survey. Use explicit named accessors or standard Python negative indexing.
6. **Mandatory benchmark in the simulation core (zipline)**. The benchmark is a reporting concern, not a simulation concern. Do not couple it into the engine constructor.
7. **Opaque proprietary data bundle format (zipline's bcolz)**. Use Parquet or Arrow as the on-disk format. Users should be able to populate the data layer with standard tools.
8. **Mandatory live-trading complexity for research (nautilus_trader's Rust + Cython build)**. For a portfolio project, the dual-language build is a barrier to entry for the audience of recruiters and reviewers. Pure Python with selective Numba is the right scaling story until a live use case justifies the complexity.
9. **Silent commission rescaling (backtrader's `/100.0` when `percabs=False`)**. Every cost parameter must have an unambiguous unit in its name and must validate its range. Never silently rescale.
10. **`# TODO: Implement` left as production default (QSTrader's slippage and market impact)**. Required parameters with no zero-default escape hatch. The simulation engine should not be runnable with cost modeling disabled.

## 10. Open design questions surfaced

Items the survey did not resolve, to be answered in the methodology phase and the architecture ADR.

- **Pipeline-equivalent's data model.** Should our cross-sectional engine operate on adjusted prices (zipline's choice, correct for return computation but wrong for absolute-price signals) or unadjusted with explicit adjustment application? How do we expose both without doubling the API surface?
- **Where does the portfolio policy layer (Algo-stack equivalent) fire in the event sequence?** bt collapses signal and fill; nautilus_trader's portfolio is a passive accounting layer; zipline's `before_trading_start` is the cleanest hook. We need a precise specification of when the policy runs relative to data arrival and order acknowledgment.
- **How do we handle MOO and MOC orders given that nautilus_trader rejects them in simulation?** Cash equity strategies need closing-auction fills. We need an explicit specification of auction-print semantics, including the auction-vs-continuous spread differential.
- **How aggressively do we model the open-to-close path within a bar?** Daily bars give us OHLC but not the intrabar path; for stop-loss and take-profit logic inside a single bar, we need an explicit ordering assumption. nautilus_trader's "adaptive OHLC ordering" reports approximate 75 to 85 percent accuracy; we need a defensible default.
- **What is the right point-in-time data API shape?** A `(date, asset) -> bool` membership mask, separate from existence; how does the user supply it; how do we validate it against price availability and corporate-action ex-dates at backtest-construction time?
- **Sweep mode versus event-driven mode: how are results labeled to prevent the vectorbt failure mode?** Strategies validated only in sweep mode must not feed capital deployment decisions. We need an explicit confidence-tier annotation on backtest results.
- **Should we provide a Numba acceleration path at all?** vectorbt proves it works for parameter sweeps; the cost is a separate kernel that drifts from the event-driven path. If sweep mode is honestly labeled, the drift may be tolerable.

These will be addressed in the methodology research (phase 2), the spec critique (ADR 0001), and the architecture ADR (0003).

## 11. Source documents

Per-library detail and citations:

- [`sources/zipline.md`](sources/zipline.md): zipline-reloaded v3.1.1, Pipeline, DataPortal, slippage/commission models, the close-price fill timing bias.
- [`sources/backtrader.md`](sources/backtrader.md): backtrader v1.9.78.123, Lines abstraction, `_minperiod` propagation, the `/100.0` commission silent rescaling.
- [`sources/vectorbt.md`](sources/vectorbt.md): vectorbt v1.0.0 (OSS) plus notes on vectorbtpro, Numba kernel architecture, the `fshift(1)` discipline footgun.
- [`sources/bt.md`](sources/bt.md): bt v1.2.0, AlgoStack pattern, tree composition, the new `CorporateActions` algo and Almgren-Chriss `CostModel` classes.
- [`sources/qstrader.md`](sources/qstrader.md): QSTrader v0.3.0, four-component event-driven separation, slippage and market impact TODO stubs, schedule-driven architecture.
- [`sources/nautilus_trader.md`](sources/nautilus_trader.md): nautilus_trader v1.228.0, message bus, NautilusKernel kernel-sharing pattern, latency model, fill model library, the corporate-actions gap (issue #3307).

## 12. Status and next steps

This document is the deliverable of research phase 1. The next milestones, per [`docs/ROADMAP.md`](../ROADMAP.md):

- **Research phase 2 (methodology canon)**. Synthesize the literature: Lopez de Prado AFML chapters 11 through 15 on backtest validity, Bailey and Lopez de Prado on the probability of backtest overfitting and the deflated Sharpe, Almgren and Chriss on optimal execution, point-in-time data treatment, and at least three practitioner postmortems on real-world backtest failures.
- **ADR 0001: spec critique and skeptical review**. The current spec is opinionated about features but not yet stress-tested. A skeptical-reviewer agent persona will critique it, and the response will be captured.
- **Roadmap, phased M1 through Mn**. Each milestone independently demoable. M1 validates against a buy-and-hold SPY known-answer test and a deterministic hand-computable strategy.
- **ADR 0003: core architecture sketch**. Class and protocol hierarchy, event loop diagram, the policy-vs-execution layer split, the data-layer point-in-time API. Reviewed by the same skeptical-agent persona before lock-in.

Only after these land does engine implementation begin.
