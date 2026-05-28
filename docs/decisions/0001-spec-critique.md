# ADR 0001: Critique of the original project spec

Status: Accepted.
Date: 2026-05-28.
Authors: Sam Doane (with critique and skeptical review captured below).

## Context

The original project spec (paraphrased from the project brief):

> A production-grade event-driven backtester with: event-driven architecture (market → signal → order → fill events through a queue); lookahead bias structurally impossible via the API/type system; realistic execution (fixed bps, volume participation, square-root impact; commissions; bid-ask costs; partial fills; MOO/MOC vs intraday semantics); corporate actions (splits, dividends, point-in-time index membership); borrow costs and short-sale constraints; walk-forward and purged k-fold cross-validation with embargo as first-class features; performance and risk analytics (Sharpe, Sortino, Calmar, max drawdown, turnover, exposure, per-trade attribution, bootstrap confidence intervals on Sharpe); clean separation of data, strategy, portfolio/risk, execution layers. Stack: Python 3.11+, uv, Polars preferred over Pandas, Pydantic, pytest, mypy strict, runs in WSL2.

Phases 1 and 2 produced material findings that the spec should be measured against:

- Phase 1 ([`docs/research/0001-existing-backtesters.md`](../research/0001-existing-backtesters.md)) surveyed six open-source backtesters and identified five recurring patterns: structural lookahead protection beats convention by orders of magnitude; corporate actions and PIT index membership are the field's consistent blind spot; execution realism varies by four orders of magnitude across libraries; sweep-mode and event-driven mode serve different decision contexts; backtest-live divergence is the single most important architectural risk.
- Phase 2 ([`docs/research/0002-methodology.md`](../research/0002-methodology.md)) extracted the methodological canon: backtesting is hypothesis testing not search; the trial registry and result confidence-tier are required architectural components; PSR, DSR, MinTRL replace raw Sharpe as the default statistics; permanent impact must feed the price series; PIT data is five distinct problems.

This ADR critiques the spec against those findings. The next section is the critique. The section after that is a skeptical-reviewer agent's review of the critique. The final section is my response and the locked-in decisions.

## Critique

This is the critique pass. I take the spec line by line, identify what is over-engineered, naive, missing, or wrong against the phase 1 and phase 2 findings, and call out where the spec needs to be tightened or revised before architecture work begins.

### 1. The breadth of v1 is unrealistic for a solo portfolio project

The spec asks for: event-driven engine, structural lookahead protection, four slippage models, commissions, bid-ask, partial fills, MOO/MOC, splits, dividends, PIT index membership, borrow costs, short-sale constraints, walk-forward, purged k-fold with embargo, bootstrap CI on Sharpe, six performance metrics, four-layer separation. Each line item is a non-trivial body of work.

Phase 1 makes the comparison sharp. nautilus_trader is the field's most rigorous open-source equivalent and has had Nautech Systems backing for a decade. Their feature surface matches roughly half this spec and they explicitly punt on the other half (corporate actions issue #3307 still open; AT_THE_OPEN/AT_THE_CLOSE rejected in simulation per `matching_engine/engine.rs:2996-3008`). Zipline-reloaded is the next-most-rigorous and lacks point-in-time index membership entirely. The spec is asking for a strict superset of features that two well-funded efforts have not yet delivered.

This matters because the audience is quant recruiters, not users. A reviewer will weight depth-of-thinking on a subset higher than breadth-of-implementation across everything. The spec as written is a strict superset of features that nautilus_trader and zipline-reloaded combined do not deliver, and the most defensible response is to commit to one well-chosen subset, not to attempt all of it.

Concrete proposal: drop futures, drop crypto microstructure, drop options, drop intraday LOB-level simulation. Commit to U.S. equity daily-bar simulation with three things done exceptionally well: structural lookahead protection, PIT data with corporate actions, and execution cost realism with the Almgren 2005 calibration. M1 demonstrates correctness against known answers; M2 demonstrates the PIT differentiation; M3 demonstrates the cost-model rigor. Everything else is post-v1 if at all.

### 2. "Structural lookahead protection" is an overpromise

The spec says "lookahead bias structurally impossible via the API/type system, not just by convention." Phase 1 shows that even zipline's Pipeline (the field's gold standard for this) has paths where the API trusts the user. `BarData.current()` and `BarData.history()` give correct results because the simulation clock controls them, but a determined user can call `data.history(asset, "close", bar_count=N, frequency="1d")` with a large bar_count and index manually into the array. The Pipeline structural enforcement applies to factor computation, not to the bar handler.

Total structural prevention is impossible in Python without a sandbox. The honest statement is: prevent the common patterns through API types and event-loop ordering, enumerate the remaining trust boundaries, document them in `docs/decisions/0003-architecture.md`, and refuse to claim that lookahead is fully prevented when it is not.

The trust boundaries that need to be enumerated, based on phase 1 evidence: arbitrary Python code in the strategy callback; user-supplied features computed outside the engine API; mixing engine-supplied PIT data with externally fetched current-day data in the same callback. We should document these as explicit "you can still bypass the engine if you do this" cases rather than promising they cannot happen.

### 3. The corporate actions list is the easy half

The spec lists "splits, dividends, point-in-time index membership." Phase 2 makes clear that splits and cash dividends are the easy two. The hard parts are: delisting cash proceeds with the right last-trade price (Shumway 1997 documents that CRSP missing delisting returns substituted with -1 introduced bias; Shumway and Warther 1999 showed the bias was 4.7x larger on Nasdaq); spin-offs as cash-equivalent treatment; stock-for-stock mergers; rights offerings; special distributions; identifier non-persistence across redomiciling transactions.

The spec implicitly equates "corporate actions" with the easy two and ignores that delistings (the survivorship-bias mechanism) are technically the hardest case to model correctly. M2 must commit to a specific corporate action coverage: splits, cash dividends, delisting cash proceeds with explicit last-trade price, and spin-offs as cash-distribution-equivalent. The rest is deferred. The spec's list as written would not catch that the spec is silent on delistings.

### 4. Partial fills without L2 data are speculative

The spec calls for partial fills as a feature. Phase 1 shows that nautilus_trader's partial fills walk actual L2 book levels. Without L2 data, partial fills are a parameter the user makes up (vectorbt's `allow_partial=True` fills the affordable fraction, which is not a model of anything real; zipline's `VolumeShareSlippage` caps fills at a volume fraction, which is closer but is still parameter-fitting).

If v1 is daily-bar U.S. equity, the realistic options are: (a) no partial fills, every order fills in full at one bar price; (b) a participation-rate cap with volume rolling forward to the next bar (the zipline pattern); (c) a square-root-impact-based partial-fill model where the engine estimates how much of the order can clear in the bar at acceptable slippage. Option (c) is theoretically defensible and matches the cost-model literature. Options (a) and (b) are honest about their approximation.

The spec's "partial fills" line item needs to commit to one of these. The current wording reads as if partial fills are a checkbox; they are a model choice with implications for the rest of the cost layer.

### 5. MOO and MOC are the field's hardest unsolved auction problem

The spec lists "MOO/MOC vs intraday semantics" as a feature. Phase 1 shows nautilus_trader (the most rigorous open-source backtester) explicitly rejects AT_THE_OPEN and AT_THE_CLOSE time-in-force orders in simulation: the enum variants exist for live order submission but the matching engine generates `OrderRejected` events. The spec is asking for what the best library has explicitly punted on.

Closing auctions are 8 to 10 percent of daily volume in U.S. large-caps. They are economically central. They are also genuinely hard to model from bar data because the auction print is a separate event from the continuous session's closing trade. The spec needs to commit to an explicit auction model: either (a) approximate MOO/MOC as a separate "auction bar" with its own price input (requires the data layer to expose auction prices as a distinct field), or (b) approximate MOO/MOC as a slippage-augmented version of the bar's open or close (which is what users typically end up doing in zipline, see phase 1 issue #2364). Without this commitment the "MOO/MOC vs intraday semantics" line item is aspirational.

### 6. Walk-forward in the spec is the old canon; CPCV is the new canon

The spec says "walk-forward analysis and purged k-fold cross-validation with embargo as first-class features." Phase 2 shows that LdP's framework treats walk-forward as a degenerate case of Combinatorial Purged Cross-Validation (CPCV) with N=T and k=1, producing exactly one path from a single dataset. CPCV generalizes this to phi(N, k) = (k/N) * C(N, k) paths from the same data. The spec separates them as if they are different things; in the current canon they are the same thing at different settings.

Concrete revision: make CPCV the primary validation surface, with walk-forward exposed as a CPCV configuration. The backtesting layer's output type is then `BacktestPathDistribution[Result]`, not `Result`. Any single-Sharpe API on CPCV results is a correctness bug. The spec needs to acknowledge this contractually.

### 7. The performance metrics list omits the actual canon

The spec lists Sharpe, Sortino, Calmar, max drawdown, turnover, exposure, per-trade attribution, bootstrap CI on Sharpe. Phase 2 establishes that the actual canon is PSR (Bailey-LdP 2012), DSR (Bailey-LdP 2014), MinTRL (Bailey-LdP 2014), HHI concentration (LdP 2018 ch. 14), and the chapter 14 scorecard. The spec's list is "standard finance class" not "current literature."

The bootstrap CI on Sharpe line item is the clearest example. Bootstrap is a fine approach in general but PSR is a closed-form asymptotic formula that delivers the same confidence interval at orders of magnitude less computation. The Bailey-LdP 2012 formula is `PSR(SR*) = Phi((SR_hat - SR*) * sqrt(T-1) / sqrt(1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2))`. There is no reason to bootstrap when this is available.

Concrete revision: replace the metric list with the LdP chapter 14 scorecard (general characteristics, performance, runs and drawdowns, implementation shortfall, risk-adjusted efficiency with PSR/DSR/MinTRL, attribution). Keep Sharpe in the list but never report it without PSR, DSR, MinTRL alongside. Drop bootstrap CI on Sharpe.

### 8. The layer separation is partly wrong

The spec says "clean separation: data layer, strategy layer, portfolio/risk layer, execution layer." Phase 2 establishes that the policy and execution concerns must be separated more carefully than this. Specifically: portfolio policy (what weights do I want?) is distinct from execution accounting (what fills did I get and what is my P&L?). Conflating them, as bt does with its `target.rebalance` call that synchronously updates positions and applies commissions, is the architectural choice that prevents adding realistic execution later.

The correct layering, derived from phase 2's findings: data layer → signal/forecast layer → portfolio policy layer (the AlgoStack-equivalent from bt, the right abstraction for "what should we hold") → execution layer (matching engine, fill model, latency model, cost accounting) → analytics/reporting layer. Risk is not a layer; it is a constraint that the policy layer applies and that the analytics layer reports on. Putting "risk" alongside portfolio as a layer name is conceptually muddy.

### 9. Polars preference is premature optimization

The spec says "Polars preferred over Pandas (recommend otherwise if you disagree)." I disagree. Pandas is the lingua franca of quantitative finance Python: zipline, backtrader, vectorbt, bt, QSTrader all use Pandas; the AFML accompanying code uses Pandas; the entire factor research community works in Pandas. Polars is faster but the speed difference matters only for hot paths.

The right answer for a portfolio project: use Pandas as the default tabular type at API boundaries (user-facing strategy code, analytics output, data adapters). Use Polars internally for hot paths where speed matters (large universe price loading, cross-sectional factor computation). Use NumPy for the simulation kernel's tightest loops. Forcing Polars at API boundaries adds a learning curve for users (and reviewers) without proportional benefit.

This is a defensible disagreement and I am taking it.

### 10. The spec is missing several things that phase 2 made required

The spec does not mention:
- **Trial registry** for DSR. Without this, the deflation collapses to PSR with N=1 and silently underreports overfitting risk. Phase 2 section 4 makes this a required architectural component.
- **Result confidence tier** (single-run / walk-forward-validated / sweep-with-DSR-correction / sweep-selected). Without this, sweep-mode results can silently feed deployment decisions, which is the vectorbt failure mode phase 1 specifically identified.
- **Permanent impact register** that feeds the price series. Phase 2 section 7 makes this required for any cost model that includes permanent impact to be more than nominally implemented.
- **Pre-trade cost estimate** exposed to the portfolio policy layer. The QSTrader gap noted in phase 1 (`# TODO: Implement cost model`) is exactly this hole. Closing it is non-negotiable for the cost model to be useful.
- **Dual-timestamp data model** (`period_end_dt`, `available_dt`). Phase 2 section 8 makes this the foundation of the data layer.
- **Persistent asset identifiers** (CRSP PERMNO style). Ticker reuse and CUSIP non-persistence make any ticker-keyed engine produce non-reproducible results across data vintages.
- **A specific PIT data source commitment for v1**. Sharadar SF1 ARQ + `SHARADAR/SP500` is the cheapest credible source (approximately $50/month). Without committing to a data source, the engine cannot be end-to-end demoed.

These belong in the architecture ADR but they need to be on the spec's radar now so the architecture can deliver them.

### 11. The spec has no validation strategy for the engine itself

The spec talks about validation for strategies (walk-forward, CV) but not for the engine. M1 should validate against known-answer tests: a buy-and-hold SPY backtest that matches actual SPY total return to within a documented tolerance; a deterministic hand-computable strategy whose P&L can be cross-checked against a spreadsheet. Without these the engine is unfalsifiable.

Phase 1 implicitly highlighted this: the Aiello et al. (2026) "Implementation Risk in Portfolio Backtesting" paper documented systematic divergence across six open-source backtesters running identical strategies under nonzero costs (up to 3.71 percentage points per year, correlated at 0.93 with cost intensity, including the backtrader `/100.0` commission silent rescale). The mitigation is cross-engine reconciliation against a reference. M1 should validate against the actual SPY total return as reported by S&P (a published number); subsequent milestones should add reconciliation against zipline-reloaded or vectorbt on identical inputs.

`docs/TESTING.md` will need to specify these known-answer tests, the tolerance levels, and the failure-handling policy (a divergence beyond tolerance fails the build, not just a warning).

### 12. The spec has no performance budget

The spec does not commit to a target runtime. Phase 1 showed that backtesters range from seconds (vectorbt's parameter sweep on 1M orders in 42ms) to hours (zipline minute-bar over thousands of assets). Without a performance budget, architecture cannot make informed tradeoffs.

A defensible budget for v1 (U.S. equity daily-bar): a 20-year backtest on 500 names should complete in under 60 seconds on a laptop. This is achievable with NumPy-vectorized cross-sectional factor computation and a per-bar Python dispatch for the strategy callback. If the dispatch loop becomes the bottleneck, Numba acceleration is the standard next step (per vectorbt's approach in phase 1). This budget rules out per-asset object instantiation patterns (the backtrader Lines memory model would not meet it on 500 names).

### 13. The "production-grade" framing is the wrong target

This is the most strategic critique. The spec says "production-grade event-driven backtester." Phase 1 surfaced that nautilus_trader is the only project credibly aiming at that target and has had ten years and a company behind it. A solo portfolio piece that misses "production-grade" reads worse than a solo portfolio piece that aims at something different and hits it.

The defensible reframing: this is a *research-grade* backtester for U.S. equity factor strategies, with explicit honesty about scope. It does three things exceptionally well (structural lookahead protection, PIT data with corporate actions, execution cost realism with Almgren 2005). It does not pretend to handle futures, crypto, options, intraday LOB, or live trading. It is the engine a careful researcher would use to validate an equity factor before paying for vectorbtpro or nautilus_trader.

A recruiter reading this repo benefits more from depth on a defined target than from a checklist of features matched against a "production-grade" ambition. The honest scope statement should be in the README and in this ADR.

## Summary of proposed changes from the critique

In order of consequence:

1. Reframe v1 scope as U.S. equity daily-bar research backtester, not production-grade multi-asset event-driven engine.
2. Replace walk-forward as the primary validation surface with CPCV. Walk-forward becomes a CPCV configuration.
3. Replace the metric list with the LdP chapter 14 scorecard. Drop bootstrap CI on Sharpe; PSR is the closed-form replacement.
4. Drop the "structurally impossible lookahead" overpromise; enumerate the remaining trust boundaries instead.
5. Make MOO/MOC and partial fills explicit model commitments, not feature checkboxes.
6. Add trial registry, confidence tier, permanent impact register, pre-trade cost estimate, dual-timestamp data model, persistent asset identifiers to the required architectural components list.
7. Commit to Sharadar SF1 ARQ + `SHARADAR/SP500` as the v1 PIT data source. (Or document an alternative.)
8. Use Pandas at API boundaries; reserve Polars and Numba for hot paths.
9. Add `docs/TESTING.md` with explicit known-answer tests (SPY total return; deterministic hand-computable strategy).
10. Set a performance budget: 20-year backtest on 500 names under 60 seconds on a laptop.

These changes are the critique's verdict. The skeptical reviewer's response and my final position follow in the next sections.

## Skeptical reviewer's response

The review below was produced by a sub-agent persona instructed to act as a senior quant researcher at a multi-strategy hedge fund, with explicit instructions to find what is wrong, naive, missing, or self-indulgent in the critique and to hold nothing back. It is reproduced verbatim.

### Reviewer summary

- The critique is mostly correct on the big strategic moves (kill scope, replace WF with CPCV as default, swap raw Sharpe for PSR/DSR/MinTRL, abandon "production-grade") but it is naive in roughly the way an undergrad with one good reading list is always naive: it treats Lopez de Prado as scripture, ignores that LdP himself has been contested in the literature on CPCV variance and on DSR power, and underestimates how much of the hard work in a real backtester is plumbing, not math.
- The author is hiding behind paper citations to avoid committing to implementation contracts. Sections 4, 5, 8, and 10 all read like "I have read the source, here are the options, let me defer the decision." A senior reviewer at a real shop reads that as inability to choose, not as humility. Choose, defend, and move on.
- The reframing to "research-grade U.S. equity backtester" is correct, but the critique stops one step short of the actual reframe a hiring committee respects: this is a teaching artifact, not a research artifact, and the README needs to say that out loud.
- The omissions are bigger than the contents. There is nothing on survivorship, nothing on factor neutralization, nothing on borrow inventory vs. borrow cost, nothing on the actual mechanics of the index reconstitution problem, nothing on numerical reproducibility, and nothing on how the engine handles its own bugs (i.e., differential testing against a second implementation).
- The author will execute sections 1, 6, 7, 11, and 13 well. He will not execute 3, 4, 5, and 10 well, because each requires a data vendor decision he has not made yet and that Sharadar SF1 ARQ cannot resolve.

### Points the reviewer thinks the critique gets right

1. **Section 1, scope kill.** The original spec is a feature checklist that no solo developer has shipped in finite time without abandoning at least three of the items. Nautilus has 10 years and a paid team and still punts on corporate actions and auction simulation. The critique's instinct to cut to U.S. equity daily-bar and do three things exceptionally is exactly what a senior reviewer would say in a real scoping meeting.
2. **Section 2, "structurally impossible" is overclaiming.** I have personally seen Pipeline-style APIs leak via three vectors: user-defined CustomFactor implementations that pull from a DataFrame the user closed over, alternative-data joins on as-of dates the user filled-forward in a notebook before passing to the engine, and "feature store" wrappers that revalidate cache on a wall-clock trigger. The type system buys you a lot but does not buy you total prevention. Enumerate the trust boundaries, as the critique says.
3. **Section 6, CPCV over walk-forward, with the right output type.** The line "Any single-Sharpe API on CPCV is a correctness bug" is correct and is the kind of crisp design constraint that hiring committees notice.
4. **Section 7, PSR/DSR/MinTRL.** Raw Sharpe with a bootstrap CI is 2010 thinking. The Bailey-Lopez de Prado PSR closed form and DSR are the right defaults. Also report the Probabilistic Sortino because skew matters.
5. **Section 11, engine self-validation.** This is the single most underrated item on the critique's list and is the one item that genuinely separates a portfolio piece from a toy. Reconciliation against SPY total return is table stakes; reconciliation against a hand-computable strategy (e.g., constant-weight monthly rebalance of three names) is what proves the engine is not silently lying. I have rejected candidates for not thinking of this; I have hired candidates partly because they did.
6. **Section 13, drop "production-grade."** "Production-grade" written by a USC undergrad triggers an instant downgrade signal at every fund I have worked at.
7. **Section 12, performance budget.** Good engineering hygiene; require the budget tracked in CI with a regression threshold.

### Points the reviewer thinks the critique gets wrong

1. **Section 9, Polars at boundaries is conservative, not judicious.** Polars 1.0 has shipped, the API has stabilized, Arrow-native zero-copy is a real win at boundaries, and committing to Polars end-to-end in 2026 signals comfort with modern tooling. Pandas at boundaries reads as conservatism. The right move is Polars-native with a documented `.to_pandas()` adapter on the public results object. The hot-path/Numba split is correct in isolation but is doing different work than the boundary decision.
2. **Section 4, partial fills as a three-option menu.** Listing options without choosing gets a candidate dinged. The correct answer at daily-bar resolution is the participation cap with rollover (ADV-percentage cap, leftover queued to next bar, decay over N bars). Almgren-Chriss style partial-fill at daily resolution is overreach because the impact model is calibrated on intraday slicing decisions. Just say it.
3. **Section 5, MOO/MOC.** At daily-bar resolution, MOO/MOC is not unsolvable. NYSE and Nasdaq publish auction prices; if the data source supports it (Sharadar does not, IEX DEEP cloud archive does, Polygon does), MOO/MOC is a fields-on-the-bar problem, not a hard auction-simulation problem. The right answer is "MOO uses open, MOC uses close, with separate slippage parameters calibrated to imbalance," and that is a one-day implementation. The critique is too deferential to nautilus's punt here.
4. **Section 10, "Sharadar SF1 ARQ" is too thin a commitment.** SF1 ARQ has restated and as-reported flavors, which is great, but it does not give you delistings with realistic cash proceeds (the SEP table does, partially), does not give you a clean index-membership history with reconstitution effective dates, does not give you borrow rates, and does not give you a corporate-actions feed. The critique endorses Sharadar SF1 ARQ as if naming the dataset solves the problem; it solves about 40 percent of the problem.
5. **Section 8, "Risk is not a layer."** Half right and half wrong. Pre-trade risk constraints are part of policy. Post-trade risk decomposition (factor attribution, var/es contribution, scenario stress) is its own layer in every fund I have worked at because the data flows are different and the consumers are different.
6. **Section 3, spin-offs-as-cash is a real shortcut.** Spin-offs distribute shares of the new entity; treating them as cash kills cross-sectional studies that follow the spinoff. The honest version is "spin-offs as cash for v1, with a documented bias toward parent-company-only studies and a note that this excludes spinoff-as-mispriced-orphan studies."
7. **Sections 4 and 5 together.** Choose Your Own Adventure reads as inability to make a call. Pick one option per section, defend in two sentences, ship.

### What the reviewer thinks the critique missed

1. **Survivorship bias as a separate concern from PIT membership.** PIT membership tells you the universe at time T. Survivorship tells you whether your data source itself dropped delisted names. The five problems are PIT membership, PIT fundamentals, survivorship in the price file itself, identifier persistence, and as-of joins for alternative data.
2. **Factor neutralization and beta hedging.** No mention of how to neutralize a factor signal against beta, sector, size, or industry. Any cross-sectional study at a real fund neutralizes before scoring. The engine needs a `Neutralizer` protocol and a residualizer against a fitted Fama-French style factor block.
3. **Cross-sectional standardization conventions.** Rank vs. z-score vs. demean vs. winsorize at what threshold. Plumbing every factor researcher uses every day.
4. **Differential testing against a second implementation.** Run the same strategy in pit-backtest and in zipline-reloaded, diff the equity curves, surface the differences. This catches engine bugs that an external reference cannot catch.
5. **Numerical reproducibility.** Seed control, BLAS thread non-determinism, floating-point reproducibility across architectures, hash-randomization in test ordering.
6. **Cost model uncertainty quantification.** Almgren 2005 calibration is a noisy point estimate from a 1998-2000 NYSE/Nasdaq pre-decimalization sample. Cost-sensitivity bands are mandatory.
7. **Borrow as an inventory problem, not a cost problem.** The right model has a borrow availability indicator plus a rate, with rejection of short orders when availability is zero.
8. **The signal-to-portfolio pipeline.** Portfolio construction (mean-variance with constraints, risk parity, sign-only equal-weight, integer-share-count) is its own layer. A hiring-committee-visible gap.
9. **Multi-asset seams.** The critique commits to U.S. equity only but does not say "the API is designed such that adding a futures asset class is a six-week project, not a re-architecture."
10. **Funding curve, dividend reinvestment, and short-rebate.** Cash returns are not zero. Dividend reinvestment policy needs to be explicit. Short rebate (interest on short sale proceeds, net of borrow cost) is its own line item.
11. **Tax-lot accounting and capital gains.** FIFO vs HIFO changes after-tax returns. For a factor study it may not matter, but worth a mention.
12. **Concrete reproducibility of every figure in the README.** One command, same number, every time.

### Reviewer's pushback on the proposed v1 reframing

The critique's reframe to "research-grade U.S. equity backtester for factor research" is correct as a first-order move and incorrect as the final position. The artifact's purpose is not to generate research; it is to demonstrate the author's competence to a hiring committee. The correct reframe is: "Teaching artifact: a U.S. equity daily-bar backtester built to demonstrate (a) structurally enforced PIT discipline, (b) a CPCV-first validation API, (c) Almgren-calibrated cost realism with honest uncertainty bounds, and (d) external plus differential validation against zipline-reloaded and a hand-computable benchmark." That framing tells the reviewer what to look for, sets the bar at a defensible height, and gives explicit permission to punt on items 3, 4, 5, and 10 with a clean "out of scope for v1, here is what v2 would look like" note.

Explicit non-goals are how senior engineers signal taste. The README should say what this engine is *not* the right engine for: not intraday market microstructure, not options books, not multi-asset macro portfolios.

### Reviewer's pushback on specific technical choices

**CPCV as primary, with guardrails.** Variance of the path distribution depends on path count; for path counts below 30 the distribution itself is noisy. The implementation must surface the path count alongside the distribution, default to a count that gives stable estimates, and refuse to compute DSR below a threshold. Walk-forward should remain available as a sanity check, not because it is correct but because it is what most readers know.

**Pandas at API boundaries.** Conservative answer. Polars-native with `.to_pandas()` adapter is the more honest 2026 answer.

**Sharadar SF1 ARQ as v1 data source.** Weakest commitment in the critique. The right v1 data stack is SF1 ARQ + SEP (prices and delistings) + TICKERS (identifier history), with a documented gap on borrow and PIT index membership, and an explicit note that short tests are estimates pending borrow data.

**Almgren 2005 calibration as default.** Honest about the citation, dishonest about the application. The eta and beta come from 1998-2000 NYSE/Nasdaq intraday VWAP study. Use as default with parameters explicitly labeled as a 1998-2000 calibration, run sensitivity analysis at eta in [0.05, 0.30], show the result bands.

**Almgren 2005 vs Bouchaud-Lillo-Farmer exponent.** Almgren says beta=0.6 from intraday VWAP slices; Bouchaud says beta=0.5 from meta-orders. For a daily-bar engine simulating meta-orders, Bouchaud's exponent is arguably more appropriate. Ship a `--impact-model=bouchaud` flag with beta=0.5; make the difference visible in the cost-sensitivity report.

**Explicit fill-price model declarations.** Cheapest correctness win in the spec. Every Order must carry a `FillPriceModel` enum field and the engine must refuse to fill without one.

### The single biggest thing the critique gets right (per reviewer)

The CPCV-as-distribution-not-scalar argument in section 6. Forcing the result type to be a distribution propagates honesty through every downstream consumer: PSR computation, DSR computation, equity-curve plot (fan chart not single line), risk decomposition, trade attribution. One decision pulls a dozen other decisions into line. Do not back down when a reviewer asks "why does my plot have 25 lines."

### The single biggest thing the critique gets wrong (per reviewer)

The Sharadar SF1 ARQ commitment in section 10. The critique treats data-source selection as a list item when in reality the data-source decision is the v1 budget. Every corporate-action edge case the critique names (delisting cash proceeds, spin-offs, rights offerings, identifier non-persistence) is a data-source capability question, not an engine capability question. SF1 ARQ does not cover that surface area. Make the data inventory decision before writing engine code.

### What a real hiring committee would think

If the repo executes cleanly on the critique's recommendations and the reviewer's additions:

- **AQR, Two Sigma, DE Shaw**: very favorable. These shops care about methodology and read the README the way an academic reads a paper. The reframe to "teaching artifact with explicit non-goals" plays well; differential testing against zipline plays very well; cost-sensitivity bands play very well.
- **Citadel and Millennium**: indifferent to mildly negative. Pod shops care about edge, not backtester elegance. The critique is about engine quality, not strategy quality. To shift this requires at least one non-trivial worked example: a real factor study with PSR-deflated Sharpes, a real cost-sensitivity report, a real OOS CPCV distribution, and an honest "DSR is 0.4, here is why this is not deployable" conclusion.
- **Renaissance**: indifferent. Different evaluation axes (math, statistics, ML papers); a daily-bar Python repo will not move the needle.
- **Bridgewater**: mildly positive but for orthogonal reasons. Wants systems thinking and economic intuition. Add a regime analysis plot from a single factor and Bridgewater notices.

The bigger point: this repo is not an interview pass on its own. It is a conversation starter. Plan the conversation, not just the artifact.

### Reviewer's recommendation for the author's final position

Execute sections 1, 6, 7, 11, 12, and 13 cleanly and aggressively. Commit to one option per section in 2, 3, 4, 5, 8, and 9 in the next 48 hours with a one-paragraph defense each. Replace "Sharadar SF1 ARQ" with a documented data inventory (SF1 + SEP + TICKERS + documented gap on borrow and PIT index membership) before writing engine code. Add three things the critique missed: a worked factor study with PSR-deflated Sharpe, differential testing against zipline-reloaded, cost sensitivity bands on the Almgren parameters. Drop "production-grade" today. Ship a v1 in four weeks or kill the project per the kill-early rule.

## My response to the reviewer

The review is the work of one critique pass with a hostile stance, not a verdict. I accept most of it. The places where I am keeping my position are listed below with the reasoning. The places where I am updating are listed first.

### Accepted

- **"Teaching artifact" reframe.** Better than "research-grade." Sets the right expectations. The README will say this. The non-goals list (not intraday LOB, not options, not multi-asset macro, not live trading) is moved to the README front matter.
- **Polars end-to-end with a `.to_pandas()` adapter.** Conceded. The conservative answer was conservatism, not judgment. The decision in section 9 flips to Polars-native; the architecture ADR will commit to the boundary types as Polars DataFrames with `.to_pandas()` available on every public result object.
- **Commit per section, not menu per section.** Conceded. Locked-in choices below.
- **Sharadar SF1 ARQ alone is not enough.** Conceded. The v1 data inventory is Sharadar SF1 ARQ (PIT fundamentals) + Sharadar SEP (prices and delistings) + Sharadar TICKERS (identifier history), with documented gaps on borrow rates and PIT S&P 500 reconstitution effective dates. Borrow is v2; short tests in v1 are flagged as estimates pending borrow data.
- **Spin-offs as cash is a documented bias.** Conceded. The cash-equivalent treatment ships, with an explicit "this excludes spinoff-as-mispriced-orphan studies" note in `docs/METHODOLOGY.md`.
- **Survivorship, factor neutralization, cross-sectional standardization, differential testing, numerical reproducibility, cost-model uncertainty bands, borrow-as-inventory, portfolio construction layer, multi-asset seams, dividend reinvestment and short rebate, README figure reproducibility.** All conceded. All added to the required architectural components list and to the M1 to M5 milestone breakdown that will be defined in [`docs/decisions/0002-roadmap-review.md`](0002-roadmap-review.md).
- **CPCV guardrails.** Conceded. The implementation surfaces path count alongside the distribution. The engine refuses to compute DSR below a minimum path count (default 30, configurable). Walk-forward remains available as a sanity check.
- **Risk decomposition as its own layer.** Conceded. The layering becomes data, signal, policy, execution, risk decomposition, analytics. Risk decomposition reads from execution and produces its own artifacts.
- **MOO/MOC at daily-bar.** Conceded. MOO uses open price with separate slippage; MOC uses close price with separate slippage. Auction-price-as-a-field is deferred to v2 if a data source for it is integrated (Polygon or IEX DEEP). The "MOO/MOC unsolvable" framing was wrong.
- **Partial fills.** Conceded to participation-rate cap with rollover. ADV-percentage cap (default 10%), leftover queued to the next bar, decay over N bars (default 3).
- **Bouchaud beta=0.5 flag.** Conceded. The default cost model is Almgren 2005 with eta=0.142, beta=0.6, gamma=0.314 and labeled as a 1998-2000 calibration. A `--impact-model=bouchaud` flag substitutes beta=0.5. The cost-sensitivity report shows both.
- **Worked factor study with PSR-deflated Sharpe, differential testing against zipline-reloaded, cost sensitivity bands.** All three become M4 or M5 requirements.
- **Performance budget tracked in CI with regression threshold.** Conceded. The 60-second budget for a 20-year backtest on 500 names becomes a CI check with a 10% regression threshold.

### Contested

The reviewer's "Probabilistic Sortino" suggestion is fine but not required. PSR adjusts for skewness and kurtosis through the third and fourth standardized moments; PSR with non-normal moments already prices the skew the reviewer is invoking. Adding Probabilistic Sortino is a free upgrade that I will not block but will not require for M1.

The reviewer says "the repo is not an interview pass on its own; plan the conversation." This is correct but is out of scope for this ADR. It informs the README's framing, not the engineering decisions.

The reviewer is right that I was hiding behind options in sections 4, 5, 8, and 10. I am committing now (see Final decisions below). The substantive point is conceded; the meta-critique about my style is noted and applied.

### Final decisions

These are locked in and will be referenced by ADR 0002 (roadmap) and ADR 0003 (architecture) without revisitation.

1. **Scope**: U.S. equity daily-bar backtester. Not multi-asset, not intraday, not LOB-level, not options, not live trading. Stated as explicit non-goals in the README.
2. **Framing**: teaching artifact. Not "production-grade." Not "research-grade." Teaching artifact with explicit non-goals.
3. **Validation surface**: CPCV is primary. Walk-forward is exposed as a CPCV configuration with one path. Result type is `BacktestPathDistribution[Result]`. Path count surfaced alongside; DSR refuses to compute below a minimum (default 30).
4. **Metrics**: the LdP chapter 14 scorecard. PSR, DSR, MinTRL, HHI, drawdown, per-year decomposition. Raw Sharpe shown alone is a configuration error.
5. **Layering**: data, signal, policy, execution, risk decomposition, analytics. Six layers. Risk decomposition is its own layer.
6. **Cost model**: SquareRootImpact with Almgren 2005 calibration as default (parameters labeled as a 1998-2000 calibration). `--impact-model=bouchaud` flag for beta=0.5. Cost-sensitivity bands required in every backtest report (eta in [0.05, 0.30]). Permanent impact register feeds the price series. Pre-trade cost estimate exposed to the policy layer.
7. **Fill semantics**: `FillPriceModel` enum required on every Order (`open`, `close`, `vwap`, `arrival`, `next-bar-open`); MOO uses `open` with separate slippage; MOC uses `close` with separate slippage; partial fills use participation-rate cap with rollover (default ADV 10%, decay 3 bars).
8. **Corporate actions for v1**: splits, cash dividends, delistings with cash proceeds at the documented last-trade price, spin-offs treated as cash equivalent (with a documented bias note). Rights offerings, special distributions, identifier history beyond Sharadar TICKERS are out of v1 scope.
9. **PIT data**: dual-timestamp model (`period_end_dt`, `available_dt`) on every record. Typed Universe API with `is_member(asset_id, date)`. Persistent asset identifiers via Sharadar TICKERS. Survivorship: data source must include delisted names; engine validates at construction time.
10. **Data inventory for v1**: Sharadar SF1 ARQ (PIT fundamentals) + Sharadar SEP (prices and delistings) + Sharadar TICKERS (identifier history). Documented gaps: borrow rates (no v1 source), PIT S&P 500 reconstitution effective dates (Sharadar SP500 event log is what we have). Borrow is v2.
11. **Borrow**: modeled as inventory, not as a rate. v1 has no borrow data; short tests are estimates with a visible warning. v2 integrates a borrow availability and rate feed.
12. **Tabular backbone**: Polars end-to-end. Public results objects expose `.to_pandas()`. NumPy and Numba for the inner kernel.
13. **Performance budget**: 20-year backtest on 500 names under 60 seconds on a laptop. CI checks this; regression of more than 10% fails the build.
14. **Engine self-validation**: M1 reconciles against SPY total return (within 5 bps annualized for a 20-year buy-and-hold) and against a deterministic hand-computable strategy (constant-weight monthly rebalance of three names, exact match). Failure beyond tolerance fails the build.
15. **Differential testing**: M4 or M5 includes differential testing against zipline-reloaded on three benchmark strategies with documented expected divergence and a published reconciliation report.
16. **Factor study, cost sensitivity, and CPCV fan chart**: M5 publishes one worked factor study (single-factor momentum or value, depending on data availability) with PSR-deflated Sharpe, cost-sensitivity bands, and the CPCV path distribution. The README links to this study as the canonical "what this engine does" demonstration.
17. **Reproducibility**: every figure in the README is generated by a script in `scripts/figures/` that the reader can run with one command. Seeds are explicit. Hash randomization is disabled in test runs.
18. **Numerical determinism**: floating-point reproducibility within a single (platform, BLAS version) tuple. Cross-platform reproducibility is documented as a known limitation, not promised.
19. **Stack**: Python 3.11+, uv for environment management, Polars as the tabular backbone, Pydantic for typed data models, pytest with high coverage on the engine core, mypy strict, runs on Linux and WSL2.
20. **Time budget**: v1 in four weeks from the start of engine implementation (estimated 2026-06-04 per the project's kill-early rule). If the four-week milestone passes without a demonstrable M1 (SPY reconciliation passing), the project is killed per [`feedback_kill_early`](../../memory/feedback_kill_early.md).

### Deferred to ADR 0002 (roadmap)

- The precise M1 through M5 milestone breakdown with deliverables and acceptance criteria.
- The decision on whether M5 ships before any further v2 work begins.
- The factor study target (momentum, value, low-volatility, profitability; depends on Sharadar SF1 ARQ field coverage).

### Deferred to ADR 0003 (architecture)

- The class and protocol hierarchy. The trust boundaries enumeration. The event-loop design (queue vs message bus; in-process publish/subscribe; thread-local clock injection).
- The data model for the dual-timestamp records and the Universe API.
- The policy-vs-execution interface (the AlgoStack-equivalent contract).
- The trial registry storage format and the confidence-tier metadata.
- The CPCV path generator implementation.
- The permanent-impact-register data flow.

### Status

This ADR is in **Accepted** status as of merge. The decisions above are binding on ADR 0002 and 0003. Revisiting them requires a new ADR that explicitly supersedes the relevant numbered decisions here.
