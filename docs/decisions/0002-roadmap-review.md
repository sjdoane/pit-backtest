# ADR 0002: M1 through M5 roadmap with skeptical-reviewer pass

Status: Accepted.
Date: 2026-05-28.
Authors: Sam Doane (with critique and skeptical review captured below).

## Context

[`ADR 0001`](0001-spec-critique.md) locked in 20 numbered decisions. The most binding for the roadmap:

- v1 timeline is four weeks from engine implementation start, with the kill-early rule applied if M1 fails to pass.
- Scope is U.S. equity daily-bar; everything else is non-goal.
- Six-layer architecture: data, signal, policy, execution, risk decomposition, analytics.
- Polars end-to-end; v1 data inventory is Sharadar SF1 ARQ + SEP + TICKERS.
- CPCV is the primary validation surface with results as path distributions.
- Cost model is SquareRootImpact with Almgren 2005 calibration and mandatory sensitivity bands.
- Engine self-validation against SPY total return (within 5 bps annualized) and a deterministic hand-computable strategy (exact match) is required by M1.
- Differential testing against `zipline-reloaded` and a worked factor study with PSR-deflated Sharpe are required by M5.
- Performance budget is 20-year backtest on 500 names under 60 seconds, CI-enforced with a 10% regression threshold.

This ADR proposes a phased M1 through M5 implementation roadmap inside the four-week timeline, with each milestone independently demoable and concrete acceptance criteria. The skeptical-reviewer pass follows; my response and final locked-in milestones come after.

## Proposed milestone breakdown

The total scope of v1 is large; the four-week timeline assumes focused part-time work alongside the user's existing obligations. The milestones are sized so that any one of M1, M2, M3 produces a demoable artifact in roughly one week; M4 and M5 share week 4 and depend on each other. If the timeline slips beyond M1, the project is killed per the kill-early rule.

### M1: walk skeleton with engine self-validation (week 1)

**Goal**: prove that the engine can reproduce SPY total return and a hand-computable strategy. This is the foundation that all subsequent milestones build on.

**Scope**:
- `data` layer: a Sharadar SEP adapter that loads adjusted prices and constructs the dual-timestamp record format. No fundamentals yet (deferred to M3).
- `signal` and `policy` layers: stub implementations sufficient to express buy-and-hold and a constant-weight monthly rebalance.
- `execution` layer: a minimal Order/Fill flow with `FillPriceModel` enum, the `close` price model, and a zero-cost commission stub (real cost model in M2).
- `analytics` layer: scaffolding to compute total return and annualized return. No PSR/DSR/MinTRL yet.
- `Clock` injection: `TestClock` plus a stub `LiveClock` (not used at v1) to establish the pattern.
- Performance budget tracked in CI: a benchmark job runs the SPY backtest on every push and fails if wall-clock time regresses more than 10% from the prior baseline.

**Acceptance criteria**:
1. A buy-and-hold SPY backtest from 2005-01-01 through 2024-12-31 reproduces the actual SPY total return (sourced from a documented authoritative reference) within 5 basis points annualized.
2. A constant-weight monthly rebalance on three names (SPY, AGG, GLD with equal target weights) produces final P&L within floating-point precision (1e-10) of a spreadsheet hand calculation.
3. The performance budget benchmark exists in CI and fails on >10% regression.
4. `docs/TESTING.md` is created with the M1 reconciliation methodology, the SPY reference, and the hand-computable strategy specification.
5. `pyproject.toml` is committed with the locked Polars, NumPy, Pydantic, pytest, mypy versions; `uv lock` reproduces the environment.

**Demo**: `uv run pit-backtest examples/spy_buy_and_hold.py` produces an equity curve PNG matching SPY total return, and the test suite shows the reconciliation test passing with explicit deltas.

**Dependencies**: requires Sharadar SEP data subscription (estimated $50/month at the time of writing). If the data subscription is not in place by start of week 1, M1 cannot start.

**Risk**: highest-risk milestone. If SPY reconciliation fails to converge within 5 bps by end of week 1, the project is killed. Failure modes anticipated: missing dividend handling in the price feed; calendar mismatches; adjustment methodology drift between Sharadar's adjusted close and the SPY reference.

### M2: cost realism with sensitivity bands (week 2)

**Goal**: turn on the cost layer so that backtests produce realistic P&L net of transaction costs, with honest uncertainty quantification.

**Scope**:
- `cost` module under the `execution` layer: `SquareRootImpact` model with Almgren 2005 calibration (eta=0.142, beta=0.6, gamma=0.314) as default. `LinearImpact` and `FixedBps` available as alternatives. `NoImpact` available only with an explicit `unsuitable_for_deployment=True` flag and a runtime warning.
- `Commission` model with typed units (`commission_per_share`, `commission_bps`) and validation at construction time. Unit tests verify known-trade commission within floating-point precision (the backtrader `/100.0` bug class is the target).
- `pre_trade_cost_estimate(instrument, shares, direction)` method on the cost model, callable by the policy layer.
- `permanent_impact_register` per instrument: applies the permanent impact additively to subsequent bar prices visible to the signal model and to portfolio valuation.
- Sensitivity-band runner: a backtest can be re-run with `eta` swept over `[0.05, 0.10, 0.142, 0.20, 0.30]` and the results reported as a band on the equity curve.
- `--impact-model=bouchaud` flag substituting beta=0.5.

**Acceptance criteria**:
1. SPY backtest from M1 reruns with the cost layer enabled and produces a P&L lower than the zero-cost version by an amount consistent with Almgren 2005 (within a tolerance documented in `docs/TESTING.md`).
2. The sensitivity band rendering shows five SPY equity curves, one per eta value, on a single plot.
3. The commission unit test catches a deliberate /100.0 bug (a regression test that fails with the bug and passes without it).
4. Pre-trade cost estimate returns a value consistent with the realized fill cost when the strategy actually trades that quantity (within a documented tolerance).
5. A 3-bar test fixture verifies that a large sell at bar t reduces the visible mid-price at bar t+1 by the permanent impact (the `permanent_impact_register` works).

**Demo**: an updated SPY backtest plot with the eta sensitivity band rendered; the commission unit-test report showing the /100.0 regression caught.

**Dependencies**: M1 must be passing; nothing else.

### M3: PIT data with corporate actions (week 3)

**Goal**: the data layer enforces point-in-time discipline. Survivorship-bias-free universes are usable. Splits, cash dividends, delistings with cash proceeds, and spin-offs as cash flow through correctly.

**Scope**:
- Dual-timestamp data records: every fundamental and corporate-action record carries `period_end_dt` and `available_dt`. The data layer's read API gates on `available_dt <= simulation_dt`.
- Sharadar SF1 ARQ adapter: PIT fundamentals (revenue, earnings, book value, shares outstanding). Used only as a data source; no factor signals yet.
- Sharadar TICKERS adapter: persistent asset identifiers; ticker history and CUSIP resolution.
- Sharadar SP500 event log: PIT S&P 500 membership. `Universe` API exposes `is_member(asset_id, date) -> bool` with the membership table backing it.
- Corporate action handlers: splits adjust position quantities; cash dividends are paid as cash on the ex-date; delistings produce a final cash flow at the documented last-trade price and close any open position; spin-offs are treated as cash-equivalent distributions with a `spin_off_treated_as_cash=True` flag visible in the result metadata.
- Validation at backtest-construction time: every asset in the user-supplied universe must have either a delisting record or an active-status confirmation across the backtest window; gaps raise an error.
- An `IsMemberAt(t)` study: a small reproducible demo showing that the S&P 500 membership at 2010-01-01 differs from the current membership, with the survivorship impact quantified for an equal-weight strategy.

**Acceptance criteria**:
1. The universe-membership demo shows the count of names in the 2010-01-01 S&P 500 versus the current S&P 500, the count of names that survived to 2024, and the CAGR delta between an equal-weight strategy on the PIT membership and an equal-weight strategy on the current-day membership (the survivorship effect).
2. A split test fixture: a 2-for-1 split on 2015-06-15 produces correct position-quantity adjustment without P&L distortion.
3. A dividend test fixture: a $1.50 dividend on a 100-share position on 2020-03-15 produces a $150.00 cash flow on the ex-date.
4. A delisting test fixture: a position in a stock that delists on 2021-09-30 at a final trade of $4.50 produces a position close at that price on that date.
5. A spin-off test fixture: a spin-off treated as cash equivalent at the documented spin-off value flows through without breaking the parent position.
6. Reading any data record returns the value as of `available_dt`, not `period_end_dt`; a `period_end_dt` after the simulation date returns the prior known value.

**Demo**: `examples/survivorship_study.py` produces a two-panel plot (PIT membership equity curve vs current-membership equity curve) with the CAGR delta as the headline.

**Dependencies**: M1 walk-skeleton needs to be runnable. M2 cost layer not strictly required but recommended (the survivorship study with realistic costs is more honest).

### M4: validation infrastructure (week 4, first half)

**Goal**: the LdP chapter 14 scorecard exists as the default analytics output. CPCV produces a Sharpe distribution per path. The trial registry feeds DSR.

**Scope**:
- `analytics.sharpe` module: PSR, DSR, MinTRL implementations with the formulas verified against the Bailey-LdP papers' numerical examples documented in [`docs/research/sources/methodology-backtest-overfitting.md`](../research/sources/methodology-backtest-overfitting.md).
- `analytics.drawdown` module: maximum drawdown, average drawdown, drawdown duration, Calmar ratio.
- `analytics.concentration` module: HHI on bar-level PnL.
- `analytics.scorecard` module: produces the full LdP ch. 14 scorecard (general characteristics, performance, runs and drawdowns, implementation shortfall, risk-adjusted efficiency with PSR/DSR/MinTRL, attribution) as a Markdown report.
- `validation.cv` module: `PurgedKFoldSplitter` and `CPCVSplitter` operating on label-horizon metadata. CPCV produces a `BacktestPathDistribution` with phi(N, k) = (k/N) * C(N, k) paths.
- `validation.trial_registry` module: persistent storage of (dataset_fingerprint, strategy_family, SR, T, gamma_3, gamma_4, timestamp). DSR queries the registry for V[{SR_n}] and N_effective (PCA-based by default; the ONC clustering option is deferred).
- Result `confidence_tier` enum: `single_run_pre_specified`, `walk_forward_validated`, `cpcv_with_dsr_correction`, `sweep_selected_no_correction`. The render path refuses to emit a report containing a raw SR without an accompanying PSR or DSR unless the confidence tier is explicitly `single_run_pre_specified` with N=1.

**Acceptance criteria**:
1. The PSR/DSR/MinTRL implementations match the numerical examples in the Bailey-LdP papers (the Bailey-LdP 2014 example: SR_hat=1.5, T=60 months, gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4, produces DSR=0.971).
2. A CPCV run on a deterministic dataset with N=6, k=2 produces 5 paths and a Sharpe distribution; the result type is `BacktestPathDistribution`, not a scalar.
3. The trial registry persists across process restarts (SQLite-backed) and supports concurrent writes from parallel sweep runs.
4. A render call with a raw SR and no PSR/DSR raises an error unless the `single_run_pre_specified` tier is set.
5. The Markdown scorecard renders for the SPY buy-and-hold backtest from M1 with all six ch. 14 categories populated.

**Demo**: a CPCV fan chart and a Markdown scorecard for the SPY backtest with PSR, DSR, MinTRL surfaced.

**Dependencies**: M1 walk skeleton; M2 cost model (for the scorecard's implementation-shortfall section); M3 PIT data (for survivorship-corrected universe in any cross-sectional study).

### M5: worked factor study, differential testing, README reproducibility (week 4, second half)

**Goal**: the engine produces a concrete worked example demonstrating PIT discipline, cost realism, and CPCV validation on a single factor; the engine cross-validates against `zipline-reloaded`; every figure in the README regenerates from a single command.

**Scope**:
- A worked factor study: single-factor cross-sectional momentum (12-month return excluding the most recent month, the standard Jegadeesh-Titman 1993 construction) on the S&P 500 PIT membership from 2005 to 2024, monthly rebalance, top-quintile long, equal-weight. The study produces a PSR-deflated Sharpe, a cost-sensitivity band, a CPCV fan chart of OOS Sharpe across paths, and an honest "this strategy's DSR is X.XX; here is what that means" conclusion.
- Differential testing harness: run three benchmark strategies (SPY buy-and-hold, the M1 hand-computable strategy, the momentum study) through both `pit-backtest` and `zipline-reloaded`; produce a reconciliation report comparing equity curves, total return, Sharpe, max drawdown. Differences beyond a documented tolerance are flagged as defects.
- `scripts/figures/` directory: every PNG/SVG in the README is generated by a script in this directory. A single `make figures` (or `uv run make-figures`) regenerates them all in under 5 minutes.
- README cross-links to the factor study, the differential report, and the figure-generation scripts.
- `docs/METHODOLOGY.md` is written, citing the phase 2 research synthesis and tying it to the engine's actual implementation choices.

**Acceptance criteria**:
1. The momentum study runs end to end and produces a single Markdown report with: PSR-deflated Sharpe; cost-sensitivity band; CPCV fan chart of OOS Sharpe; year-by-year return decomposition; HHI concentration; conclusion paragraph that explicitly addresses whether the DSR clears a 0.95 threshold.
2. The differential test produces a reconciliation report PDF/Markdown with side-by-side equity curves for the three benchmark strategies; documented divergences are explained.
3. `make figures` regenerates every README figure in a single command; the README links every figure to its generating script.
4. `docs/METHODOLOGY.md` exists and connects the phase 2 research findings to specific implementation choices, with cross-references.

**Demo**: the worked factor study Markdown report is the centerpiece. A reviewer reading the README finds a link to the study and can both inspect the figures and rerun them.

**Dependencies**: M1 through M4 all complete and passing.

## Timeline and risk

| Week | Milestone | Headline demo |
|---|---|---|
| 1 | M1 | SPY buy-and-hold matches the actual SPY total return within 5 bps |
| 2 | M2 | SPY backtest with Almgren cost model and eta sensitivity band |
| 3 | M3 | PIT S&P 500 membership at 2010 versus today, with the survivorship CAGR delta |
| 4 (early) | M4 | CPCV fan chart and LdP scorecard for the SPY backtest |
| 4 (late) | M5 | Worked momentum study + differential test report + README reproducibility |

**Hard kill condition**: if M1's SPY reconciliation does not pass within 5 bps by end of week 1, the project is killed and a `POSTMORTEM.md` is written explaining why.

**Soft kill condition**: if M3 or M4 slip into week 5, the user reassesses whether to extend the timeline or to cut the worked study and differential test from M5. The minimum shippable v1 is M1 through M4; M5 is the "what makes this a portfolio piece" milestone and is the first thing to cut if time runs out.

**Highest-risk milestones**:
- M1: the SPY reconciliation tolerance is tight (5 bps annualized over 20 years is approximately 0.10 percentage points cumulative). If Sharadar's adjustment methodology drifts from the SPY reference, the tolerance may need to be widened or the reference recalculated.
- M3: corporate action handling has many edge cases (special distributions, multi-class share creation, ticker reuse after delisting). The v1 scope explicitly excludes these but the test fixtures need to be comprehensive enough that the cuts are visible.
- M5: differential testing against `zipline-reloaded` requires a working `zipline-reloaded` installation, which is non-trivial on Windows. The fallback is to run the differential test under WSL2 only and to document the Windows installation gap.

## Skeptical reviewer's response

The review below was produced by the same sub-agent persona that reviewed ADR 0001 (senior multi-strat-fund quant researcher, fifteen years of experience, built backtesters at three firms). Reproduced verbatim.

### Reviewer summary verdict

The roadmap is structurally competent and substantially better than the spec from last round: cost realism internalized, CPCV carried through, PIT data in M3 not buried, SPY reconciliation as the kill gate. That is a meaningfully more disciplined plan than the reviewer usually sees from a phase-2 undergrad.

It is also, as written, **undeliverable in four calendar weeks** by one part-time human with two other live projects. The roadmap collapses three to five hard problems per week and the dependencies between weeks are not honest. M2 assumes M1's fill model is stable, M3 assumes M2's commission and impact APIs do not need refactoring, M4 assumes M3's PIT plumbing works on real Sharadar pulls (which it will not, on the first try), and M5 assumes a working `zipline-reloaded` environment on Windows (which almost certainly does not exist). Zero slack, hard kill gate at end of week 1: recipe for either a real kill at day 7 or a Potemkin v1 at day 28.

Honest calendar: 8 to 11 weeks of focused effort, equivalent to roughly 10 weeks at the author's likely 15 to 25 effective hours per week alongside DripWatch, Kalshi, and undergraduate obligations.

### What the reviewer thinks the roadmap gets right

- **Acceptance-criterion discipline.** Every milestone has numbered, falsifiable gates. SPY within 5 bps; commission /100.0 regression unit test; PSR/DSR/MinTRL matching Bailey-LdP 2014. Real tests, not hand-waves.
- **Kill-early rule on M1.** SPY reconciliation is the single test that, if it fails, says the engine is fundamentally broken. Right gate.
- **`NoImpact` only with `unsuitable_for_deployment=True` flag.** Exactly the API-level safety belt that prevents quants leaving zero-cost flags on by accident.
- **Sensitivity band with eta in [0.05, 0.10, 0.142, 0.20, 0.30].** Most undergrads pick a single eta and pretend Almgren's 0.142 is a constant of nature. Showing the band is publishable hygiene.
- **Dual-timestamp records.** The right primitive. Most quants conflate "as-of" with "available-at" and get crushed.
- **CPCV with `BacktestPathDistribution`.** Reporting a single mean Sharpe from CPCV defeats the point; the roadmap kept the structure.
- **`IsMemberAt(t)` survivorship demo.** Teaching gold. One chart can make the whole project worth doing.
- **Trial registry with `confidence_tier` enforcement.** Most backtesters do not count trials at all.
- **Performance budget in CI.** Mature beyond your years. Most professional shops do not do this until they get burned twice.

### What the reviewer thinks the roadmap gets wrong

- **M1 scope creep disguised as "minimal."** Six engineering subsystems plus two acceptance demos plus a CI pipeline plus pyproject.toml lock plus docs/TESTING.md, while ingesting Sharadar SEP for the first time. No budget for the half-day on Polars version pinning, the half-day on SEP rate limits, the day on **total-return reconstruction** (dividend reinvestment is where every SPY reconciliation the reviewer has personally seen has died, and M1 does not mention it once).
- **SPY reconciliation tolerance is underspecified.** "Within 5 bps annualized of actual SPY total return" needs to name *which* SPY total return: SPDR published TR, Bloomberg TR index, reconstructed from SEP closes plus dividends with what reinvestment convention (same-day at close, next-open, end-of-month), CRSP value-weighted. These differ by 10 to 30 bps annualized over 20 years.
- **M2 acceptance criterion 1 is hand-wavy garbage.** "P&L lower than zero-cost version consistent with Almgren 2005 (tolerance documented)" is not a test. "Lower than zero" is a tautology. "Consistent with Almgren 2005" is undefined. Rewrite with a concrete bps band derived from eta=[0.05, 0.30], with the central eta=0.142 estimate falling between.
- **M3 cratered into one week.** Nine items each one to three engineering days, stacked into seven days. Delistings alone can take two weeks of senior quant time at a real shop (zero versus cash-acquisition versus stock-acquisition versus Chapter 11 reorg).
- **M4 collapses two weeks into three days.** PSR/DSR/MinTRL replication, CPCV with embargo, SQLite trial registry with concurrency, confidence-tier enforcement, full scorecard renderer.
- **M5 is not a milestone, it is a paper.** Single-factor momentum + PSR-deflated Sharpe + cost-sensitivity band + CPCV fan chart + differential test against `zipline-reloaded` on three strategies + figure-generation pipeline + METHODOLOGY.md. In three to four days. No.
- **Ordering issue: M1 SPY reconciliation gate uses raw Sharpe.** Defensible for SPY buy-and-hold where PSR is degenerate, but say so explicitly.
- **No mention of dividend reinvestment in M1 or M3.** Single biggest source of SPY total-return drift. You will get this wrong twice before getting it right.
- **No mention of fractional shares.** Constant-weight monthly rebalance with three assets at notional amounts that do not divide evenly will need them or will have rounding drift violating the 1e-10 spreadsheet match.
- **No mention of timezone handling.** Sharadar uses dates; the moment you cross-reference with simulation_dt for `available_dt` gating, you need a documented convention.

### What the reviewer thinks the roadmap missed

- **Total-return reconstruction methodology document.** Needs to exist before M1 starts. Pick benchmark, reinvestment convention, document math.
- **Data quality contracts.** Sharadar SEP has known gaps (pre-IPO bars, post-delisting phantom prices, occasional corp-action mistimings). Need a validation step asserting invariants on every pull.
- **Reproducibility lockfile beyond pyproject.toml.** Sharadar restates. Snapshot the pull, version the hash, commit the hash. Otherwise the SPY reconciliation that passes today fails six months from now with no explanation.
- **Benchmark selection for differential testing.** "Three benchmark strategies" in M5 is undefined.
- **`zipline-reloaded` on Windows: this will not work.** Hard deps on bcolz and CPython extensions; maintainer recommends WSL2 with Linux Python or Docker. You will burn two days fighting CMake, give up, install WSL2, burn another day on bifurcated Polars paths, then realize the data layer is now bifurcated. Budget honestly or cut.
- **Walk-forward as a separate validation primitive.** CPCV is right primary; a simple walk-forward (train 2005-2015, test 2016-2024) is the dumb-but-honest baseline that catches CPCV implementation bugs. Two hours of work, catches a class of errors.
- **Memory and out-of-core handling.** 20 years times 500 names times 252 days times O(20) columns at float64 is 1 GB. Polars handles in memory on a modern laptop, but joining with SF1 ARQ will hit swap on 16 GB. You committed to a 60s perf budget without saying what hardware.
- **"What to do when SF1 disagrees with SEP."** Marketcap from SF1 vs `close * sharesbas` from SEP and TICKERS will not agree. Decide which is authoritative for which use case.
- **Logging and observability.** No logging strategy. Silent failure on a corp-action edge case is worse than a crash. Add structured logging with `--log-level` in M1.

### Reviewer's pushback on the four-week timeline

CLAUDE.md says DripWatch is active with Fly deploy and App Store submission ahead (Apple review can lose three days to a single rejection cycle). Kalshi is "separate agent" but "separate agent" does not mean "zero cognitive load." StepDrill is on hold awaiting Keck Medical Reviewer who will come back at the wrong moment. USC undergrad in spring term: classes, exams, social, finals productivity cliff.

Realistic effective hours: 15 to 25 per week, not 40. At 20 hours per week, four weeks = 80 hours. The roadmap is 200 to 300 hours. 2.5x to 4x gap.

At 20 hours per week:
- M1 (skeleton + SPY reconciliation + constant-weight): 2 weeks
- M2 (cost realism + sensitivity bands): 2 weeks
- M3 (PIT + corp actions, descoped): 2 to 3 weeks
- M4 (validation + scorecard): 1 to 2 weeks
- M5 (worked study, no zipline diff): 1 to 2 weeks

Honest v1 ship window: 8 to 11 weeks of wall clock. Call it 10 weeks.

### Reviewer's pushback on specific milestone choices

- **M1 scope:** Cut `FillPriceModel` enum (one member is theater). Cut `docs/TESTING.md` from M1; belongs in M4. Cut performance budget CI from M1; belongs in M2 after the cost model lands. M1 = SEP adapter, total-return reconstruction, buy-and-hold demo, constant-weight demo, TestClock, pyproject lock. Six items, still aggressive for one week, doable for two.
- **M2 criterion 1:** Rewrite as "For SPY monthly rebalance at $1M notional, total impact cost falls in [A, B] bps annualized where A is from eta=0.05 and B from eta=0.30, central eta=0.142 between." Cross-check against Frazzini-Israel-Moskowitz 2018 (their headline is roughly 10 bps for liquid US large-cap).
- **M3 spin-off bias:** Quantify, do not just acknowledge. Cusatis-Miles-Woolridge 1993, McConnell-Ovtchinnikov 2004 show spin-offs systematically outperform parents by 10-20% over 3 years. Document affected events in test universe.
- **M4 SQLite trial registry:** "Concurrent-safe" needs specification: how many writers, WAL or rollback journal, contention model.
- **M5 zipline-reloaded on Windows:** Most likely line item to kill the timeline. Cut or budget two weeks.

### The single biggest scope cut

**Cut differential testing against `zipline-reloaded` from v1 entirely. Move to v1.1.**

The replication anchors (Bailey-LdP 2014 numerical examples for PSR/DSR; Almgren 2005 for impact; Jegadeesh-Titman 1993 for momentum) are sufficient validation for a v1 teaching artifact. The artifact is defensible without `zipline-reloaded`. It is not defensible without PSR/DSR.

Second-biggest cut if needed: M5 worked momentum study from "full Markdown report" to "one chart plus a paragraph in the README." Chart plus paragraph is 80% of the teaching value at 20% of the time.

### The single biggest soft acceptance criterion

**M5 criterion 1: "conclusion explicitly addressing DSR>=0.95."**

"Addressing" lets you write three sentences and check the box. The actual test: compute the DSR for JT1993 12-1 momentum on PIT S&P 500 2005-2024 with documented trial count (including eta sweeps), report whether it passes DSR>=0.95.

Likely outcome: it does not. Vanilla 12-1 momentum on liquid US large-cap post-2005 has a real Sharpe of 0.3 to 0.5; after honest deflation it does not clear DSR. **This is the whole point.** A teaching artifact whose worked study honestly concludes "this famous strategy does not survive deflated Sharpe scrutiny" is far more valuable than one claiming it does.

### Reviewer's recommended Week 1, day by day

20 effective hours, 4 hours per day Monday-Friday, weekend slack.

- **Monday (4h).** Do not write code. Write `docs/methodology/total_return_reconstruction.md` (pick benchmark, reinvestment convention, document math) and `docs/methodology/dataset_versioning.md` (Sharadar pull snapshot strategy, hash commitment). Two short docs. These are your contracts.
- **Tuesday (4h).** Sharadar SEP adapter. Polars-only. One function with documented schema. Unit test against a known three-day fixture.
- **Wednesday (4h).** Total-return reconstruction. Pure function. Unit-test against hand-computed three-day SPY example with one dividend.
- **Thursday (4h).** Buy-and-hold runner. TestClock. Run SPY 2005-2024. Compare to actual SPDR-published TR. Within 5 bps = viable. Within 50 bps = close but missing something, debug Friday. Off by hundreds of bps = kill signal.
- **Friday (4h).** Pass: write constant-weight demo + spreadsheet 1e-10 match. Fail: debug.
- **Weekend slack.** Whichever Friday did not finish, plus STATUS.md and CHANGELOG.md updates.

Deliberately not in week 1: `FillPriceModel` enum (premature), perf budget CI (premature), `docs/TESTING.md` (premature), pyproject.toml lock (do at end of M2 when deps stabilize).

### Reviewer's final position

**Restructure. Do not ship in four.**

Recommended restructure:
- Keep ADR 0001's twenty decisions binding.
- Rewrite timeline: M1 weeks 1-2, M2 weeks 3-4, M3 weeks 5-7, M4 weeks 8-9, M5 weeks 10-11.
- Move kill-early gate to end of week 2 on M1 SPY reconciliation.
- Cut `zipline-reloaded` differential testing from v1. Move to v1.1 backlog.
- Cut M5 worked study from full Markdown report to chart + paragraph if time pressed.
- Minimum shippable v1: M1 + M2 + M3 (PIT + splits + dividends + delistings, no spin-offs) + M4 (PSR/DSR/MinTRL + walk-forward + CPCV + scorecard, no fancy trial registry concurrency).

If four weeks is non-negotiable, descope to v0.5: M1 + M2 + basic M3 (splits + dividends only, no S&P 500 reconstitution, no spin-offs) + minimal M4 (PSR + CPCV + scorecard only). Call it v0.5 with "v1 work in progress" in README. Far better to ship an honest v0.5 than a v1 with checkboxes that do not survive scrutiny.

"You are not bad at this. You are pacing like a phase-2 undergrad who has not yet been burned by his own optimism. Get burned now in the timeline rather than in the artifact."

## My response to the reviewer

I am accepting most of this. The reviewer is right that the four-week timeline was optimistic by 2x to 3x at honest hours, and the consequence of forcing it would be either a kill at day 7 or a Potemkin v1 at day 28. Both are worse outcomes than honest extension.

### Accepted

1. **Timeline extension to ten weeks at honest pace.** M1 weeks 1-2, M2 weeks 3-4, M3 weeks 5-7, M4 weeks 8-9, M5 week 10. The original four-week commitment in ADR 0001 decision 20 is superseded here.
2. **Kill-early gate moves to end of week 2** on M1 SPY reconciliation. Week 1 is too tight to fail honestly.
3. **Pre-M1 methodology docs as Monday week 1 work.** `docs/methodology/total_return_reconstruction.md` (benchmark: SPDR-published SPY total return as the authoritative reference; reinvestment convention: close-of-ex-date; math documented). `docs/methodology/dataset_versioning.md` (Sharadar pull hash committed; SHA256 of the parquet files; pull date recorded).
4. **Cut zipline-reloaded differential testing from v1.** Moved to v1.1 backlog. The Bailey-LdP 2014, Almgren 2005, Jegadeesh-Titman 1993 replication anchors stand as the validation set for v1.
5. **M1 scope tightened.** SEP adapter, total-return reconstruction, buy-and-hold demo, constant-weight demo, `TestClock`, structured logging with `--log-level` (added per the reviewer's logging point). The following move out of M1: `FillPriceModel` enum (added in M2 when there are two models), performance budget CI (added in M2), `docs/TESTING.md` (added in M4), pyproject lockfile (refined in M2 when dependencies stabilize).
6. **SPY reconciliation tolerance explicitly named.** Authoritative reference: SPDR-published SPY total return series. Reinvestment convention: same-day-at-close on ex-date. Tolerance: within 5 bps annualized over 20 years. The reconciliation tests this exact specification; failure is unambiguous.
7. **M2 acceptance criterion 1 rewritten.** Not "consistent with Almgren 2005." The new criterion: for SPY monthly rebalance at $1M notional from 2005 to 2024, total impact cost in basis points annualized falls in `[A, B]` where `A` is the model output at `eta=0.05` and `B` is the model output at `eta=0.30`, with the central `eta=0.142` estimate falling between. Cross-checked against Frazzini-Israel-Moskowitz 2018 "Trading Costs" (their headline of ~10 bps for liquid US large-cap is the order-of-magnitude sanity check).
8. **M2 receives performance budget CI** (moved from M1). Target hardware spec documented in CI configuration: GitHub Actions `ubuntu-latest` runner, 4 vCPU, 16 GB RAM. The 60-second budget is calibrated to this baseline.
9. **M3 dividend reinvestment explicitly named** at the data-layer level. Adjusted close uses the standard CRSP-style cumulative-factor approach with documented ex-date treatment.
10. **M3 fractional shares supported by default** at the position-management level. The constant-weight rebalance demo uses fractional shares so the 1e-10 spreadsheet match is achievable.
11. **M3 timezone convention named.** All Sharadar dates interpreted as end-of-day America/New_York (16:00 ET close). `available_dt` for SF1 records uses `datekey` which is the SEC submission date. Cross-references with `simulation_dt` use the convention `available_dt <= simulation_dt` with both in America/New_York.
12. **M3 data quality contracts added.** `data.validation.contracts` module with the invariants: every TICKERS row has SEP price within 5 trading days of `firstpricedate`; no SEP bars after `delisted`; SF1 `datekey` is non-null for ARQ rows after 1990; no duplicate `(ticker, datekey)` pairs in SF1. Failures raise at ingest time with the offending rows surfaced.
13. **M3 SF1-vs-SEP authoritative-source decision.** Market cap: SF1 `marketcap` is authoritative for the as-reported figure; computed `close * sharesbas` is used only when SF1 is missing and is flagged as `estimated_marketcap=True`. Shares outstanding: SF1 `sharesbas` is authoritative for fundamental ratios; SEP/TICKERS counts are used for portfolio sizing where they match contemporaneous SF1, else flagged.
14. **M3 spin-off bias quantified.** Documentation note in `docs/METHODOLOGY.md` (written at M5) cites Cusatis, Miles, Woolridge (1993) and McConnell, Ovtchinnikov (2004) showing spin-offs systematically outperform parents by 10 to 20% over 3 years. The v1 cash-equivalent treatment introduces a measurable negative bias; the affected events in the v1 test universe are listed in the methodology doc.
15. **M3 memory budget documented.** Target: 16 GB laptop. Polars frame for 20 years x 500 names x daily x 20 columns is approximately 1 GB; joined with SF1 ARQ approximately 2 to 3 GB. Headroom verified; out-of-core lazy evaluation is the fallback if joins exceed memory.
16. **M3 scope narrowed.** Splits, cash dividends, delistings with cash proceeds (zero / cash acquisition / stock acquisition all return cash at the documented last-trade price; Chapter 11 reorgs treated as a delisting at zero with a documented bias note). Stock acquisitions in M3 are explicitly cash-equivalent at the announced deal price; the gap is documented. Spin-offs as cash-equivalent ship with the quantified bias note from decision 14 above.
17. **M4 walk-forward as a separate primitive added.** `validation.cv.WalkForwardSplitter` ships alongside `PurgedKFoldSplitter` and `CPCVSplitter`. Two hours of work, catches a class of CPCV implementation bugs as the reviewer notes.
18. **M4 receives `docs/TESTING.md`** (moved from M1). Documents the SPY reconciliation methodology, the hand-computable strategy, the corp-action test fixtures, the CPCV golden tests, and the differential-testing roadmap deferred to v1.1.
19. **M4 SQLite trial registry concurrency clarified.** WAL mode; supports concurrent reads from multiple notebooks plus serialized writes; acceptance test verifies that two parallel `pytest -xvs` runs writing to the same registry produce a consistent, non-corrupted state. "Concurrent-safe" is scoped to single-machine multi-process, not distributed.
20. **M5 DSR acceptance criterion rewritten** from "addressing DSR>=0.95" to "honestly compute and report the DSR for JT1993 12-1 momentum on the PIT S&P 500 universe from 2005-2024 with the documented trial count from M2's eta sweep plus any momentum-construction parameter sweep; pass the milestone whether the strategy clears DSR>=0.95 or fails it; document the result either way." The teaching value lives equally in pass and fail outcomes; the criterion lives in the honesty of the computation, not the result.
21. **M5 worked study has a fallback.** If end-of-week-10 is reached without the full Markdown report ready, the minimum shippable form is one chart (the CPCV fan chart for JT1993 momentum) plus a one-paragraph honest DSR conclusion in the README. The Markdown report can be a v1.1 polish item.

### Contested

The reviewer's day-by-day plan for week 1 (Mon docs, Tue SEP, Wed TR, Thu runner, Fri constant-weight or debug) is good and I will use it as a template. I will not commit to a fixed daily schedule because real life will intervene. `docs/methodology/total_return_reconstruction.md` and `docs/methodology/dataset_versioning.md` ship as Monday work; the rest of the week is flexible against the M1 acceptance criteria.

The reviewer's suggestion of v0.5 fallback (descope rather than extend) is the alternative I am rejecting. Extending to ten weeks is the right call given the methodology and validation requirements. Shipping a v0.5 without PSR/DSR or CPCV would fail decision 4 of ADR 0001 (which made PSR/DSR/MinTRL non-optional) and would not be the teaching artifact ADR 0001 committed to.

### Final milestone decisions

These supersede ADR 0001 decision 20 (the four-week timeline). All other ADR 0001 decisions remain binding.

#### Timeline (locked)

| Weeks | Milestone |
|---|---|
| Week 1 (Mon) | Pre-M1 methodology docs |
| Weeks 1-2 | M1: walk skeleton + SPY reconciliation |
| End of week 2 | **Kill-early gate**: if M1 acceptance criteria fail, project is killed and `POSTMORTEM.md` is written |
| Weeks 3-4 | M2: cost realism + sensitivity bands + perf budget CI |
| Weeks 5-7 | M3: PIT data + corporate actions |
| Weeks 8-9 | M4: validation infrastructure + scorecard |
| Week 10 | M5: worked momentum study + README reproducibility |

#### M1 (locked)

Scope: SEP adapter; total-return reconstruction; buy-and-hold demo; constant-weight monthly rebalance demo; `TestClock` injection pattern; structured logging with `--log-level` flag. **Not in M1**: `FillPriceModel` enum (M2); performance budget CI (M2); `docs/TESTING.md` (M4); pyproject lockfile finalization (M2).

Acceptance criteria:
1. Buy-and-hold SPY from 2005-01-01 through 2024-12-31 reproduces SPDR-published SPY total return within 5 bps annualized; reinvestment convention is same-day-at-close on ex-date. **Superseded by [ADR 0006](0006-trailing-period-spy-reconciliation.md):** the comparison surface is now SSGA's published trailing 1Y / 3Y / 5Y / 10Y / SI annualizations anchored on SSGA's `as_of_date`; the 5-bp tolerance per period and the same-day-at-close reinvestment convention are unchanged.
2. Constant-weight monthly rebalance on SPY, AGG, GLD with equal target weights and fractional-share support produces final P&L matching a spreadsheet hand calculation to 1e-10.
3. `docs/methodology/total_return_reconstruction.md` and `docs/methodology/dataset_versioning.md` exist with the math and the Sharadar pull hash committed.
4. Structured logging works at INFO and DEBUG levels.

#### M2 (locked)

Scope: `SquareRootImpact` (Almgren 2005) as default; `LinearImpact`, `FixedBps` as alternatives; `NoImpact` only with `unsuitable_for_deployment=True` flag and runtime warning; `Commission` with typed units and /100.0 regression unit test; `pre_trade_cost_estimate` API; `permanent_impact_register`; sensitivity-band runner over eta in [0.05, 0.10, 0.142, 0.20, 0.30]; `--impact-model=bouchaud` flag for beta=0.5; `FillPriceModel` enum (added here, when there is more than one option); performance budget CI on the SPY backtest.

Acceptance criteria:
1. SPY monthly rebalance at $1M notional from 2005 to 2024 produces total impact cost in `[A, B]` bps annualized where `A` is the model output at `eta=0.05` and `B` at `eta=0.30`, with `eta=0.142` central estimate falling between. Sanity-checked against Frazzini-Israel-Moskowitz 2018 (~10 bps for liquid US large-cap). **Superseded by [ADR 0007](0007-fim-2018-demoted-to-upper-ceiling.md):** the formula-derived band is the gate; FIM 2018 is preserved as an upper-ceiling sanity check (central cost < 50 bps annualized) rather than a central-estimate target, because SPY at $1M notional is sub-scale for FIM's institutional calibration.
2. Sensitivity band rendering shows five SPY equity curves on one plot.
3. Commission unit test deliberately fails when a /100.0 silent rescale is introduced and passes when removed.
4. Pre-trade cost matches realized fill cost within the documented tolerance.
5. Three-bar fixture verifies the `permanent_impact_register` lowers the next bar's visible mid-price.
6. CI runs the perf benchmark on every push to `main`; >10% regression fails the build.

#### M3 (locked)

Scope: dual-timestamp records on every fundamental and corp-action record; Sharadar SF1 ARQ + TICKERS + SP500 adapters; `Universe.is_member(asset_id, date)`; data quality contracts; SF1-vs-SEP authoritative-source resolution; splits, cash dividends, delistings with cash proceeds (zero / cash acquisition / stock acquisition as cash at announced price / Chapter 11 as zero with bias note); spin-offs as cash equivalent with quantified bias from CMW1993 and MO2004; America/New_York timezone convention; fractional shares; memory budget at 16 GB.

Acceptance criteria:
1. `IsMemberAt(t)` demo shows the 2010-01-01 S&P 500 count, the current S&P 500 count, the survivor count, the equal-weight CAGR delta. Numbers consistent with published survivorship-bias studies.
2. Split, dividend, delisting, spin-off test fixtures each pass with the documented semantics.
3. Data reads gate on `available_dt <= simulation_dt`.
4. Data quality contracts fail loudly at ingest time when invariants are violated.
5. The SF1-vs-SEP authoritative-source resolution is documented and enforced; conflicts surface as flagged records.
6. A 20-year PIT S&P 500 backtest fits in 16 GB; out-of-core fallback is documented.

#### M4 (locked)

Scope: `analytics.sharpe` (PSR, DSR, MinTRL); `analytics.drawdown`; `analytics.concentration` (HHI); `analytics.scorecard` (full LdP ch.14 Markdown); `validation.cv` with `PurgedKFoldSplitter`, `WalkForwardSplitter`, `CPCVSplitter`; `validation.trial_registry` (SQLite WAL, single-machine concurrent); `confidence_tier` enum with render-path enforcement; `docs/TESTING.md` written.

Acceptance criteria:
1. PSR/DSR/MinTRL implementations match the Bailey-LdP 2014 numerical example: SR_hat=1.5, T=60 months, gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4, DSR=0.971 (within 1e-3).
2. CPCV with N=6, k=2 on a deterministic dataset produces 5 paths and a `BacktestPathDistribution`.
3. Walk-forward (train 2005-2015, test 2016-2024) produces a single-path result; the result type is comparable to the CPCV path-distribution at phi=1.
4. Trial registry persists across process restart; two parallel `pytest -xvs` runs produce a consistent registry.
5. Render call with raw SR and no PSR/DSR raises unless `confidence_tier=single_run_pre_specified` and `N=1`.
6. Full LdP ch.14 scorecard renders for the SPY buy-and-hold backtest.
7. `docs/TESTING.md` documents the SPY reconciliation, the hand-computable strategy, the corp-action fixtures, the CPCV golden tests, and the v1.1 differential-testing roadmap.

#### M5 (locked)

Scope: single-factor JT1993 12-1 momentum on PIT S&P 500 2005-2024 with monthly rebalance and top-quintile long equal-weight; PSR-deflated Sharpe; cost-sensitivity band; CPCV fan chart; year-by-year decomposition; HHI; `scripts/figures/` for README reproducibility; `docs/METHODOLOGY.md` connecting phase 2 research to implementation. **Not in M5**: differential testing against `zipline-reloaded` (moved to v1.1).

Acceptance criteria:
1. Momentum study Markdown report produced. Includes PSR-deflated Sharpe, cost-sensitivity band, CPCV fan chart, year-by-year decomposition, HHI, and an honest conclusion paragraph that reports DSR with full trial count (including eta sweeps and any construction parameters) and explicitly states whether the strategy clears DSR>=0.95.
2. `scripts/figures/` and `make figures` (or `uv run scripts/figures/regenerate.py`) regenerate every README figure in under 5 minutes.
3. `docs/METHODOLOGY.md` exists with phase 2 cross-references and per-implementation-choice citations.

Fallback: if week 10 ends without the full Markdown report ready, the minimum shippable form is the CPCV fan chart plus the honest DSR conclusion paragraph in the README. The full report becomes a v1.1 polish item.

#### v1.1 backlog (explicit)

The items below were considered and explicitly deferred to v1.1 or later. They are tracked here so they do not get lost.

- Differential testing against `zipline-reloaded` on three benchmark strategies (cut from M5; the Windows toolchain cost is the binding reason).
- Spin-offs as actual share distributions rather than cash equivalent.
- Rights offerings, special distributions, multi-class share creation, ticker reuse after delisting.
- Borrow availability and rate feed integration; live short-sale tests.
- ONC clustering for effective trial count `N` (currently PCA-based).
- Auction prices as a data-layer field; MOO/MOC as separate auction bars rather than open/close + slippage.
- The full marked-up Markdown report for the M5 worked study if the fallback (chart + paragraph) was shipped.
- Full PIT S&P 500 reconstitution effective dates beyond the Sharadar event log.

### Status

This ADR is in **Accepted** status as of merge. The timeline restructure supersedes ADR 0001 decision 20. All other ADR 0001 decisions remain binding. Revisiting any of the M1 through M5 decisions above requires a new ADR.
