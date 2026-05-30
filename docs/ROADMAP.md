# Roadmap

Status: Research phases 1 and 2 complete. Phase 3 complete: all three ADRs accepted (0001 spec critique, 0002 M1 through M5 roadmap, 0003 architecture). The implementation phase begins on the Monday after ADR 0003 merges, with pre-M1 methodology documents, and proceeds through M1 through M5 over ten weeks. See [`README.md`](../README.md) for context.

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

### Phase 3: spec critique and architecture (complete)

All three phase-3 ADRs plus the M1-implementation-phase ADRs 0004, 0006 and the M2-implementation-phase ADRs 0005, 0007 accepted and merged:
- [`docs/decisions/0001-spec-critique.md`](decisions/0001-spec-critique.md): 20 locked decisions reframing the project as a teaching artifact with explicit non-goals.
- [`docs/decisions/0002-roadmap-review.md`](decisions/0002-roadmap-review.md): 21 locked decisions defining M1 through M5 over ten weeks with the week-2 kill gate. M1 acceptance criterion 1 superseded by ADR 0006 (trailing-period window); M2 acceptance criterion 1 superseded by ADR 0007 (formula-derived band is the gate; FIM 2018 demoted to upper-ceiling sanity check).
- [`docs/decisions/0003-architecture.md`](decisions/0003-architecture.md): 23 locked decisions on the protocol hierarchy, event loop, trust boundaries, data model.
- [`docs/decisions/0004-rebalance-calendar-independence.md`](decisions/0004-rebalance-calendar-independence.md): rebalance calendar is fund-policy-determined and independent of the backtest window; `start_dt` is not forced as a rebalance date. Captured during the M1-day-3 skeptical-reviewer pass.
- [`docs/decisions/0005-m2-cost-realism-plan.md`](decisions/0005-m2-cost-realism-plan.md): M2 implementation plan (cost realism with sensitivity bands) with 18 locked decisions covering the Almgren formula form and units, VWAP refusal, the `ImpactedPriceSource` decorator + policy split, the pre-trade vs fill-cost tolerance contract, the /100 regression-test target, the four-PR split. Captured during the pre-M2 Plan + skeptical-reviewer pass.
- [`docs/decisions/0006-trailing-period-spy-reconciliation.md`](decisions/0006-trailing-period-spy-reconciliation.md): SPY reconciliation reframed from a single 2005-2024 window to SSGA's published trailing 1Y / 3Y / 5Y / 10Y / SI periods anchored on SSGA's `as_of_date`. 18 locked decisions including snap-backward anchor row, `ExpenseRatioSchedule` for the 2003-11-01 step, three-way overall verdict, the post-pull range assertion in `scripts/pull_m1_data.py`. Plan + skeptical-reviewer pass surfaced 2 Critical + 4 High findings; all addressed before code.
- [`docs/decisions/0007-fim-2018-demoted-to-upper-ceiling.md`](decisions/0007-fim-2018-demoted-to-upper-ceiling.md): M2 cost-realism acceptance criterion revised. The Almgren formula-derived `[eta=0.05, eta=0.30]` band is the gate; FIM 2018 is preserved as a 50-bp annualized upper-ceiling sanity check, because SPY at $1M notional is sub-scale for FIM's institutional calibration.

Key architectural decisions from ADR 0003 that constrain implementation: Pydantic only at adapter load, CLI/config, and user-facing render targets (everywhere else uses attrs with `slots=True, frozen=True`); CashFlow split from CorporateAction as two streams; permanent impact lives in the data source layer via `ImpactedPriceSource`, not as a separate register; `PreTradeCostEstimator` and `FillCostComputer` are separate protocols; `Signal.compute` returns `dict[AssetId, float]`; `MatchingEngine.submit` returns `list[Fill]`; Clock includes `is_market_open` and `next_bar`; `AssetId = NewType("AssetId", int)` with a separate `IdentifierResolver` for v2 forward compatibility; LongOnlyPolicy at v1; determinism invariant documented; trust boundaries enumerated in 11 items.

## Implementation phase M1 through M5

Locked in [`docs/decisions/0002-roadmap-review.md`](decisions/0002-roadmap-review.md). Ten-week timeline (extended from the original four-week proposal after the skeptical-reviewer pass). The kill-early gate fires at end of week 2 if M1 SPY reconciliation fails.

### Week 1 (Monday): pre-M1 methodology and package scaffold

Five deliverables ship as contracts before any engine logic lands. All five are landed in PR-stage (this PR):
- [`docs/methodology/total_return_reconstruction.md`](methodology/total_return_reconstruction.md): SPDR-published SPY NAV TR as the authoritative reference, same-day-at-close reinvestment convention, math documented with toy three-day and SPY Q1 2024 worked examples, tolerance budget breakdown.
- [`docs/methodology/dataset_versioning.md`](methodology/dataset_versioning.md): Sharadar SF1 ARQ + SEP + TICKERS + SP500 inventory; pull procedure with SHA256 manifest at `data/snapshots/manifest.toml`; restatement-handling rationale.
- [`docs/methodology/pydantic_polars_boundary.md`](methodology/pydantic_polars_boundary.md): Pydantic restricted to three surfaces (adapter load, CLI/config, user-facing render targets); attrs `slots=True frozen=True` for every inner-loop type; performance-cost numbers for orientation.
- [`docs/methodology/determinism.md`](methodology/determinism.md): the bit-identical-outputs invariant; five requirements (pinned Polars, injected RNG, sorted output frames, no `set` iteration in policy/signal, per-worker POLARS_MAX_THREADS=1); 11-item trust boundary list with mitigations per item; per-platform reproducibility caveat.
- `src/pit_backtest/` package layout per the locked architecture, with protocols stubbed (`Protocol` body `...`; concrete classes raise `NotImplementedError("<milestone> deliverable")`); `pyproject.toml` with pinned dependencies; minimal `tests/test_scaffold.py` verifying imports, attrs immutability, FillPriceModel requirement, NoImpact flag enforcement, and render-path enforcement on raw SR.

### M1 (weeks 1-2): walk skeleton with engine self-validation

Goal: prove the engine reproduces SPY total return and a hand-computable strategy.

Scope: SEP adapter; total-return reconstruction; buy-and-hold demo; constant-weight monthly rebalance demo with fractional shares; `TestClock` injection pattern; structured logging.

Acceptance: per ADR 0006, buy-and-hold SPY reconciles against SSGA's published trailing 1Y / 3Y / 5Y / 10Y / SI annualizations (anchored on SSGA's `as_of_date`) within 5 bps annualized per window; constant-weight SPY/AGG/GLD monthly rebalance matches spreadsheet to 1e-10; methodology docs landed; logging works at INFO/DEBUG.

**Kill-early gate** at end of week 2: if M1 SPY reconciliation does not pass, project is killed and `POSTMORTEM.md` is written.

### M2 (weeks 3-4): cost realism with sensitivity bands

Goal: realistic P&L net of transaction costs with honest uncertainty quantification.

Scope: `SquareRootImpact` (Almgren 2005) default; `LinearImpact`, `FixedBps` alternatives; `NoImpact` only with `unsuitable_for_deployment=True` and a runtime warning; commission with typed units and /100.0 regression test; pre-trade cost estimate API; `permanent_impact_register`; sensitivity-band runner over eta in [0.05, 0.10, 0.142, 0.20, 0.30]; `--impact-model=bouchaud` flag for beta=0.5; `FillPriceModel` enum (added here once there are multiple); CI performance budget.

Acceptance: SPY $1M monthly rebalance total impact cost falls in the [eta=0.05, eta=0.30] band sanity-checked against Frazzini-Israel-Moskowitz 2018; sensitivity band renders five curves; /100.0 regression unit test passes; permanent-impact fixture verifies next-bar mid-price drop; CI runs the perf benchmark with 10% regression threshold.

Progress:
- **PR A shipped** (cost-model math + Commission + golden fixture + ADR 0006/0007). Almgren formula + MarketStateLookup + PerShareCommission/BasisPointsCommission + rolling helpers + `docs/methodology/cost_model_tolerance.md`. The FIM 2018 acceptance criterion 1 is the formula-derived `[eta=0.05, eta=0.30]` band as the gate with 50 bp annualized as the upper-ceiling sanity check (ADR 0007).
- **PR B shipped** (ImpactedPriceSource + SquareRootImpactMatchingEngine + BarLoop wiring + ADR 0009). ImpactedPriceSource standalone decorator with per-asset cumulative impact register; SquareRootImpactMatchingEngine supporting OPEN/CLOSE/ARRIVAL (NEXT_BAR_OPEN deferred to M3); MatchingEngine Protocol extended with `on_bar_start`; BarLoop wires cost_estimator to policy per ADR 0003 decision 4; Layer 2 1e-10 invariant split into two tests; golden fixture E2E; permanent-impact next-bar mid-drops fixture per acceptance criterion 5; determinism trust boundary extended to 12 items.
- **PR C1 shipped** (SensitivityBand + Runner.run_sweep + examples/spy_cost_sensitivity.py + ADR 0010). Sensitivity-band attrs container with from_run_sweep factory and confidence-tier gating; Runner.run_sweep with spawn-only multiproc, POLARS_MAX_THREADS=1 worker bootstrap, picklability + dry-run probes at submit time, num_workers default reserving one core for parent process; SPY cost-sensitivity CLI demo at eta in (0.05, 0.10, 0.142, 0.20, 0.30). The original PR C plan also landed active tolerance enforcement; the Plan-reviewer surfaced 3 Critical findings (tolerance check is structurally dead under shared-instance dispatch; mid_at_estimate source is wrong; Almgren cost model is mid-INSENSITIVE so the check measures the wrong quantity). Split: PR C1 ships sensitivity band; PR C2 will ship active tolerance enforcement with ADR 0011.
- **PR C2 shipped** (tolerance contract dormancy + ADR 0011). 4-member council (Realist/Quant/Builder/Growth) + verifier per session_rules.md rule 1 settled on HYBRID = "docs + tests + Realist's NotImplementedError tripwire" pattern. Verifier corrected the Quant's `epsilon_bps > 0` activation gate (wrong on physics; epsilon controls slippage not impact). Verifier corrected the Builder's "180 LOC reactivation cost" (PR B's `test_cost_estimate_vs_fill_tolerance.py` already ships symbolic exercise; dormant-scaffold reduces to ~30 LOC of test additions + ADR). Verdict: NO Order Decimal field, NO matcher check, NO `CostEstimateVsFillMismatchError`, NO BarLoop/TargetPositions/Policy changes. Just `Order.estimate_bps_at_submit` NotImplementedError stub property + README design-pillar line + `@pytest.mark.dormant_until_m3` skip-test marker + ADR 0011. Activation gate is structural (distinct policy-time vs matcher-time `MarketStateLookup` snapshots), not `epsilon_bps > 0`, not a calendar date.
- **PR D Phase 0 shipped** (perf-budget infrastructure + warning-mode workflow + bouchaud CLI flag + BarLoop.timing_breakdown opt-in + ADR 0012). 4-member council (Realist/Quant/Builder/Growth: A1 vote 3-1; B1 vote 2-1-1) + Verifier HYBRID synthesis converted to Phase 0 + Phase 1 split. Verifier corrected the council on noise-floor preconditions (Realist's "median-of-N" and Growth's "publish 7-run stdev" are the same recommendation in two registers; threshold = max(20%, 3 * sigma) honors Quant's CoV objection without abandoning README's public CI promise). Phase 0 ships the workflow in warning-only mode; the `.bench-baseline.json` is a bootstrap placeholder with `n_runs: 0`. Phase 1 follow-up PR runs the workflow once on main, commits empirical median + stdev, and flips `bench/compare.py` to `--phase-1-gate`.
- **PR D Phase 1 shipped** (perf-budget workflow flipped from warning-only to gated at `max(20%, 3 * sigma)`). `workflow_dispatch` run 26672772043 on main HEAD `293a2ad` collected the 7-run median + stdev (median = 0.0430 s, stdev = 0.000225 s, CoV = 0.52%; runner image `ubuntu24-20260525.161.1`). Empirical `.bench-baseline.json` committed; `.github/workflows/perf-budget.yml` passes `--phase-1-gate` to `bench/compare.py`. The CoV is well inside the Quant council member's flagged 5-15% range; the 20% floor binds (not the 3-sigma term). The synthetic harness is a SPY-only single-ticker probe at 43 ms (two orders of magnitude under the 60-second 500-name production target); M3 PIT-data work, which makes 500-name universes available, will revisit the harness shape. M2 is now fully shipped.

### M3 (weeks 5-7): PIT data with corporate actions

Goal: PIT discipline on every data record; survivorship-bias-free universes; splits, dividends, delistings, spin-offs flow correctly.

Scope: dual-timestamp records (`period_end_dt`, `available_dt`); Sharadar SF1 ARQ + TICKERS + SP500 adapters; `Universe.is_member(asset_id, date)`; data quality contracts; SF1-vs-SEP authoritative-source resolution; America/New_York timezone convention; fractional shares; memory budget at 16 GB; corp actions (splits, cash dividends, delistings with cash proceeds across zero/cash-acquisition/stock-acquisition/Chapter-11 cases, spin-offs as cash-equivalent with bias quantified from Cusatis-Miles-Woolridge 1993 and McConnell-Ovtchinnikov 2004).

Acceptance: `IsMemberAt(t)` demo shows the 2010 vs current S&P 500 count + survivor count + CAGR delta consistent with published studies; split/dividend/delisting/spin-off test fixtures pass; reads gate on `available_dt <= simulation_dt`; data quality contracts fail loudly at ingest; SF1-vs-SEP resolution enforced; 20-year PIT backtest fits in 16 GB.

Progress:
- **PR 1 shipped (data layer foundation)**: `SharadarPermatickerResolver` real implementation backed by Sharadar TICKERS with multi-match raise-on-ambiguity policy; `SharadarDataSource.read_tickers(...)` and `read_sf1_arq(...)` low-level Polars readers with cast-before-filter contract and PIT dimension rejection (MRQ / MRT / MRY); `LookaheadLeakError` + `assert_not_lookahead(available_dt, simulation_dt, *, context, period_end_dt=None)` helper in `src/pit_backtest/data/contracts.py` that subsequent M3 PRs call at the entry of every per-row PitDataSource method. Per-row PitDataSource stubs unchanged. 39 new tests across `test_contracts.py` (new), `test_resolver.py` (new), and `test_sharadar_adapter.py` (extended). Plan + Plan-reviewer + post-impl reviewer ran per project rule 2; no new ADR (architecture locked by ADRs 0001 + 0002 + 0003).
- **PR 2 shipped (per-row get_price + get_fundamental)**: `SharadarDataSource.get_price(asset_id, dt, field)` and `get_fundamental(asset_id, available_dt, field, flavor)` real implementations consuming the M3 PR 1 primitives. Lazy `SharadarPermatickerResolver` via `cached_property`; Decimal at boundary via `to_boundary_decimal` (renamed from `_to_boundary_decimal` to public name; single source of truth across `data.sources` and `execution.cost`). `get_price` raises `PriceNotFoundError(KeyError)` for missing bars or NULL fields; `get_fundamental` returns None for missing rows or null fields and raises ValueError for non-PIT flavors and unknown field columns. Volume returns via `Decimal(int(value))` direct (lossless at any magnitude). The lookahead-leak regression test asserts `get_fundamental(available_dt=2024-04-14)` returns the 2024-01-15 datekey row's revenue, NOT the 2024-04-15 row; a `<` vs `<=` typo would trip it. 23 new tests in `tests/data/test_sharadar_adapter.py`. Per-row stubs unchanged for `get_corporate_actions`, `get_cash_flows`, `members_at`, `get_delisting`. **Out-of-scope per PR scope**: discriminated union dispatch (PR 3); universe (PR 4); data quality contracts (PR 5).
- **PR 3 shipped (corporate actions + cash flows + delisting dispatch)**: `SharadarDataSource.get_corporate_actions(asset_id, start_dt, end_dt)`, `get_cash_flows(...)`, and `get_delisting(asset_id)` real implementations dispatched over the Sharadar ACTIONS string + TICKERS-derived delisting record. v1 dispatch table locks `dividend -> cash_dividend CashFlow`, `split -> SplitAction`, `spinoff -> spinoff_cash_equivalent CashFlow`. Skipped (announce-only + TICKERS-routed per ADR 0002 dec 16): `listed`, `initiated`, `delisted`, `transfer`, `tradinghaltresumed`, `acquisitionby*`, `bankruptcy*`. Unknown codes log a WARNING and skip (Plan-reviewer Counter on Choice 1: vendor schema additions must not crash backtests). Delisting cash from SEP `closeunadj` at `lastpricedate` per `docs/methodology/dataset_versioning.md:25`. Chapter 11 documented as a v1 approximation overstating proceeds vs the v1.1 bankruptcy-code-driven zero baseline. `DelistingDataQualityError` for missing SEP at lastpricedate or NULL closeunadj. Explicit ordinal sort for `get_cash_flows` (cash_dividend < spinoff < delisting per ADR 0003 dec 13). 24 new tests in `test_sharadar_adapter.py` + 1 in `test_resolver.py`. Stock-for-stock acquisitions + share-distribution spin-offs + bankruptcy-zero deferred to v1.1 per ADR 0002 dec 14 + dec 16. `members_at` stays NotImplementedError for PR 4.
- **PR 4 shipped (universe + members_at)**: `SharadarSP500Universe` real implementation with `is_member`, `members_at`, `membership_spells` replacing the three `NotImplementedError("M3 deliverable")` stubs. Event-log replay state machine; raises `UniverseValidationError` on double-add, remove-without-add, unknown action, resolver-unknown-ticker (with `from exc` chaining); same-date "added" + "removed" pair produces a documented one-day interval. `Universe.membership_spells` Protocol return type amended to `list[tuple[datetime, datetime | None]]` for honest open-ended encoding; `engine/m1_demo.py::FixedTickerUniverse` stub updated to match. `SharadarDataSource.read_sp500(*, ticker, action, start_dt, end_dt)` general reader (cast-before-filter; selects only the documented (ticker, date, action) columns). `@cached_property _sp500_universe` mirrors PR 2's lazy resolver pattern. `SharadarDataSource.members_at(universe_id, dt)` wired with `{"sp500"}` allowlist; this was the LAST `NotImplementedError("M3 deliverable")` stub on SharadarDataSource. 22 new tests in `test_sharadar_adapter.py` + 11 in new `test_universe.py`. Structural PIT regression: an "added" event at 2026 must NOT cause `members_at(2010)` to include the future-added asset. Multi-interval testing happens in inline bundles with a synthetic `MULTI` ticker so the multi-interval narrative does not conflate SP500 membership with TICKERS lifecycle (Plan-reviewer Critical 1). IsMemberAt(t) demo deferred to PR 5 alongside data quality (coherent M3 acceptance-criteria milestone).
- **PR 5 pending**: data quality contracts (5 invariants per `src/pit_backtest/data/contracts.py` Protocol surface); SF1-vs-SEP authoritative-source resolution; data freshness check at startup per ADR 0003 decision 16; `IsMemberAt(t)` demo with 2010 vs current S&P 500 count + survivor count + CAGR delta consistent with published studies (ADR 0002 M3 acceptance criterion 1).
- **NEXT_BAR_OPEN deferred-fill mechanism (council pending)** per ADR 0009 lock #4: spawn 4-member council (Realist / Quant / Builder / Growth) + Verifier to settle the Order plumbing vs deferred-orders queue trade-off.
- **Distinct policy-time vs matcher-time MarketStateLookup snapshots (council pending)** per ADR 0011 lock #6: spawn 4-member council to settle the BarLoop ctor surface (two cost models) vs the shifted-dt lookup approach for tolerance enforcement reactivation.

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
