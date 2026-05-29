# ADR 0005: M2 cost-realism implementation plan

Status: Accepted.
Date: 2026-05-28.
Authors: Sam Doane (with Plan + skeptical-reviewer pass per the project rule on substantial code).

## Context

ADR 0002 commits M2 (weeks 3-4 of the ten-week timeline) to "cost realism with sensitivity bands" and locks six acceptance criteria. M1 day 3 established the convention that any code module > 200 LOC must go through a Plan + skeptical-reviewer pass with a numbered ADR before code lands (ADR 0004 captured the most consequential M1 reframing).

This ADR is the M2 design pass. The Plan subagent produced a ~3000-word file-by-file implementation plan; the skeptical-reviewer subagent (senior multi-strat quant persona) produced a critique with 5 Critical/High findings, 3 ADR-naming recommendations, and a 4-PR split recommendation. This ADR captures the plan, the reviewer's response, my response, and the final locked decisions. ADRs 0006 (cost-model acceptance criterion revision) and 0007 (`ImpactedPriceSource` policy on Signal `pit_view`) will be written and ship with PR A and PR B respectively per the split.

## The plan (summarized)

The Plan agent's deliverable, condensed:

### Math (section A)

Almgren et al. 2005 (Risk magazine v18, Section 3) gives total round-trip cost as a fraction of arrival price:

```
tcost_fraction = (1/2) * gamma * sigma_D * (Q/V_D) * (Theta/V_D)^(1/4)         # permanent
               + eta * sigma_D * |Q / (V_D * T)|^beta                            # temporary
tcost_bps = tcost_fraction * 10_000
```

Variables: `sigma_D` daily return vol (fraction; e.g., 0.012 for SPY); `Q` order size in shares; `V_D` average daily volume in shares; `Theta` shares outstanding (the float-adjusted turnover proxy that the literature names the "Theta/V" factor); `T` execution horizon as fraction of day (1.0 for single-bar fills); calibration constants `eta=0.142`, `beta=0.6`, `gamma=0.314` default; `beta=0.5` under `--impact-model=bouchaud`.

`CostBreakdown` decomposes into `slippage_bps`, `temporary_impact_bps`, `permanent_impact_bps`, `permanent_impact_per_share` (signed dollar amount fed to `ImpactedPriceSource`), and `commission` dollars.

### Architecture (sections B-G)

- **`ImpactedPriceSource`** decorator wraps the raw `SharadarDataSource`; maintains a per-asset `dict[AssetId, Decimal]` of cumulative permanent impact; intercepts all price reads (volume bypassed); per-bar one-fill-per-(asset, dt) constraint to remove in-bar compounding ambiguity.
- **`SquareRootImpactMatchingEngine`** alongside `CloseFillMatchingEngine`; takes (clock, cost_model, commission, impacted_source); handles `FillPriceModel.OPEN/CLOSE/VWAP/ARRIVAL/NEXT_BAR_OPEN`; per-model arrival-price selection.
- **Pre-trade vs fill-cost** share a `_impact_bps` helper to guarantee consistency; tolerance contract documented in new `docs/methodology/cost_model_tolerance.md`.
- **Commission**: `PerShareCommission(rate_per_share)` and `BasisPointsCommission(rate_bps)`; the /100 regression test asserts known-trade values.
- **Sensitivity-band runner**: `Runner.run_sweep` produces a new `SensitivityBand` attrs container (`parameter_name`, `parameter_values`, `per_parameter_equity`, `confidence_tier=SWEEP_SELECTED_NO_CORRECTION`); not a `BacktestPathDistribution` (parameter uncertainty is not statistical uncertainty).
- **`--impact-model=bouchaud`** wired via `BacktestConfig.impact_model`; selects `beta=0.5` family.

### Performance (section H)

GitHub Actions `ubuntu-latest` 4 vCPU / 16 GB baseline; new `src/pit_backtest/bench/spy_20y.py` runs the SPY 20y backtest 3x with median+IQR; `bench/compare.py` exits 1 on >10% regression; `.bench-baseline.json` committed and bumped via explicit PR; `BarLoop.timing_breakdown()` instrumentation gated by `enable_timing` flag.

### M1 1e-10 invariant preservation (section I)

Three layers proposed:
1. Existing `test_constant_weight_demo.py` against the M1 `CloseFillMatchingEngine` unchanged.
2. New `test_constant_weight_demo_m2_zero_cost.py`: `SquareRootImpactMatchingEngine` + `NoImpact` + zero commission must equal the M1 baseline to 1e-10.
3. New `test_constant_weight_demo_m2_with_costs.py`: engine vs a new `reference_constant_weight_pnl_with_costs` function to 1e-10.

### Files (~3000 LOC total)

11 files modified, 18+ files created. Estimated 3000 LOC across production code, tests, CI workflow, methodology doc, and the example study script.

## Skeptical reviewer's response

The senior-quant reviewer surfaced the following. The findings below are condensed from the reviewer's verbatim output; the critical math and architectural points are preserved.

### Top 5 things the plan gets RIGHT

1. **Layer 2 of the 1e-10 invariant** (NoImpact + zero commission == M1) is the right test. Catches the entire "I refactored the matcher and silently broke arithmetic" failure class at zero ongoing maintenance.
2. **`NoImpact` gated on `unsuitable_for_deployment=True` at the matcher boundary** (not just cost-model boundary) prevents the "ran prod through dev backtester with no-impact still on" production incident class.
3. **`CostBreakdown` decomposition** matches the existing `Fill` schema at `src/pit_backtest/execution/orders.py:67-70`. Conflating slippage and impact in storage is the root cause of debugging despair.
4. **`SensitivityBand` as a dedicated container with `confidence_tier=SWEEP_SELECTED_NO_CORRECTION`, explicitly NOT a `BacktestPathDistribution`.** Treating parameter uncertainty as statistical uncertainty is a primary source of industry overfitting fraud; the plan avoids it.
5. **Tolerance contract as a separate methodology doc** rather than a magic number buried in a test is the right architectural commitment.

### Top 5 things the plan gets WRONG or MISSES

#### 1. [Critical] Almgren formula misquoted in two ways

The headline formula in section A is dimensionally inconsistent: `tcost_bps = (1/2) * gamma * sigma_D * ...` where all the factors are fractions (gamma dimensionless ~0.314; sigma_D fraction ~0.012; participation rate fraction ~0.005; Theta/V dimensionless) yields a fraction, not bps. The plan later does multiply by 10_000 in the decomposition lines, but the inconsistency at the top will propagate. Fix: rewrite the headline as `tcost_fraction = ...` then `tcost_bps = tcost_fraction * 10_000`, OR multiply explicitly in the headline. Plus: the `(Theta/V_D)^(1/4)` factor is specifically from Almgren, Thum, Hauptmann, Li 2005 (Risk magazine v18, Section 3); cite the equation in the methodology doc.

#### 2. [Critical] Synthetic VWAP = (O+H+L+C)/4 is wrong and should be refused

`(O+H+L+C)/4` is the "typical price" / "OHLC4", not VWAP. Real VWAP is volume-weighted and requires intraday tick data. The two can disagree by 30-80 bps on a high-volatility bar. A user who writes `fill_price_model=VWAP` because their PM mandated VWAP gets a fill with nothing to do with real VWAP. Fix: refuse to fill at VWAP without real intraday data (raise `UnsupportedFillPriceModelError`, matching the `CloseFillMatchingEngine` precedent at `src/pit_backtest/execution/matching.py:82-87`). Optionally add a separate `FillPriceModel.TYPICAL_PRICE` enum that documents what it computes.

#### 3. [Critical] Performance budget CI cannot run on Sharadar data

Per `docs/methodology/dataset_versioning.md:138`: "CI does not have access to the Sharadar API." The plan's CI workflow runs `bench/spy_20y.py` on every push but is silent on data source. Three options: (1) synthetic data (workable but must be labeled); (2) committed mini-snapshot (Sharadar TOS likely forbids redistribution); (3) Git LFS / cloud storage (v1.1 backlog). Fix: commit to option 1, label explicitly.

#### 4. [High] The 5 bps + 1 day sigma_D tolerance is a non-test

SPY's daily vol is ~0.012 = 120 bps. Tolerance = 5 + 120 = 125 bps. An 8 bps pre-trade estimate vs 50 bps fill passes; vs 80 bps also passes. Not a tolerance, a permission slip. Fix: `0.5 bps + 0.1 * |delta_mid|_bps` where `delta_mid` is the actual estimate-time vs fill-time mid difference. For SPY this typically lands around 3-5 bps in practice.

#### 5. [High] /100 regression test exercises the wrong code path

The historical backtrader bug is `commission_per_share=0.005` silently treated as `0.005/100 = 0.00005`, a `PerShareCommission` bug. The plan's test is on `BasisPointsCommission`. Fix: the killer assertion goes on `PerShareCommission`:

```python
def test_per_share_commission_no_silent_rescale():
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    cost = commission.compute(quantity=Decimal("1000"), fill_price=Decimal("50"))
    assert cost == Decimal("5.00")  # 1000 sh * $0.005 = $5.00
    assert not (Decimal("0.04") <= cost <= Decimal("0.06")), (
        "PerShareCommission silently divided by 100; backtrader bug class"
    )
```

Keep the `BasisPointsCommission` test as a separate, additional test. Both classes need /100 protection.

### Gotchas before first line of code

- **One-fill-per-(asset, dt)** is correct for daily bars; enforce at the matcher with a typed error. Intraday slicing is a v1.1 / outside-scope concern.
- **`ImpactedPriceSource` on Signal `pit_view` should default OFF.** The plan's own risk section flags the feedback loop on mean-reversion strategies; default-ON has no decision-record justification. Split into `apply_permanent_impact_to_signal_pit_view: bool = False` and `apply_permanent_impact_to_valuation: bool = True`. Requires its own ADR.
- **Bouchaud flag naming**: cite the research note's distinction between Almgren 2005 3/5 exponent and Bouchaud-Lillo-Farmer 1/2 exponent in the docstring. Recommend alias `--impact-model=square-root-law` for clarity.
- **FIM 2018 sanity check**: the plan's "use formula-derived band, not FIM number" is right empirically (SPY at $1M is sub-scale for FIM's institutional calibration; formula yields 1-5 bps not 10) but deviates from ADR 0002 acceptance criterion 1. Fix: write a new ADR (call it 0006) that explicitly supersedes ADR 0002 criterion 1's FIM cross-check, do not silently drop.
- **Trust boundary #12** (user custom cost model with non-injected RNG) is forward-compatibility theater. Almgren is deterministic. No shipping cost model needs randomness. Drop from M2; add in v1.1 if a stochastic cost model lands.
- **Layer 3 of 1e-10 preservation** (engine vs `reference_constant_weight_pnl_with_costs`) is "writing the cost arithmetic twice and asserting equality", which tests that you copied the code correctly. Drop Layer 3. Replace with a golden-file fixture: 3-bar synthetic fixture with specific cost parameters; expected fill prices and commissions committed as JSON; test asserts the engine matches the fixture. This tests the formula, the bps conversion, the commission rounding, and the cumulative-impact register update without duplicating the implementation.

### ADR-naming recommendation

Three ADRs, not one:

- **ADR 0005 (this ADR)**: M2 cost-model implementation plan. File layout, four-PR split, matcher drop-in surface, sensitivity-band runner architecture, bench design.
- **ADR 0006**: Cost-model acceptance criterion revision. Supersedes ADR 0002 acceptance criterion 1's FIM 2018 ~10 bps sanity check; documents the formula-derived A/B band; re-anchors the sanity check on `eta=0.142` central estimate falling inside the eta=0.05/0.30 band.
- **ADR 0007**: `ImpactedPriceSource` policy on Signal `pit_view`. Defaults to OFF; v1.1 opt-in path; rationale cites the mean-reversion feedback loop.

### Splitting recommendation

Four PRs, in order:

1. **PR A: cost-model math + Commission + tests** (~600 LOC). `execution/cost/*` + `/100` regression tests (PerShareCommission focus) + golden-file fixture + `docs/methodology/cost_model_tolerance.md`. Ships ADR 0006.
2. **PR B: ImpactedPriceSource + SquareRootImpactMatchingEngine** (~600 LOC). The decorator + matcher + `BarLoop` wiring + new `FillPriceModel` raise paths for VWAP + Layer 2 1e-10 invariant. Ships ADR 0007.
3. **PR C: sensitivity-band runner + analytics** (~500 LOC). `engine/runner.py` + `analytics/sensitivity.py` + multiproc pool.
4. **PR D: perf budget CI + bench harness** (~400 LOC). `bench/spy_20y.py` on synthetic data + `bench/compare.py` + `.github/workflows/perf-budget.yml` + `.bench-baseline.json`.

Each PR is under 700 LOC. Each PR has a single acceptance-criterion focus. Each PR gets its own reviewer pass per the M1 day 3 precedent. **One 3000-LOC PR violates the precedent the project set in ADR 0004.** Four-PR split adds ~1.5 days of overhead and removes a week of debugging.

## My response to the reviewer

The reviewer is right on every Critical and High finding. The Almgren unit error (C1) is the kind of mistake that fails the headline acceptance criterion in a way that is hard to localize after the fact; catching it before the first line of code is the entire point of running this pass. The VWAP fall-through (C2) walks back from the `CloseFillMatchingEngine` discipline I just shipped in M1; the reviewer correctly held me to my own precedent. The CI data source (C3) is a real "the plan does not work as described" gap. The 125 bps tolerance (C4) and the misdirected /100 test (C5) both miss their stated targets.

The structural recommendations also land: three ADRs (one per orthogonal decision) is cleaner than one bag-of-decisions; four PRs (each with its own reviewer pass) is the only honest way to apply the M1 day 3 standard.

### Accepted

1. **Almgren formula labeled `tcost_fraction` at the headline, `tcost_bps = tcost_fraction * 10_000` derived explicitly.** The methodology doc cites Almgren, Thum, Hauptmann, Li 2005 (Risk magazine v18 (July), pp. 57-62, Section 3) and prints the equation with units annotated per variable. The `(Theta/V_D)^(1/4)` factor's float-adjustment origin is called out so future contributors do not silently drop it.
2. **`SquareRootImpactMatchingEngine` raises `UnsupportedFillPriceModelError` for `FillPriceModel.VWAP`** with a message pointing at the v1.1 intraday-data adapter. No silent OHLC4 substitution. If a user wants the (O+H+L+C)/4 proxy for any reason, they get it via a new `FillPriceModel.TYPICAL_PRICE` enum value that documents what it computes (not VWAP).
3. **`bench/spy_20y.py` runs on synthetic data, labeled explicitly.** Module header carries the comment `# Synthetic data per dataset_versioning.md CI gap; real-data perf number is captured in the PR description per the local-run gate.` `.bench-baseline.json` carries the synthetic-data figure; local runs against real Sharadar produce a separate `bench-local.json` whose value is the canonical "60-second budget on the dev laptop" number.
4. **Pre-trade vs fill-cost tolerance rewritten** to `0.5 bps + 0.1 * |delta_mid|_bps`, where `delta_mid` is the actual estimate-time vs fill-time mid difference for the asset in question. The 0.5 bps commission-rounding base is unchanged. The new formula lands at ~3-5 bps for typical SPY rebalances and ~10-15 bps for high-vol single-bar moves.
5. **`/100` regression test on `PerShareCommission` specifically.** `BasisPointsCommission` gets its own /100 test as well; the killer assertion lives on both classes. The meta-test (asserting a faulty implementation would fall in the rejected band) ships for both.
6. **`ImpactedPriceSource` on Signal `pit_view` defaults OFF.** `BacktestConfig.apply_permanent_impact_to_signal_pit_view: bool = False` and `apply_permanent_impact_to_valuation: bool = True` ship in PR B. ADR 0007 captures the rationale.
7. **Trust boundary #12 dropped.** The 11-item list in `docs/methodology/determinism.md` stays at 11 for M2; v1.1 adds the cost-model-RNG item when a stochastic cost model needs it.
8. **Layer 3 of the 1e-10 invariant replaced with a golden-file fixture.** PR B ships `tests/integration/fixtures/cost_3bar_golden.json` with expected fill prices, commissions, and impact register state for a synthetic 3-bar fixture under specific cost parameters. The test asserts the engine matches the fixture. No `reference_constant_weight_pnl_with_costs` written.
9. **Bouchaud flag aliased.** `--impact-model=bouchaud` continues to work; `--impact-model=square-root-law` is the canonical name; both produce `beta=0.5`. The docstring cites the Bouchaud-Lillo-Farmer 1/2 exponent vs the Almgren 2005 3/5 exponent distinction per the research note.
10. **Four-PR split with own reviewer pass per PR.** This ADR is the umbrella; PRs A through D execute under it. ADR 0006 ships with PR A; ADR 0007 with PR B.
11. **ADRs 0006 and 0007** as new ADRs ship with PR A and PR B respectively. Their summaries are in this ADR's "Final decisions" section; the full text lands when the corresponding PR opens.
12. **One-fill-per-(asset, dt) enforced at the matcher** with `MultipleFillsPerBarError` typed error. Documented as a daily-bar v1 constraint; intraday slicing is v1.1.
13. **Pre-computed `sigma_D`/`V_D`/`Theta` Polars frames at `Backtest.__init__`.** The cost-model `estimate` and `compute` methods read by O(1) dict lookup, never recompute the rolling window.
14. **`compute_rolling_adv` and `compute_rolling_daily_vol`** implemented in pure NumPy via explicit `np.convolve` rather than Polars `rolling_mean`, to insulate the cost-model determinism from Polars version bumps.

### Contested

None on the substance. The only push-back is on emphasis:

The reviewer's "Layer 3 tests nothing" is technically correct, but a golden-file fixture that captures the same arithmetic still requires the implementer to compute the expected fill prices by hand once. That hand computation is itself a "second implementation" that has to be maintained when the cost model changes. I am accepting the recommendation because the golden fixture is at least an out-of-band artifact (a JSON file) that a reviewer can inspect independently, whereas duplicate code in `reference.py` invites the reviewer to suspect copy-paste. But the maintenance cost is real and the M5 PR will have to revisit if the cost model grows complex enough that the fixture becomes brittle.

### Final locked decisions

These decisions are binding on PRs A, B, C, D and on ADRs 0006 and 0007. Revisiting any requires a superseding ADR.

1. **Almgren formula.** `tcost_fraction = (1/2) * gamma * sigma_D * (Q/V_D) * (Theta/V_D)^(1/4) + eta * sigma_D * |Q/(V_D*T)|^beta`; `tcost_bps = tcost_fraction * 10_000`. `eta=0.142`, `beta=0.6`, `gamma=0.314` default per Almgren et al. 2005 Risk magazine v18 Section 3. Methodology doc cites the equation with annotated units.
2. **Bouchaud flag.** `beta=0.5` under `--impact-model=bouchaud` (or alias `--impact-model=square-root-law`). Docstring cites Bouchaud-Lillo-Farmer vs Almgren 2005 exponent distinction.
3. **`epsilon_bps` default = 0.** No spread proxy; cost model is impact-only. Methodology doc names the v1.1 intraday-spread integration as the path to nonzero default epsilon.
4. **VWAP rejected.** `SquareRootImpactMatchingEngine` raises `UnsupportedFillPriceModelError` for `FillPriceModel.VWAP`. Optional new `FillPriceModel.TYPICAL_PRICE` enum for the (O+H+L+C)/4 proxy, clearly distinguished.
5. **`ImpactedPriceSource`.** Decorator wraps `SharadarDataSource`; per-asset cumulative impact dict; never iterated for arithmetic (sorted membership only); reset per `Backtest.__init__`. Intercepts price reads only; volume bypassed.
6. **`ImpactedPriceSource` policy.** Applied to valuation by default. NOT applied to Signal `pit_view` by default (`apply_permanent_impact_to_signal_pit_view=False`). ADR 0007 captures the rationale.
7. **One-fill-per-(asset, dt) enforced** at matcher with typed `MultipleFillsPerBarError`. Documented v1 constraint; intraday slicing is v1.1.
8. **Cost-model arithmetic uses pre-computed Polars frames** for `sigma_D`/`V_D`/`Theta`. Rolling windows computed via pure NumPy (`np.convolve`) for Polars-version determinism.
9. **Pre-trade vs fill-cost tolerance.** `0.5 bps + 0.1 * |delta_mid|_bps`. Documented in `docs/methodology/cost_model_tolerance.md`.
10. **/100 regression test** on `PerShareCommission` AND `BasisPointsCommission`. Killer assertion + meta-test for both.
11. **CI bench data source.** Synthetic data; labeled explicitly in `bench/spy_20y.py` header. `.bench-baseline.json` carries synthetic figures; real-data perf number captured in PR descriptions.
12. **`BarLoop.timing_breakdown()`** added with per-step accumulator dict; gated by `enable_timing: bool = False` so production backtests do not pay the cost.
13. **M1 1e-10 invariant preservation.** Layer 1 (existing M1 test unchanged) + Layer 2 (zero-cost M2 matcher == M1 baseline to 1e-10). No Layer 3 (`reference_constant_weight_pnl_with_costs` is not written). Cost behavior verified via golden-file fixture under `tests/integration/fixtures/cost_3bar_golden.json`.
14. **Sensitivity band.** `SensitivityBand` attrs container; `confidence_tier=SWEEP_SELECTED_NO_CORRECTION`; not a `BacktestPathDistribution`.
15. **Trust boundary list** stays at 11 for M2. Item 12 (cost-model RNG) deferred to v1.1.
16. **Four-PR split with own reviewer pass per PR.**
    - PR A: cost-model math + Commission + golden fixture + `docs/methodology/cost_model_tolerance.md` + ADR 0006.
    - PR B: `ImpactedPriceSource` + `SquareRootImpactMatchingEngine` + `BarLoop` wiring + Layer 2 invariant + ADR 0007.
    - PR C: `Runner.run_sweep` + `analytics/sensitivity.py` + `examples/spy_cost_sensitivity.py`.
    - PR D: `bench/spy_20y.py` (synthetic) + `bench/compare.py` + `.github/workflows/perf-budget.yml` + `.bench-baseline.json` + baseline-bump procedure.
17. **ADR 0006 (queued for PR A)**: supersedes ADR 0002 acceptance criterion 1's FIM 2018 ~10 bps cross-check. Documents that the formula-derived `[eta=0.05, eta=0.30]` band is the gate; FIM 2018 is preserved as an upper-bound ceiling (`central cost < 50 bps annualized`) rather than a central-estimate target, because SPY at $1M notional is sub-scale for FIM's institutional sample.
18. **ADR 0007 (queued for PR B)**: `ImpactedPriceSource` policy on Signal `pit_view`. Defaults to OFF; v1.1 opt-in via `apply_permanent_impact_to_signal_pit_view=True`. Rationale: mean-reversion feedback loop (research note open question).

## Status

Accepted. PR A begins next session against this ADR. ADRs 0006 and 0007 follow with their respective PRs. The M2 implementation must conform to the 18 locked decisions above; deviations require a superseding ADR.
