# ADR 0011: Tolerance contract dormancy at M2

Status: Accepted.
Date: 2026-05-29.
Authors: Sam Doane (via 4-member council + verifier pass per session_rules.md rule 1).

## Context

ADR 0009 lock #8 (M2 PR B) deferred active enforcement of the cost-model tolerance contract `tolerance_bps = 0.5 + 0.1 * |delta_mid_bps|` from `docs/methodology/cost_model_tolerance.md` to "PR C" with the explicit acknowledgement that `SquareRootImpactCostModel.estimate(...)` and `compute(fill_state)` produce bit-identical outputs under shared-instance dispatch. ADR 0010 (M2 PR C1) further deferred the active enforcement to PR C2 because the Plan-reviewer surfaced 3 Critical structural findings on the original PR C tolerance plan:

- **C1 (dead code)**: under the M2 default wiring locked at `tests/integration/test_cost_estimator_wired_to_policy.py:141` (`assert bar_loop._cost_estimator is cost_model`), `cost_model.estimate(asset_id, |qty|, direction, dt)` and `cost_model.compute(fill_state)` both call `MarketStateLookup.get((asset_id, _et_date(dt)))` and run identical `_almgren_terms` evaluation against the same `MarketStateRow`. Outputs are bit-identical; the proposed `CostEstimateVsFillMismatchError` cannot fire.
- **C2 (wrong mid)**: the proposed policy population of `mid_at_estimate` reads today's close from `price_lookup(asset_id, dt)`. The methodology doc at line 27 says `mid_at_estimate = mid at the start of the bar (typically prior close)`.
- **C3 (cost model is mid-insensitive)**: `_almgren_terms` at `src/pit_backtest/execution/cost/impact.py:162-193` reads `(eta, beta, gamma, sigma_D, V_D, Theta, Q, T)`. Mid is structurally absent. The tolerance check tests whether `abs(0.0) <= 0.5 + 0.1 * |something|`, which is identically true under any cost model that does not consume mid.

Sam confirmed splitting PR C: PR C1 (sensitivity-band runner) shipped via ADR 0010 and PR #20; PR C2 (active tolerance enforcement) is the current PR, blocked on an architectural decision between three options:

1. **Two cost-model instances** at the BarLoop ctor (`policy_cost_model` + `matcher_cost_model`, default same).
2. **Shifted-dt `PolicyTimeMarketStateLookup`** subclass that shifts `dt` by minus-one-trading-day inside `get()`.
3. **Dormant scaffold** (Order grows fields, matcher implements check, ADR documents dormancy).

Plus the implicit fourth option **KILL** (drop active enforcement; methodology doc names tolerance as documentation-only).

Per session_rules.md rule 1 ("For decisions, not just code, spawn a 3-4 member parallel council plus a verifier"), Sam directed the architectural decision to a 4-member council (Realist / Quant / Builder / Growth) plus a Verifier. The council vote was 2 KILL (Realist, Builder) vs 2 DORMANT SCAFFOLD with caveats (Quant, Growth). The verifier synthesized to a HYBRID that catches what 3 of 4 council members missed.

## Council vote (condensed)

### Realist: KILL

> "Three of my last incidents started exactly here: somebody bolted on a 'tolerance gate' that felt like enforcement but wasn't, then six months later a real bug slid through because everyone assumed the gate was live. ... None of [Options 1/2/3] make the contract honest. The cost model is mid-insensitive at M2. Document that, demote the tolerance contract to non-normative until the cost model gets a spread or mid-dependent term in v1.1, and stop pretending."

Compromise: "Add a NotImplementedError stub on `Order.estimate_bps_at_submit` so the future ADR has to deliberately turn it on."

### Quant: 3 (Dormant scaffold) with sharp caveats

> "The locked formula `tolerance_bps = 0.5 + 0.1 * |delta_mid_bps|` is a category error against the cost model it is meant to police. ... Almgren-Thum-Hauptmann-Li 2005 Section 3 derives the square-root law in fractional return space precisely so that the result is dimensionless and scale-invariant in price; that is the whole point of expressing impact in bps. Calibrating a tolerance to mid drift is therefore a contradiction in terms at M2. ... The honest move is to make the scaffold dormant under M2, document the contract as falsifiable-but-not-fired, and defer activation to a milestone where the cost model is genuinely mid-sensitive."

Activation gate proposed: M3 epsilon_bps > 0 OR M4 BLF propagator wiring.

### Builder: KILL

> "Three options, and all three are answers to a question whose premise C3 already invalidated. Option 1 buys two parallel cost-model instances but both read the same `MarketStateLookup` dict keyed by `(asset_id, date)`, so unless M3 also forks the lookup the LHS stays zero. Option 2's shifted-dt subclass is the worst kind of spooky action. Option 3 is precisely the 'live infrastructure presented as theater' pattern PR B's reviewer C1 already rejected."

### Growth: 3 (Dormant scaffold) with strong portfolio framing

> "Option 3 ships the contract in code, populates it from the policy, and pairs it with ADR 0011 that says in plain English: 'the cost model at M2 is mid-insensitive, so any tolerance check we wire today is identically zero on its LHS; we are shipping the scaffold because M3 introduces a mid-sensitive impact term and we want the integration surface auditable now, not retrofitted later.' That paragraph IS the portfolio piece."

Critical caveat (binding): "If the dormancy is not surfaced in the FIRST screen of README and the FIRST line of the matcher's docstring, dormant-scaffold becomes indistinguishable from sloppy YAGNI violation. ... If Sam cannot commit to making ADR 0011 visible from the README scorecard line, downgrade to kill."

## Verifier's synthesis (HYBRID)

The Verifier read all four council outputs, verified the three Critical findings against source, and surfaced two items 3 of 4 missed:

### Verifier-corrected facts

- **C3 (mid-insensitive cost model): CONFIRMED** at `impact.py:184-193`. The Quant's dimensional argument is correct: the formula is in fractional-return space; mid does not enter.
- **C1 (shared-instance dispatch is dead): CONFIRMED** at `test_cost_estimator_wired_to_policy.py:141`.
- **C2 (mid_at_estimate = prior close per methodology, not today's close): PARTIALLY VERIFIED**. The doc (line 27) says "typically prior close"; the first-bar fallback (lines 70-82) is `open`, not close. The Plan-reviewer's framing was slightly imprecise but the underlying critique stands: there is no policy-time snapshot today.
- **The Quant's "M3 epsilon_bps > 0 activation gate" is WRONG ON PHYSICS**. `epsilon_bps` controls the slippage term, not the impact term. Adding `epsilon_bps > 0` adds a constant bps slippage; it does NOT inject mid into Almgren. The correct activation gate is BLF propagator OR an explicit spread-sensitive impact term in `_almgren_terms`' signature, NOT epsilon_bps.
- **The Builder's "180 LOC reactivation cost" is high**. `test_cost_estimate_vs_fill_tolerance.py` (172 lines, PR B) already ships the symbolic exercise of the formula with two cost-model instances at different sigma_D values. The dormant-scaffold Option 3 is not 180 LOC of new infrastructure; it's ~30 LOC of test additions plus an ADR.

### Verifier's recommended option: HYBRID = "docs-and-tests-only scaffold + Realist's NotImplementedError tripwire"

The Realist + Builder are right that adding `Order` fields + matcher checks + policy population is theater. The Quant + Growth are right that a portfolio artifact saying "I considered this and chose to defer with a documented contract" is more valuable than silence. The synthesis: ship the dormancy as DOCS + TESTS + TRIPWIRE only. No `Order.estimate_bps_at_submit` Decimal field, no matcher check method, no `CostEstimateVsFillMismatchError` class, no BarLoop ctor surface change, no `TargetPositions.per_asset_estimate_bps` parallel dicts, no `EqualWeightMonthlyRebalancePolicy` population. The existing `test_cost_estimate_vs_fill_tolerance.py` already does the symbolic exercise; ADR 0011 + a single `NotImplementedError` stub property on `Order.estimate_bps_at_submit` + a README design-pillar line is the entire deliverable.

### Verifier's binding requirements for ADR 0011

1. **Scope clamp**: PR C2 ships ADR 0011 + README line + `@pytest.mark.dormant_until_m3` skip test + one `NotImplementedError("ADR 0011: dormant until M3 PR with policy-time MarketStateLookup snapshot")` stub property on `Order`. ZERO changes to BarLoop ctor, `TargetPositions`, matcher dispatch, or exception hierarchy.
2. **Correct activation gate**: Reactivation requires distinct policy-time vs matcher-time `MarketStateLookup` snapshots (i.e., day-shifted sigma_D / V_D). NOT `epsilon_bps > 0`. ADR 0011 must explicitly reject the `epsilon_bps` activation framing as a category error against the Almgren formula.
3. **Dimensional honesty paragraph**: ADR 0011 states that the locked formula `0.5 + 0.1 * |delta_mid_bps|` is a tolerance specification calibrated against a future mid-sensitive impact term and is structurally a no-op against the M2 Almgren-2005 formula in fractional-return space.
4. **README precondition (binding, not optional)**: A single line under design pillars: "M2 enforces tolerance contracts in docs and tests only; live matcher enforcement is dormant by design; see ADR 0011." If this line is not in the same PR, the dormancy collapses to kill per Growth's caveat.
5. **Falsifiability target rewrite**: The acceptance test asserts "dormancy contract holds" (NotImplementedError raised, README contains the line, ADR 0011 exists) NOT "formula evaluates correctly" (already covered by `test_cost_estimate_vs_fill_tolerance.py`).

## Author's response

The verifier is correct on every count. The Quant's "epsilon_bps > 0" activation gate is wrong on physics; the verifier caught what I missed. The Realist + Builder's "ship the surface and ignore the math" critique is the correct frame; the verifier's hybrid converts it into a positive deliverable rather than a no-op. The Growth's binding README precondition is satisfiable in one line; the verifier's hybrid takes it as load-bearing.

### Accepted

All five verifier binding requirements are accepted in full.

### Contested

None. The verifier's synthesis is the correct read.

### Final locked decisions

These 7 decisions are binding on the PR C2 implementation. Revisiting any requires a superseding ADR.

1. **`Order.estimate_bps_at_submit` is a NotImplementedError stub property on the `attrs.frozen(slots=True)` class.** Reading the attribute raises `NotImplementedError("ADR 0011: dormant until M3 PR with policy-time MarketStateLookup snapshot")`. No Decimal field is added; no defaulted attrs.field; the property descriptor lives on the class and bypasses the slots-only field surface.
2. **`Order.mid_at_estimate` is NOT added.** The methodology doc's tolerance formula presupposes a mid the cost model does not consume; surfacing a Decimal field for an unused quantity would invite future contributors to populate it incorrectly.
3. **`CostEstimateVsFillMismatchError` is NOT added.** No matcher dispatch change. `MatchingError`, `UnsupportedFillPriceModelError`, `MultipleFillsPerBarError` hierarchy from PR B is unchanged.
4. **`SquareRootImpactMatchingEngine.submit(...)` is NOT extended.** No tolerance check call. The matcher's existing dispatch stays as ADR 0009 lock #4 specified.
5. **`TargetPositions`, `EqualWeightMonthlyRebalancePolicy`, `BarLoop.__init__` are NOT modified.** The existing identity-check at `tests/integration/test_cost_estimator_wired_to_policy.py:141` (`assert bar_loop._cost_estimator is cost_model`) is preserved verbatim; a comment is added pointing at ADR 0011 so a future contributor does not "fix" the assertion by deleting it.
6. **Activation gate is DISTINCT policy-time vs matcher-time `MarketStateLookup` snapshots**, NOT `epsilon_bps > 0`. The verifier-corrected gate prevents a future ADR from flipping dormancy on a milestone that does not actually change the cost model's mid-sensitivity. Sample wiring at the activation milestone: `policy_cost_model = SquareRootImpactCostModel(market_state=policy_time_lookup)` and `matcher_cost_model = SquareRootImpactCostModel(market_state=matcher_time_lookup)` where the two lookups are bound to day-shifted rolling-window stats.
7. **Acceptance contract is dormancy, not formula correctness.** `test_cost_estimate_vs_fill_tolerance.py` (172 lines, shipped in PR B) already exercises the formula symbolically. PR C2 ships `test_order_estimate_bps_at_submit_dormant_per_adr_0011` (or similar) asserting the stub property raises with the documented message, plus a marker-registered `@pytest.mark.dormant_until_m3` annotation that any future contributor must reckon with.

### Methodology doc updates

8. **`docs/methodology/cost_model_tolerance.md` gains a "Dormancy at M2 (per ADR 0011)" section** at the top of the file, before the "Goal" section. The section states:
   - The cost model at M2 is mid-insensitive per the Almgren-2005 formula's structural argument list `(sigma_D, V_D, Theta, Q, T)`.
   - The locked formula `tolerance_bps = 0.5 + 0.1 * |delta_mid_bps|` is a tolerance specification calibrated against a future mid-sensitive impact term.
   - The "What changed in PR B" subsection's "Active enforcement is M3 scope" statement is corrected: active enforcement requires distinct policy-time vs matcher-time `MarketStateLookup` snapshots, NOT `epsilon_bps > 0`.
9. **`README.md` design pillars block** (the "Cost realism with honest uncertainty bounds" bullet) gains the verifier's binding line: "At M2 the cost-tolerance enforcement contract is scaffolded but dormant by design; see ADR 0011."

## What this ADR does NOT do

- **Does NOT modify any production code path** beyond adding the `Order.estimate_bps_at_submit` stub property.
- **Does NOT add a Decimal field to Order, an exception class to matching.py, a method call in the matcher's submit, a return-field on TargetPositions, or a ctor kwarg on BarLoop.**
- **Does NOT modify the cost-model implementation.** `_almgren_terms` is unchanged.
- **Does NOT modify any existing test.** `test_cost_estimator_wired_to_policy.py:141`'s identity-check assertion is preserved (only a comment is added pointing at ADR 0011).
- **Does NOT reserve a `BacktestConfig` field** (ADR 0009 lock #4 precedent: do not reserve names on non-existent surfaces).
- **Does NOT name a date for M3 reactivation.** The reactivation gate is structural (distinct snapshots), not calendar-bound.

## Status

Accepted. PR C2 implements the 9 locked decisions above with the verifier's hybrid scope. The implementation is approximately 50 LOC of code (stub property + skip-marker test + pyproject.toml marker registration + comment at the identity-check test) plus the methodology doc + README + CHANGELOG + ROADMAP updates plus this ADR.
