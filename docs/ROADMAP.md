# Roadmap

Status: Research phases 1 and 2 complete. Phase 3 in progress: ADRs 0001 (spec critique) and 0002 (roadmap) accepted; ADR 0003 (architecture) next. Implementation milestones M1 through M5 are now defined; see the [Implementation phase](#implementation-phase-m1-through-m5) section below. See [`README.md`](../README.md) for context.

## Phases

### Phase 1: existing-landscape survey (complete)

Goal: understand the design choices, strengths, and failure modes of existing open-source backtesters before designing our own.

Deliverable: [`docs/research/0001-existing-backtesters.md`](research/0001-existing-backtesters.md) plus per-source detail under [`docs/research/sources/`](research/sources/).

Coverage: zipline (including zipline-reloaded), backtrader, vectorbt, bt, qstrader, nautilus_trader.

Key findings carried into phase 2 and the architecture ADR: the field collectively gets corporate actions and point-in-time index membership wrong; lookahead protection should be structural (Pipeline + min-period + clock injection) not by convention; execution realism must be required, not optional; sweep mode and event-driven mode should be separate, explicitly labeled paths.

### Phase 2: methodology canon (complete)

Goal: synthesize the literature on backtest validity and execution realism. Includes Lopez de Prado AFML chapters 11 through 15, Bailey and Lopez de Prado on the deflated Sharpe and the probability of backtest overfitting, Almgren and Chriss on optimal execution and market impact, the standard treatment of point-in-time data, and seven practitioner postmortems.

Deliverable: [`docs/research/0002-methodology.md`](research/0002-methodology.md) plus per-topic source analyses under [`docs/research/sources/methodology-*.md`](research/sources/).

Key findings carried into phase 3 and the architecture ADR: the analytics layer must compute PSR, DSR, MinTRL, and a confidence-tier label by default (raw SR alone is a configuration error); the cost model defaults to SquareRootImpact with Almgren 2005 calibration (eta = 0.142, beta = 0.6, gamma = 0.314) and permanent impact must feed the price series; the data layer requires a dual-timestamp model (`period_end_dt` and `available_dt`), a typed Universe API, and persistent asset identifiers; CPCV returns a Sharpe distribution, not a scalar.

### Phase 3: spec critique and architecture (in progress)

Goal: stress-test the spec, draft the architecture, both reviewed by a skeptical agent persona before lock-in.

Deliverables:
- [`docs/decisions/0001-spec-critique.md`](decisions/0001-spec-critique.md) (spec critique plus skeptical review, **accepted**)
- `docs/decisions/0002-roadmap-review.md` (M1..Mn milestone breakdown plus skeptical review, pending)
- `docs/decisions/0003-architecture.md` (class/protocol hierarchy, event loop, trust boundaries, plus skeptical review, pending)

Each ADR captures the original proposal, the skeptical reviewer's critique, the author's response, and the final locked-in decisions.

Key locked-in decisions from ADR 0001 that constrain 0002 and 0003: scope is U.S. equity daily-bar only; framing is "teaching artifact" not "production-grade"; CPCV is primary validation surface with results as path distributions; the LdP chapter 14 scorecard is the default analytics output; cost model defaults to Almgren 2005 SquareRootImpact with sensitivity bands; six-layer architecture (data, signal, policy, execution, risk decomposition, analytics); Polars end-to-end with `.to_pandas()` boundary adapter; v1 data inventory is Sharadar SF1 ARQ + SEP + TICKERS with documented gaps on borrow and PIT S&P 500 reconstitution dates; performance budget is 20-year backtest on 500 names in under 60 seconds; v1 timeline is four weeks from engine implementation start or the project is killed per the kill-early rule.

## Implementation phase M1 through M5

Locked in [`docs/decisions/0002-roadmap-review.md`](decisions/0002-roadmap-review.md). Ten-week timeline (extended from the original four-week proposal after the skeptical-reviewer pass). The kill-early gate fires at end of week 2 if M1 SPY reconciliation fails.

### Week 1 (Monday): pre-M1 methodology docs

Two short documents that ship as contracts before any engine code lands:
- [`docs/methodology/total_return_reconstruction.md`](methodology/total_return_reconstruction.md) (pending): SPDR-published SPY total return as the authoritative reference, same-day-at-close reinvestment convention, math documented.
- [`docs/methodology/dataset_versioning.md`](methodology/dataset_versioning.md) (pending): Sharadar pull hash committed, SHA256 of parquet files, pull date recorded.

### M1 (weeks 1-2): walk skeleton with engine self-validation

Goal: prove the engine reproduces SPY total return and a hand-computable strategy.

Scope: SEP adapter; total-return reconstruction; buy-and-hold demo; constant-weight monthly rebalance demo with fractional shares; `TestClock` injection pattern; structured logging.

Acceptance: buy-and-hold SPY 2005-2024 within 5 bps annualized of SPDR-published SPY TR; constant-weight SPY/AGG/GLD monthly rebalance matches spreadsheet to 1e-10; methodology docs landed; logging works at INFO/DEBUG.

**Kill-early gate** at end of week 2: if M1 SPY reconciliation does not pass, project is killed and `POSTMORTEM.md` is written.

### M2 (weeks 3-4): cost realism with sensitivity bands

Goal: realistic P&L net of transaction costs with honest uncertainty quantification.

Scope: `SquareRootImpact` (Almgren 2005) default; `LinearImpact`, `FixedBps` alternatives; `NoImpact` only with `unsuitable_for_deployment=True` and a runtime warning; commission with typed units and /100.0 regression test; pre-trade cost estimate API; `permanent_impact_register`; sensitivity-band runner over eta in [0.05, 0.10, 0.142, 0.20, 0.30]; `--impact-model=bouchaud` flag for beta=0.5; `FillPriceModel` enum (added here once there are multiple); CI performance budget.

Acceptance: SPY $1M monthly rebalance total impact cost falls in the [eta=0.05, eta=0.30] band sanity-checked against Frazzini-Israel-Moskowitz 2018; sensitivity band renders five curves; /100.0 regression unit test passes; permanent-impact fixture verifies next-bar mid-price drop; CI runs the perf benchmark with 10% regression threshold.

### M3 (weeks 5-7): PIT data with corporate actions

Goal: PIT discipline on every data record; survivorship-bias-free universes; splits, dividends, delistings, spin-offs flow correctly.

Scope: dual-timestamp records (`period_end_dt`, `available_dt`); Sharadar SF1 ARQ + TICKERS + SP500 adapters; `Universe.is_member(asset_id, date)`; data quality contracts; SF1-vs-SEP authoritative-source resolution; America/New_York timezone convention; fractional shares; memory budget at 16 GB; corp actions (splits, cash dividends, delistings with cash proceeds across zero/cash-acquisition/stock-acquisition/Chapter-11 cases, spin-offs as cash-equivalent with bias quantified from Cusatis-Miles-Woolridge 1993 and McConnell-Ovtchinnikov 2004).

Acceptance: `IsMemberAt(t)` demo shows the 2010 vs current S&P 500 count + survivor count + CAGR delta consistent with published studies; split/dividend/delisting/spin-off test fixtures pass; reads gate on `available_dt <= simulation_dt`; data quality contracts fail loudly at ingest; SF1-vs-SEP resolution enforced; 20-year PIT backtest fits in 16 GB.

### M4 (weeks 8-9): validation infrastructure

Goal: LdP ch.14 scorecard as default analytics. CPCV with path distributions. Trial registry feeds DSR.

Scope: `analytics.sharpe` (PSR, DSR, MinTRL); `analytics.drawdown`; `analytics.concentration` (HHI); `analytics.scorecard` (Markdown); `validation.cv` with `PurgedKFoldSplitter`, `WalkForwardSplitter`, `CPCVSplitter`; `validation.trial_registry` (SQLite WAL, single-machine concurrent); `confidence_tier` enum with render-path enforcement; `docs/TESTING.md`.

Acceptance: PSR/DSR/MinTRL match the Bailey-LdP 2014 numerical example (DSR=0.971 within 1e-3); CPCV N=6 k=2 produces 5 paths as `BacktestPathDistribution`; walk-forward produces a single-path result; trial registry survives concurrent writes; render with raw SR errors unless `confidence_tier=single_run_pre_specified` and N=1; full scorecard renders for SPY.

### M5 (week 10): worked momentum study and README reproducibility

Goal: a concrete worked example demonstrating PIT discipline, cost realism, and CPCV validation on a single factor, with full reproducibility.

Scope: single-factor JT1993 12-1 momentum on PIT S&P 500 2005-2024, monthly rebalance, top-quintile long equal-weight; PSR-deflated Sharpe; cost-sensitivity band; CPCV fan chart; year-by-year decomposition; HHI; `scripts/figures/` for README reproducibility; `docs/METHODOLOGY.md` connecting phase 2 research to implementation choices.

Acceptance: momentum study Markdown report with the honest DSR conclusion (passing milestone whether the strategy clears DSR>=0.95 or fails it); `make figures` regenerates every README figure in under 5 minutes; `docs/METHODOLOGY.md` written.

Fallback if week 10 ends without the full Markdown report: ship the CPCV fan chart plus a one-paragraph honest DSR conclusion in the README; the full report becomes a v1.1 polish item.

### v1.1 backlog (explicit)

Deferred to v1.1 or later, tracked here so they do not get lost:
- Differential testing against `zipline-reloaded` on three benchmark strategies (cut from M5; Windows toolchain cost is the binding reason).
- Spin-offs as actual share distributions rather than cash equivalent.
- Rights offerings, special distributions, multi-class share creation, ticker reuse after delisting.
- Borrow availability and rate feed integration; live short-sale tests.
- ONC clustering for effective trial count `N` (currently PCA-based).
- Auction prices as a data-layer field; MOO/MOC as separate auction bars.
- Full marked-up Markdown report for the M5 worked study if the fallback was shipped.
- Full PIT S&P 500 reconstitution effective dates beyond the Sharadar event log.

## Deferred / out of scope (until reconsidered)

- Live trading. This is a backtester; live trading is explicitly out of scope for the v1 horizon.
- Options modeling. Equity only at M1; revisit after the equity engine is solid.
- Crypto-specific market microstructure. Assume traditional equities (regular sessions, T+1/T+2 settlement abstracted) at M1.
- Fixed income, FX, futures roll mechanics. Out of scope for v1.
- GPU acceleration. Profile first; defer until a hot path is empirically a bottleneck.
