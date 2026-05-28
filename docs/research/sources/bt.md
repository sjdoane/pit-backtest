# bt Analysis

**Library:** `bt` by Philippe Morissette
**Repository:** https://github.com/pmorissette/bt
**Version analyzed:** 1.2.0 (commit `2630651`, 2026-05-05)
**Companion library:** `ffn` (https://github.com/pmorissette/ffn)

---

## Executive summary

- bt is a **daily-bar portfolio backtesting framework** organized around a tree of `Node` objects (Strategy and SecurityBase) with an algo-stack execution model. The tree allows hierarchical portfolio composition (a strategy of strategies), which is its defining architectural feature.
- **Maintenance is active**. Version 1.2.0 shipped April 2026, adding nonlinear cost models (Almgren-Chriss, square-root law) and a `CorporateActions` algo. The README still says "alpha stage," which understates the actual maturity of the codebase.
- **Biggest strength:** the `AlgoStack` pattern makes portfolio rebalancing policies (select, weigh, constrain, rebalance) composable and individually testable. A complete daily equal-weight strategy is four lines of algo configuration. This is genuinely well-designed for that problem class.
- **Biggest weakness:** all fills happen at the bar's closing price with no latency, no partial fills, and no order book. The same bar's close price that triggers a signal is the execution price. For any strategy where fill assumptions matter, bt's numbers cannot be trusted without significant wrapping.
- **Key lessons:** adopt the `AlgoStack` composition pattern for portfolio policies; do not adopt the tree as the primary architecture for an event-driven engine; model execution with an explicit order object separate from position accounting.

---

## Project status and maintenance

bt is maintained by Philippe Morissette (morissette.philippe@gmail.com). As of the latest shallow clone (commit `2630651`, merged 2026-05-05), development is active. Recent releases:

| Version | Date | Notable change |
|---|---|---|
| v1.2.0 | 2026-04-25 | `CorporateActions` algo, nonlinear cost models (SqrtCostModel, AlmgrenChrissCostModel) |
| v1.1.5 | 2026-03-24 | Python 3.14 support, progress bar fix |
| v1.1.4 | 2026-03-24 | Floating point fixes, pandas 3 FutureWarning in WeighInvVol |
| v1.1.3 | 2024-02-23 | Margin algo, pandas 3 support |
| v1.1.2 | 2023-04-12 | Dropped Python 3.8 |

Source: [Releases page](https://github.com/pmorissette/bt/releases)

The PyPI classifier is "Beta" (classifier `4`) despite the README saying alpha. The project has ~2.9k GitHub stars and 479 forks (as of 2026). It is a one-maintainer project; there is no organizational backing.

**ffn relationship:** bt imports ffn as a first-class dependency (`ffn>=1.1.2`, `pyproject.toml:38`). `ffn` provides performance statistics (`GroupStats`, `PerformanceStats`), financial math (`calc_total_return`, `limit_weights`, `get_num_days_required`), and the `Result` class that wraps bt's backtest output inherits from `ffn.GroupStats` (`backtest.py:458`). If ffn stagnates, bt's reporting layer stagnates with it. Quants used to Sharpe ratios, drawdown tables, and monthly return heatmaps get all of this via bt for free because of ffn, but they are also tightly coupled to ffn's opinions on how those statistics are computed.

---

## Architecture

### Tree-based composition

bt's core primitive is `Node` (`core.py:24`). Both `StrategyBase` and `SecurityBase` inherit from it. A `Strategy` node holds an ordered list of children (`_childrenv`); each child is either another `Strategy` or a `SecurityBase`. This forms a directed tree rooted at the top-level strategy.

```python
# core.py:76-108
def __init__(self, name, parent=None, children=None):
    self.name = name
    self.children = {}
    self._lazy_children = {}
    self._universe_tickers = []
    self._childrenv = []  # Shortcut to self.children.values()
    ...
    if parent is None:
        self.parent = self
        self.root = self
        self.integer_positions = True
    else:
        self.parent = parent
        parent._add_children([self], dc=False)
    self._add_children(children, dc=True)
    self.now = 0
    self.root.stale = False
```

`StrategyBase.setup()` (`core.py:563`) propagates the price universe down the tree at initialization time. Each child receives the same universe and filters it to only the tickers it owns. Strategy children create a synthetic column in the parent's universe keyed to their NAV price (`core.py:835-838`), enabling a parent strategy to treat a sub-strategy exactly like a security when computing weights.

The `root.stale` flag (`core.py:886`) is a lazy-invalidation mechanism: any allocation or transaction marks the root stale, and the next access to `.value`, `.weight`, or `.price` on any node triggers a full-tree `update()` walk. This avoids redundant recalculations but means there is no explicit event queue, state changes propagate through property accessors.

### Algo stack pattern

`AlgoStack` (`core.py:2020`) is a sequential pipeline where each algo receives the `Strategy` as its sole argument and returns a `bool`. Returning `False` short-circuits the rest of the stack:

```python
# core.py:2038-2044
def __call__(self, target):
    # normal running mode
    if not self.check_run_always:
        for algo in self.algos:
            if not algo(target):
                return False
        return True
```

Algos communicate via `target.temp` (a dict cleared each bar, `core.py:2099`) and `target.perm` (persistent dict). This is a blackboard pattern. A standard rebalancing stack looks like:

```
RunMonthly() -> SelectAll() -> WeighEqually() -> Rebalance()
```

`SelectAll` writes `target.temp['selected']`; `WeighEqually` reads `selected` and writes `target.temp['weights']`; `Rebalance` reads `weights` and calls `target.rebalance()` for each security. If any algo returns False (say the date is not month-end), the whole stack short-circuits and no trades are generated.

The `run_always` decorator (`algos.py:18`) is an escape hatch: algos marked with it run even when a prior algo returned False. This is used by `RebalanceOverTime` to continue spreading a rebalance across days after the trigger algo stops firing.

### Strategy.run(): the central execution loop

`Strategy.run()` (`core.py:2097`) is the method called by the backtest on each bar:

```python
# core.py:2097-2107
def run(self):
    # clear out temp data
    self.temp = {}

    # run algo stack
    self.stack(self)

    # run children
    for c in self._childrenv:
        c.run()
```

The backtest loop (`backtest.py:336-355`) drives the overall simulation:

```python
# backtest.py:336-352
self.strategy.update(self.dates[0])

for dt in self.dates[1:]:
    # update strategy
    self.strategy.update(dt)

    if not self.strategy.bankrupt:
        self.strategy.run()
        # need update after to save weights, values and such
        self.strategy.update(dt)
```

The sequence per bar is: (1) `update(dt)` marks `now = dt` and walks children to compute NAV; (2) `run()` executes the algo stack and generates trades; (3) `update(dt)` again recomputes NAV post-trade. Orders generated in step 2 fill immediately at the current bar's close price. There is no pending order queue.

### How this differs from event-driven or vectorized

Vectorized frameworks (like VectorBT) compute the entire price history in NumPy arrays upfront. bt processes one date at a time (event-step), but it is not truly event-driven in the Zipline/Nautilus sense because there are no order objects with separate fill events. An algo calls `target.rebalance(weight, child, ...)`, which internally calls `SecurityBase.allocate()` (`core.py:1486`), which computes the quantity, applies commission, and adjusts parent capital, all in a single synchronous call. The "order" and "fill" are the same operation.

---

## Lookahead bias protection

### How `strategy.now` is used

`strategy.now` is set by `StrategyBase.update(date, ...)` (`core.py:716`). The critical protection is in the `universe` property:

```python
# core.py:515-519
@property
def universe(self):
    if self.now == self._last_chk:
        return self._funiverse
    else:
        self._last_chk = self.now
        self._funiverse = self._universe.loc[: self.now]
        return self._funiverse
```

`target.universe` is always windowed to `[:target.now]`. Any algo that uses `target.universe` to compute signals (e.g., `StatTotalReturn` at `algos.py:946-953`) sees only data up to and including the current bar:

```python
# algos.py:946-952
def __call__(self, target):
    selected = target.temp["selected"]
    t0 = target.now - self.lag
    if target.universe[selected].index[0] > t0:
        return False
    prc = target.universe.loc[t0 - self.lookback : t0, selected]
    target.temp["stat"] = prc.calc_total_return()
    return True
```

The `lag` parameter in `StatTotalReturn` and similar algos is the mechanism for enforcing a publication lag (e.g., a factor that is only available one day after the price date). Omitting the lag uses `DateOffset(days=0)`, meaning the signal uses the same day's closing price, which is fine for rebalancing on close, but represents implicit fill-at-close assumption.

### Failure modes

**1. User-computed signals passed as external data.** If a user pre-computes signals outside bt and passes them as `additional_data`, bt performs no validation that those signals are point-in-time. The entire lookahead burden falls on the user's data pipeline.

**2. The bar close is both signal and fill.** All orders fill at `target.now`'s close price, the same price the algo used to compute weights. In practice, for daily strategies this is the standard academic assumption (signal computed on day T close, fills at day T+1 open or close). bt silently uses T close for both. Users who want T+1 open fills must shift their price series by one day before passing it to `Backtest`, which is a non-obvious step.

**3. Integer positions and high-priced stocks.** `SecurityBase.allocate()` uses `math.floor` for long positions (`core.py:1534`). For high-priced stocks or small portfolios, rounding to zero shares is silent and produces distorted weights. The `Backtest` class exposes `integer_positions=True` as default, and the docs warn about this, but it is easy to miss.

**4. Sub-strategy paper trading.** When a strategy is a child of another strategy, bt creates a "paper trade" copy (`core.py:581-591`) to compute the child strategy's NAV price without affecting real capital. This paper portfolio runs the same algo stack with the same data, meaning if the parent uses the child's NAV price as a signal, no lookahead is introduced. This is a thoughtful design choice.

---

## Corporate actions and survivorship

### What bt provides

As of v1.2.0, `CorporateActions` (`algos.py:1630`) handles dividends and splits:

```python
# algos.py:1662-1687
def __init__(self, dividends, splits):
    super(CorporateActions, self).__init__()
    self.dividends = dividends.fillna(0.0)
    self.splits = splits.fillna(1.0)

def __call__(self, target):
    if target.now in self.splits.index:
        for c in target.children:
            if c in self.splits.columns:
                spl = self.splits.loc[target.now, c]
                if spl != 1.0:
                    target.children[c]._position *= spl
    if target.now in self.dividends.index:
        div_inflow = 0.0
        for c in target.children:
            if c in self.dividends.columns:
                div = self.dividends.loc[target.now, c]
                if div != 0.0:
                    div_inflow += div * target.children[c]._position
        target.adjust(div_inflow, flow=False)
    return True
```

Dividends adjust capital on the ex-date (not payment date, as that is hard to obtain). Splits adjust position quantities directly. The docstring explicitly acknowledges the ex-date simplification (`algos.py:1644-1646`).

### What bt does NOT provide

- **Survivorship bias correction.** bt has no concept of index membership over time. If you pass a universe of today's S&P 500 constituents, you are running with survivorship bias. The user must supply a properly constructed point-in-time universe.
- **Delistings.** `CloseDead` (`algos.py:1690`) closes positions where price is zero, treating this as bankruptcy. Actual delisting procedures (cash proceeds at a specific price, date of last trade) require user-supplied data.
- **Spin-offs.** Not modeled. Cash-equivalent treatment would require custom algo logic.
- **Adjusted vs. unadjusted.** bt can run on either; `CorporateActions` supports running on unadjusted prices, which is the correct approach for realistic transaction count backtesting. Most examples in the docs use Yahoo Finance adjusted close prices, which silently handles splits and dividends in the price series but produces incorrect share counts for transaction cost analysis.

---

## Cost and execution modeling

### Commission functions

bt supports two commission paradigms:

**1. Flat function hook (original):** `commissions=lambda q, p: max(1, abs(q) * 0.01)`. Set via `Backtest(commissions=fn)` or `strategy.set_commissions(fn)`. The function signature is `(quantity, price) -> float` (`core.py:1132-1134`). This is simple but cannot model volume-dependent impact.

**2. CostModel classes (v1.2.0 addition):** `SqrtCostModel` and `AlmgrenChrissCostModel` (`core.py:2153-2218`) accept bar volume and volatility:

```python
# core.py:2175-2180
def cost(self, q, p, V, sigma):
    abs_q = abs(q)
    if V <= 0.0 or abs_q == 0.0:
        return 0.0
    return (2.0 / 3.0) * self.Y * sigma * abs_q * (abs_q / V) ** 0.5 * p
```

The `Backtest` wires these via a monkey-patched `commission` method on each `SecurityBase` (`backtest.py:256-268`). Volume and volatility are passed as separate DataFrames at construction time.

### Bid-offer spread

A `bidoffer` DataFrame (same shape as price data) can be passed to `Backtest` as `additional_data`. Half the spread is charged per unit on each transaction (`core.py:1711-1712`). This is symmetrical and time-varying, which is realistic for daily data.

### Slippage and execution realism

bt has no slippage model. There are no partial fills, no limit orders, no market orders with queue position, no intraday timing assumptions. Execution happens at the exact closing price of the bar on which `run()` is called. `LimitDeltas` (`algos.py:1337`) is often misread as a slippage control, it is a weight constraint that limits how much a weight can change per bar, not a fill model. `RebalanceOverTime` (`algos.py:1827`) spreads rebalancing across N bars, which approximates TWAP slicing but does not model market impact during execution.

### When do orders fill?

Orders fill in the same bar's `update()` call that follows `run()`. The sequence in `Backtest.run()` (`backtest.py:346-351`) is:

```
strategy.update(dt)   # price is now T's close
strategy.run()         # algos fire, allocate() called, position adjusted
strategy.update(dt)   # NAV recalculated at T's close with new positions
```

This means a signal computed from T's close executes at T's close. For strategies that require T+1 entry, the user must shift prices. This is a footgun that bt documents but does not enforce.

---

## Performance and scaling

### Multi-asset support

bt handles multi-asset portfolios natively. The universe is a `pd.DataFrame` with dates as index and tickers as columns. There is no hard limit on the number of assets; performance degrades gracefully. The lazy-children mechanism (`SecurityBase.lazy_add`) defers the creation of child nodes until they are first transacted, improving performance for sparse universes (`core.py:1163-1165`).

### Memory model

All price series, value series, position series, and cash series are stored as `pd.Series` backed by a pre-allocated `pd.DataFrame` per node (`core.py:624-646`, `SecurityBase.setup:1341`). Memory is proportional to `(dates * nodes)`. For a 20-year daily backtest of 500 stocks, this is manageable on modern hardware (roughly 500 * 5000 * 8 bytes * ~7 arrays per node = ~140 MB).

### Speed

`core.py` is compiled with Cython for the hot path (`pyproject.toml:81`). `algos.py` and `backtest.py` are not compiled. The Cython annotations use `cy.double` and `cy.bint` typed locals (`core.py:684-691`) which provide meaningful speedup for the inner update loop. The docs explicitly acknowledge that speed is a roadmap item, not a current priority. Expect backtests over large universes (1000+ assets, 20+ years, monthly rebalance) to take tens of seconds rather than milliseconds. For parameter sweeps with hundreds of strategy variants, this becomes a bottleneck.

---

## Strengths

### Algo composition for rebalancing is genuinely elegant

The unix-philosophy design of the algo stack solves a real problem: rebalancing strategies differ mainly in (a) which assets to include, (b) how to weight them, and (c) how often to rebalance. These three concerns map cleanly onto Select, Weigh, and Run algo families. Swapping `WeighEqually` for `WeighInvVol` or `WeighMeanVar` without touching any other code is a real productivity win. The `temp` dict as a blackboard between algos avoids coupling algos to each other's internal state, making individual algos unit-testable in isolation.

The library ships a comprehensive algo catalogue: 15 selection algos, 8 weighting algos, 6 rebalancing control algos, risk and margin algos, and `CorporateActions`. This covers the vast majority of daily-bar portfolio research workflows without any custom code.

The `run_always` decorator (`algos.py:18-24`) is a clean solution to the problem of algos that must run regardless of whether earlier algos short-circuited (e.g., continuation of a multi-day rebalance). It avoids forcing users to write their own control-flow logic.

### Multi-level portfolio composition

The ability to nest strategies, a `FixedIncomeStrategy` of corporate bonds alongside an equity `Strategy`, both children of a parent allocation strategy, is architecturally clean. The parent sees each child's NAV as a synthetic ticker and can rebalance between them without knowing their internal composition. This maps well to real fund-of-funds or sleeve-based mandates.

### Fixed income support

`FixedIncomeStrategy`, `CouponPayingSecurity`, and `HedgeSecurity` (`core.py:2109-2218`) provide notional-weighted accounting, coupon accrual, and holding cost modeling. This is unusual in open-source backtesting libraries and makes bt usable for bond portfolio research, rate swap strategies, and hybrid portfolios.

---

## Weaknesses, footguns, critiques

### Not for intraday or HFT

bt is daily-bar only in practice. Nothing in the architecture prevents sub-daily data, but every design choice (algo stacks, end-of-bar settlement, the fact that signal and fill use the same timestamp) assumes one execution opportunity per bar. For minute-level strategies, the absence of a real order model makes results meaningless.

### The fill-at-signal-close footgun

This is the most dangerous footgun. There is no warning, no guard, no default lag. Users who do not explicitly shift their price series by one period are backtesting a strategy that looks at today's close and buys at today's close. For liquid large-cap equities with daily rebalancing, the overstatement of returns is modest. For illiquid assets, small-cap names, or any strategy where the rebalance signal itself moves the market, the bias is material.

### Tree composition limits extensibility

The tree is elegant for hierarchical portfolio allocation, but it becomes a constraint when you need cross-tree information. An algo in a child strategy cannot directly read the parent strategy's universe or other children's weights without going through `target.parent`. For complex multi-strategy overlays (e.g., a risk overlay that modifies weights of all sub-strategies simultaneously), this requires careful structuring and is not well documented.

The `deepcopy` of the strategy at `Backtest` construction (`backtest.py:181`) means you cannot share state between a `Backtest` and the `Strategy` object you defined. This is correct for correctness (the strategy template is reusable), but it surprises users who try to inspect strategy internals before calling `bt.run()`.

### No order book, no partial fills, no realistic execution

Beyond the fill-at-close assumption, bt does not model:
- Limit vs. market orders
- Queue position or priority
- Partial fills due to limited liquidity
- Execution over multiple bars (other than the approximation in `RebalanceOverTime`)
- Short-selling costs beyond the `cost_short` parameter on `CouponPayingSecurity`

### Survivorship bias is the user's entire problem

bt provides no tools for constructing point-in-time universes, validating that data is free of future-membership bias, or sourcing historical constituent data. The default example data source is Yahoo Finance adjusted closes, which contains survivorship bias by construction (delisted companies disappear from the API). This is not a bt-specific problem, but bt's documentation does not address it and the library provides no scaffolding to help.

### Single-maintainer risk

The project has one named maintainer with a personal email address in `pyproject.toml`. Active development may pause without warning. Any organization building production research infrastructure on bt should plan for this contingency.

### AlgoStack is not a computation graph

Algos are executed in serial, one per bar. If two algos are independent (e.g., computing covariance and computing momentum), they cannot be parallelized within the stack. For compute-intensive signal algos (mean-variance optimization with a large covariance matrix), the daily loop becomes the bottleneck and there is no native way to cache or batch across dates.

---

## Lessons for our design

### Things to copy

**1. The Algo composition pattern for portfolio policies.**
The `Select -> Weigh -> Constrain -> Rebalance` pipeline is the right abstraction for portfolio-level decision making. Our event-driven engine should have an equivalent "portfolio policy" layer that is composable and independently testable. The key design decisions to replicate: algos communicate through a typed context object (bt's `temp`/`perm`), and the pipeline short-circuits on `False` for clean control flow.

**2. The `run_always` decorator.**
Some portfolio actions (risk checks, margin calls, forced liquidations) must run regardless of whether the main signal fired. The decorator pattern is clean and avoids hard-coded special cases.

**3. The `lag` parameter on signal algos.**
Every signal algo in our system should have an explicit `lag` parameter with a non-zero default. This forces users to think about publication delay and prevents the silent fill-at-close footgun.

**4. Bid-offer as a separate DataFrame.**
Separating the bid-offer series from the price series is the right API. It allows time-varying, asset-specific spread modeling without embedding it in the price data.

**5. Nonlinear cost model interface.**
The `CostModel` base class with `cost(q, p, V, sigma) -> float` is a clean extension point. Adopting the same interface would allow users to plug in academic impact models (Almgren-Chriss, square-root) without modifying engine code.

### Things to avoid

**1. Fill-at-signal-close by default.**
Our engine must use an explicit order object with a separate fill event. The fill price must come from a bar *after* the signal bar. The default should be next-bar open, with the user explicitly opting into same-bar close if they want it.

**2. Tree as the primary architecture.**
The tree is the right structure for *composing portfolios*, but it is the wrong structure for *routing orders*. Our engine should separate the portfolio composition layer (where the tree metaphor makes sense) from the execution layer (where an order queue, matching engine, and fill reporting are needed). Conflating these, as bt does, means you can never add realistic execution without a major architectural rewrite.

**3. Implicit universe = no survivorship control.**
Our data layer must have explicit support for point-in-time index membership. Users should be required to specify a universe with inclusion/exclusion dates, not just a price DataFrame.

**4. Cython as the only performance path.**
bt compiles `core.py` with Cython for speed, which creates a binary dependency and complicates installation. Our engine should target vectorized NumPy/pandas operations in the hot path before considering compiled extensions.

### Open questions

- Can the algo-stack pattern be extended to support a computation-graph model (parallel signal computation, deferred evaluation) without losing the composability benefit?
- How do we make the portfolio policy layer (which maps naturally to bt's algo stack) interact cleanly with an event-driven execution layer? Specifically: at what point in the event sequence does the policy fire, and what is the contract between the policy output and the order router?
- For nested sub-strategies, does our engine need the paper-trading NAV mechanism bt uses, or can we compute sub-strategy NAV analytically from positions?

---

## Sources

1. **bt GitHub repository**, https://github.com/pmorissette/bt, Primary source; all code references are to the v1.2.0 shallow clone at commit `2630651`.
2. **bt official documentation**, https://pmorissette.github.io/bt/, Design philosophy, feature overview, algo documentation.
3. **bt/bt/core.py** (cloned), Node, StrategyBase, SecurityBase, Algo, AlgoStack, Strategy, CostModel class implementations.
4. **bt/bt/algos.py** (cloned), SelectAll, SelectN, WeighEqually, WeighTarget, WeighInvVol, WeighMeanVar, LimitDeltas, Rebalance, RebalanceOverTime, CorporateActions class implementations.
5. **bt/bt/backtest.py** (cloned), Backtest, run(), Result, RenormalizedFixedIncomeResult; the main date loop at lines 336-355.
6. **bt Releases page**, https://github.com/pmorissette/bt/releases, Version history and changelog summaries.
7. **ffn GitHub repository**, https://github.com/pmorissette/ffn, bt's companion library for performance statistics and financial math; required dependency.
8. **PyPI bt page**, https://pypi.org/project/bt/, Version, classifier ("Beta"), install metadata.
9. **The Python Backtesting Landscape (2026)**, https://python.financial/, Third-party comparison of bt against backtrader, VectorBT, NautilusTrader; contextualizes bt's positioning as a portfolio research tool.
10. **QuantStart: Backtesting Systematic Trading Strategies in Python**, https://www.quantstart.com/articles/backtesting-systematic-trading-strategies-in-python-considerations-and-open-source-frameworks/, Discusses bt alongside zipline and backtrader; notes execution realism gaps as the core limitation of daily-bar portfolio frameworks.
11. **bt/pyproject.toml** (cloned), Dependency specification (`ffn>=1.1.2`), Cython build targets, Python version support matrix.
