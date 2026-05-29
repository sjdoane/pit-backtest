# ADR 0009: ImpactedPriceSource policy and M2 PR B matcher structural decisions

Status: Accepted.
Date: 2026-05-29.
Authors: Sam Doane (with Plan + skeptical-reviewer pass per the project rule on substantial code).

## Context

ADR 0005 step 18 reserved an ADR slot for the `ImpactedPriceSource` policy on Signal `pit_view`. That slot was originally numbered "ADR 0007" in ADR 0005's body; ADR 0007 was subsequently consumed by the Frazzini-Israel-Moskowitz 2018 ceiling decision (M2 PR A), and ADR 0008 by the SSGA nominal-year annualization and per-window tolerance dict (post-M1 hotfix). The next sequential number for the Signal-`pit_view` policy is therefore ADR 0009.

This ADR was written for M2 PR B per the four-PR M2 split locked in ADR 0005 step 16. PR B's scope is the `ImpactedPriceSource` decorator, the `SquareRootImpactMatchingEngine`, the BarLoop wiring of both, and the Layer 2 1e-10 invariant test. Per project rule 2 the author drafted a Plan with the plan-agent persona and received a skeptical-reviewer critique with the senior multi-strat-fund quant persona that produced ADRs 0001-0008. The reviewer pass surfaced 2 Critical and 4 High findings before any code was written; the Author's response below addresses each. The single most consequential reviewer finding was that the original Plan's `FillPriceModel.NEXT_BAR_OPEN` semantics (peek `next_open` from `trading_days_in_window[i+1]` on the same bar's submit call) would silently leak structural look-ahead in violation of ADR 0001 decision 4. The locked decision below defers NEXT_BAR_OPEN to M3 with a typed `UnsupportedFillPriceModelError("M3 deliverable")` rather than ship an unsafe peek path.

ADR 0009 captures the Signal-`pit_view` policy that was the originally reserved scope plus the architectural commitments the reviewer pass forced into M2 PR B. The two are bundled in this ADR because they share the same PR (M2 PR B) and the same operationalizing reviewer pass; splitting into two ADRs would have produced two near-empty doc-only PRs. ADR 0008 set the precedent for bundling multiple coupled decisions under one ADR (Decisions A, B, and C of nominal-year annualization, schedule removal, and per-window tolerances).

## The plan (summarized)

The Plan agent's deliverable, condensed:

### Original scope (ADR 0005 step 18 reservation)

`ImpactedPriceSource` decorator wraps the raw `SharadarDataSource`; maintains a per-asset `dict[AssetId, Decimal]` of signed cumulative permanent-impact dollars per share. The decorator exposes:

- `apply_permanent_impact(asset_id, per_share_signed)`: accumulates signed dollars per share; buy positive lifts subsequent reads; sell negative lowers them.
- `adjust_price(asset_id, raw_price) -> Decimal`: `raw_price + cumulative[asset_id]`.
- `cumulative_for(asset_id)`: read-only accessor.
- `reset()`: zeros the register at `Backtest.__init__`.

The plan recommended `apply_permanent_impact_to_valuation: bool = True` (ON by default; required for impact-aware portfolio NAV) and `apply_permanent_impact_to_signal_pit_view: bool = False` (OFF by default; v1.1 opt-in to avoid the mean-reversion feedback loop noted in ADR 0005's reviewer pass).

### Expanded scope from the M2 PR B reviewer pass

The reviewer pass on the file-by-file plan forced six additional architectural decisions:

1. NEXT_BAR_OPEN cannot be priced via a `next_open` peek; the matcher must implement a deferred-fill mechanism.
2. The tolerance contract `0.5 + 0.1 * |delta_mid_bps|` cannot be actively enforced by the matcher at M2 because `cost_model.estimate(...)` and `cost_model.compute(...)` resolve to the identical `MarketStateLookup` row and therefore produce bit-identical outputs.
3. The `MatchingEngine` Protocol must be extended with `on_bar_start(bar_dt) -> None` rather than rely on `hasattr` for the per-bar reset hook.
4. ADR 0009 cannot reserve a `BacktestConfig` field name because `BacktestConfig` does not exist as a wired surface at M2.
5. The BarLoop must wire the real `cost_model` into the policy's `cost_estimator` slot to satisfy ADR 0003 decision 4.
6. The Layer 2 1e-10 invariant test must be split into two named tests, one for matcher-vs-matcher and one for matcher-vs-reference, to honestly name the failure classes each catches.

The plan files and test names below are the post-reviewer plan that this ADR records.

### Files (condensed)

| File | Status | Change |
|---|---|---|
| `src/pit_backtest/data/sources/base.py` | rewrite | Replace `ImpactedPriceSource` stub with the standalone decorator |
| `src/pit_backtest/execution/matching.py` | extend | Add `SquareRootImpactMatchingEngine`, extend `MarketState`, add `MultipleFillsPerBarError`, add `MatchingError` shared base, extend `MatchingEngine` Protocol with `on_bar_start` |
| `src/pit_backtest/engine/bar_loop.py` | extend | Wire real OHLC, `prior_close`, `impacted_source`, `cost_estimator`, call `on_bar_start` |
| `tests/data/test_impacted_price_source.py` | new | Decorator unit tests |
| `tests/execution/test_matching.py` | extend | SquareRootImpactMatchingEngine fill-model coverage; one-fill-per-bar reset; sign-convention numeric pins |
| `tests/integration/test_constant_weight_demo_m2_zero_cost.py` | new | Two Layer 2 invariant tests with honest names |
| `tests/integration/test_cost_golden_fixture_e2e.py` | new | End-to-end fixture exercise through BarLoop + matcher |
| `tests/integration/test_permanent_impact_next_bar_mid_drops.py` | new | ADR 0002 M2 acceptance criterion 5 |
| `tests/integration/test_cost_estimate_vs_fill_tolerance.py` | new | Documentation-only formula exercise (matcher does NOT raise at M2) |
| `docs/decisions/0009-...md` | new | This ADR |
| `docs/methodology/cost_model_tolerance.md` | extend | Add "What changed in PR B" note + first-bar fallback semantics + "active enforcement is M3+ via Order.estimate_bps_at_submit" |
| `docs/methodology/determinism.md` | extend | Grow trust boundary table to 12 items (add ImpactedPriceSource) |
| `CHANGELOG.md` | extend | Unreleased entries |
| `docs/ROADMAP.md` | extend | M2 PR B shipped |
| `README.md` | extend | One-line M2 progress update |

Net new positive code is approximately 600 LOC across the production code and tests; the four-PR split's PR B remains under the 700 LOC per-PR ceiling.

## Skeptical reviewer's response

The senior multi-strat-fund quant reviewer (same persona that critiqued ADRs 0001-0008) returned a `restructure` verdict. The verbatim Critical and High findings:

### Top 5 RIGHT

1. One-fill-per-(asset, dt) enforced as a typed error matches ADR 0005 step 12; the `_et_date` key is the right dedup convention.
2. VWAP raises `UnsupportedFillPriceModelError` rather than silently substituting OHLC4 per ADR 0005 step 4, preserving the `CloseFillMatchingEngine` no-silent-substitution precedent.
3. `MarketState` extension chosen over a parallel `BarContext` companion type avoids the cross-cutting plumbing churn a second context object would create.
4. PR B keeps `SensitivityBand`, `Runner.run_sweep`, perf-budget CI, and `--impact-model=bouchaud` CLI out of scope per the four-PR split.
5. `ImpactedPriceSource.adjust_price` is the seam at v1 rather than a `get_price` override; volume bypass and the explicit `NotImplementedError("M3 deliverable")` on `get_price` keep PR B from tangling with the per-row PitDataSource protocol that `SharadarDataSource` has not implemented yet.

### [Critical] The tolerance-contract `CostEstimateVsFillMismatchError` enforcement is dead code

The reviewer walked through `src/pit_backtest/execution/cost/impact.py:231-262` (`estimate(...)`) and `src/pit_backtest/execution/cost/impact.py:264-300` (`compute(fill_state)`) and demonstrated that both calls resolve to the identical `MarketStateLookup` row and the identical `_almgren_terms(...)` evaluation when `_et_date(market_state.dt) == _et_date(fill_state.dt)` and `abs(order.quantity) == abs(fill_state.shares)`. The matcher's `_almgren_terms` evaluation is therefore bit-identical between `estimate` and `compute`; `abs(estimate - breakdown.total_bps)` equals exactly 0.0 for every (asset, dt) pair the matcher sees. The proposed "active enforcement" of `CostEstimateVsFillMismatchError` cannot fire and is presented as live infrastructure when it is theater. The methodology doc's tolerance is meaningful only between the policy's cached pre-trade estimate (at the START of the bar) and the matcher's fill-time compute (at the END of the bar); active enforcement at M2 requires `Order.estimate_bps_at_submit` plumbing that is out of scope for PR B.

### [Critical] `FillPriceModel.NEXT_BAR_OPEN` resolved via `trading_days_in_window[i+1]` peek violates ADR 0001 decision 4

The reviewer walked through the plan's BarLoop sequence: compute `next_open = bar_at[(ticker, trading_days_in_window[i+1])][0]`, construct `MarketState` with `next_open=...`, the matcher's step 3 resolves `arrival = market_state.next_open`, the resulting `Fill.dt = market_state.dt` (the CURRENT bar's dt), and the BarLoop's snapshot/MTM at step 7 re-marks the position at the CURRENT bar's close. This double-counts the bar-N to bar-N+1 return into the M2 P&L silently. The reviewer recommended either implementing a deferred-fill mechanism (matcher returns `[]` on bar N, fill materializes on bar N+1 via `on_bar_start` flushing a `_pending_next_bar_open` queue) OR deferring NEXT_BAR_OPEN to M3.

### [High] `hasattr(matching_engine, "on_bar_start")` is silent on typos and contradicts ADR 0003's mypy-strict Protocol discipline

A future user who typos `on_bar_started` satisfies the `MatchingEngine` Protocol under mypy strict (because the Protocol does not declare the method), the BarLoop's `hasattr` returns False, the matcher's per-bar reset never fires, the one-fill-per-(asset, dt) constraint silently bleeds across bars, and the bug surfaces in a 20-year M5 backtest as a quiet over-trading anomaly that no test reproduces. The Protocol is the contract.

### [High] ADR 0009 reserves `apply_permanent_impact_to_signal_pit_view` on a non-existent `BacktestConfig` surface

A Grep for `class BacktestConfig` returns the empty Pydantic `BacktestConfig` in `src/pit_backtest/cli/config.py` which has no field for the signal pit_view policy; that field is M3 work. Reserving a name on a non-existent surface produces the "half-implemented placeholder" the M1 day-3 reviewer rejected. ADR 0009 should document only what code it controls (the BarLoop ctor args at M2) and explicitly defer the `BacktestConfig` field to M3's BacktestConfig ADR.

### [High] No wiring of real `cost_model` into the policy's `cost_estimator` slot contradicts ADR 0003 decision 4

`src/pit_backtest/engine/bar_loop.py:71-86` defines `_NoopCostEstimator` that always returns `Decimal("0")`; the BarLoop passes it to `Policy.target_positions(cost_estimator=...)` at line 211. ADR 0003 decision 4 commits PreTradeCostEstimator-into-policy as the structural surface that lets policies opt out of trades whose cost exceeds the expected alpha. If PR B ships the matcher but not the policy wiring, the policy continues to commit to trades on the assumption that costs are zero and the matcher computes real costs against a policy decision that ignored them. ADR 0003 decision 4 is silently violated. The four-PR split's PR B is the natural home for the wiring.

### [High] First-bar `mid_at_estimate` ambiguity in the tolerance formula has no documented semantics

When `prior_close is None` (first bar of the backtest, first bar of any restart), the plan falls back to `mid_at_estimate = open` which makes `delta_mid_bps = (close - open) / (2 * open) * 10000`. For SPY at $500 with a 50 bp open-to-close move, this is 25 bps; tolerance widens to 3.0 bps. The methodology doc's "typically prior close" phrasing is silent on the first-bar fallback; a reviewer looking at a tolerance failure on bar 1 cannot tell whether the failure is real or a documented first-bar artifact.

### [Medium] Plan section 2 step 6's `signed_perm` builds permanent impact from `arrival` not `fill_price`

`signed_perm = float(breakdown.permanent_impact_bps) / 10_000.0 * float(arrival) * sign(qty)` is correct for OPEN and CLOSE (where arrival equals open or close) and slightly biased for ARRIVAL (where arrival is prior_close, not today's open). Using `fill_price` (the realized post-temporary-impact price) as the per-share notional basis keeps the magnitude consistent across all fill-price models.

### [Medium] Sign-convention tests should pin numeric examples

The plan lists `test_square_root_matcher_buy_lifts_fill_price_above_arrival` and `test_square_root_matcher_sell_lowers_fill_price_below_arrival` but does not pin numeric values. A future refactor that, for instance, makes the cost model directionally asymmetric (a v1.1 spread proxy with `epsilon_bps > 0`) would silently break the sell case if the test only asserts "sell fill_price < arrival" rather than a numeric value.

### [Medium] MarketState construction sites need an audit + keyword-only lint test

`src/pit_backtest/engine/bar_loop.py:235-243` and `tests/execution/test_matching.py:31-39, 88-96` are both keyword-based and absorb the new optional fields without churn. A drive-by positional construction added in a future PR (e.g., a benchmark harness in `bench/spy_20y.py`) would silently shift `prior_close` into the volume slot. A keyword-only lint test guards against this.

### [Medium] Layer 2 1e-10 invariant test name conflates two failure classes

The plan's `test_zero_cost_matcher_equity_curve_matches_m1_baseline_to_1e_minus_10` compares two engine paths (CloseFillMatchingEngine vs SquareRootImpactMatchingEngine + NoImpact + zero commission), not engine-vs-reference. The reference-function comparison (the Layer 1 1e-10 invariant the M1 day 3 reviewer pass committed to) is a SEPARATE failure class. Two tests; two failure classes; honest names.

### [Medium] ImpactedPriceSource missing from the 11-item trust boundary table

`docs/methodology/determinism.md:96-114` enumerates 11 boundaries. The ImpactedPriceSource carries mutable per-asset state that, in v1.1 with intraday slicing, becomes path-dependent on within-bar fill order. A v1.1 signal that reaches the decorator via the data source layer could break the determinism invariant silently.

### Splitting recommendation

Split PR B into PR B1 (ImpactedPriceSource decorator + standalone tests + ADR 0009 doc-only) and PR B2 (matcher + BarLoop wiring + Layer 2 invariant + golden-fixture E2E + matcher tests).

### Closing

The single most important thing the author should do before writing one line of code is fix the NEXT_BAR_OPEN structural lookahead.

## Author's response

The reviewer is right on every Critical and High. C2 (NEXT_BAR_OPEN structural lookahead) is the kind of error that produces wrong P&L on every NEXT_BAR_OPEN order silently, exactly the failure class the project's spec critique was designed to prevent; addressing it before code is the entire point of running this pass. C1 (dead tolerance check) is right at the level of math; the matcher cannot enforce the policy-vs-fill tolerance without seeing the policy's frozen estimate as a separate input, which requires growing the Order surface and is therefore PR C scope. H3 (Protocol vs hasattr) follows directly from the mypy-strict discipline committed in ADR 0003; the cost of a no-op default on `CloseFillMatchingEngine` is one line. H4 (BacktestConfig forward-staging) preserves the existing project convention that an ADR binds only on code it controls. H5 (cost_estimator wiring) closes the ADR 0003 decision 4 gap that the M1 day 3 reviewer's `_NoopCostEstimator` annotation already flagged as M2 work. H6 (first-bar fallback) is a documentation gap; with C1 deferred to PR C the documentation lives in the methodology doc and `test_cost_estimate_vs_fill_tolerance.py` exercises the formula symbolically.

The five Mediums are accepted in full. The splitting recommendation is contested: the four-PR M2 split already addressed the too-big risk; further splitting B1 from B2 produces two doc-only commits with a fragile ordering constraint (B2 cannot land before B1 because the matcher imports the decorator), and the review surface savings are small because B2 still carries the matcher's full complexity. The plan keeps PR B as a single PR with four feature commits and one doc commit per the four-commit plan section.

### Accepted

1. **NEXT_BAR_OPEN deferred to M3 with a typed UnsupportedFillPriceModelError at M2.** PR B's `SquareRootImpactMatchingEngine.submit(...)` raises `UnsupportedFillPriceModelError("FillPriceModel.NEXT_BAR_OPEN is an M3 deliverable; the deferred-fill mechanism requires Order plumbing that is M3 scope per ADR 0009")` for `FillPriceModel.NEXT_BAR_OPEN`. ARRIVAL is supported because the prior bar's data is unambiguously knowable; OPEN and CLOSE are the bar's own data. The `next_open` field on `MarketState` is NOT added; the only new optional field is `prior_close: Decimal | None = None`. This addresses the reviewer's C2 cleanly without requiring the matcher to carry a `_pending_next_bar_open` queue or the BarLoop to coordinate the queue flush. The M3 momentum signal that wants NEXT_BAR_OPEN gets a future ADR that lands the deferred-fill mechanism with its own reviewer pass.

2. **Tolerance contract NOT actively enforced at the matcher at M2.** `CostEstimateVsFillMismatchError` is NOT added to `execution/matching.py`. `docs/methodology/cost_model_tolerance.md` documents the formula and the worked example; `tests/integration/test_cost_estimate_vs_fill_tolerance.py` exercises the formula symbolically (constructs two cost models with different `MarketStateRow` sigma_D values keyed at the same (asset_id, dt), computes `estimate` on one and `compute` on the other, asserts the difference matches the locked formula). The methodology doc gets a new section "Active enforcement deferred to PR C" naming `Order.estimate_bps_at_submit` as the M3 scope that lets the matcher receive the policy's frozen estimate. This addresses the reviewer's C1 honestly: the contract is documented and tested via symbolic exercise; the live invariant ships when Order carries the policy-time estimate.

3. **MatchingEngine Protocol extended with on_bar_start.** The `MatchingEngine` Protocol at `src/pit_backtest/execution/matching.py:52-62` gains `on_bar_start(self, bar_dt: date) -> None`. `CloseFillMatchingEngine` adds a no-op `on_bar_start(self, bar_dt: date) -> None: pass`. `SquareRootImpactMatchingEngine`'s `on_bar_start` clears `_fills_this_bar`. The BarLoop calls `matching_engine.on_bar_start(bar_dt)` unconditionally at the top of each per-bar iteration AFTER `clock.advance_to(bar_dt)` (so the matcher's `_now` accessors see the advanced clock). The `hasattr` guard is dropped. This addresses the reviewer's H3 cleanly; mypy strict catches a typo at the implementation site.

4. **BacktestConfig field name NOT reserved.** ADR 0009 documents the BarLoop ctor arg `apply_permanent_impact_to_valuation: bool = True` (the M2 surface) and explicitly defers the `BacktestConfig.apply_permanent_impact_to_signal_pit_view` field to M3 with no name reservation. The "What this ADR does NOT do" section names "ADR 0009 does NOT reserve a BacktestConfig field; M3's BacktestConfig ADR will land the field with its own rationale." This addresses the reviewer's H4.

5. **BarLoop wires the real cost_model into the policy's cost_estimator slot.** The BarLoop ctor gains `cost_estimator: PreTradeCostEstimator | None = None`; if non-None, `self._cost_estimator = cost_estimator` replaces the `_NoopCostEstimator()` line at `bar_loop.py:119`. `tests/integration/test_cost_estimator_wired_to_policy.py` (new) constructs a BarLoop with the real `SquareRootImpactCostModel` as the cost estimator, runs the constant-weight demo, and asserts the policy's `cost_estimator.estimate(...)` is non-zero for at least one rebalance bar. The `EqualWeightMonthlyRebalancePolicy` does not currently consult `cost_estimator` so no behavior changes; the test asserts the wiring exists structurally. This addresses the reviewer's H5.

6. **First-bar `mid_at_estimate` fallback documented in methodology.** `docs/methodology/cost_model_tolerance.md` gains a new subsection "First-bar fallback" naming the open-as-mid convention and the resulting tolerance widening. The first-bar case is a documented artifact; under tolerance non-enforcement at M2 (Accepted #2) the artifact does not produce false failures, only documented widened tolerances when PR C wires Order.estimate_bps_at_submit. This addresses the reviewer's H6.

7. **signed_perm uses fill_price not arrival.** The matcher's step 6 becomes `signed_perm = float(breakdown.permanent_impact_bps) / 10_000.0 * float(fill_price) * sign(qty)`. The Fill's `permanent_impact_per_share` is `_to_boundary_decimal(signed_perm)`. The matcher's docstring includes a one-line note "permanent_impact_per_share derives from realized fill_price not arrival, so OPEN/CLOSE/ARRIVAL agree on the dollar magnitude that hits the ImpactedPriceSource register." This addresses the reviewer's Medium #7.

8. **Sign-convention tests pin numeric values.** `test_square_root_matcher_buy_lifts_fill_price_above_arrival` constructs a SPY-shaped fixture (arrival=$500, expected temp_bps=1.0) and asserts `Decimal("500.05") - Decimal(repr(fill_price))` is within 1e-9. The sell test asserts the symmetric value. A round-trip cash flow test asserts that buy outflow plus sell inflow at identical |qty| equals `2 * arrival * |qty| * temp_bps / 10_000` to within float64 noise. This addresses the reviewer's Medium #8.

9. **MarketState construction sites audited; keyword-only lint test ships.** `tests/lint/test_market_state_keyword_only.py` (new) AST-walks `src/pit_backtest/` and `tests/` for `MarketState(...)` calls and asserts every call uses keyword arguments. The existing two call sites at `src/pit_backtest/engine/bar_loop.py:235-243` and `tests/execution/test_matching.py:31-39, 88-96` are keyword-only by inspection. This addresses the reviewer's Medium #9.

10. **Layer 2 1e-10 invariant test split into two named tests.** `tests/integration/test_constant_weight_demo_m2_zero_cost.py` ships TWO tests:
   - `test_zero_cost_matcher_path_equals_close_fill_matcher_path_to_1e_minus_10` (matcher-vs-matcher; catches a refactor that breaks dispatch equivalence at zero cost).
   - `test_zero_cost_matcher_path_equals_reference_function_to_1e_minus_10` (matcher-vs-pure-Python-reference; catches a cost-model bug NoImpact does not exercise plus a matcher Decimal round-trip bug).
   The two tests catch two failure classes and are named honestly. This addresses the reviewer's Medium #10.

11. **Determinism trust boundary table extended to 12 items.** `docs/methodology/determinism.md` table at lines 96-114 grows row 12: "ImpactedPriceSource mutable state. The decorator carries a per-asset cumulative Decimal register. Signal.compute() and Policy.target_positions() must not read from the ImpactedPriceSource directly; they must consume only the engine-supplied `pit_view`. The lint test at `tests/lint/test_determinism_invariants.py` is extended to flag any `from pit_backtest.data.sources.base import ImpactedPriceSource` import in `src/pit_backtest/signal/` or `src/pit_backtest/policy/`." This addresses the reviewer's Medium #11.

12. **ImpactedPriceSource does NOT claim PitDataSource Protocol inheritance.** The decorator is a standalone class with `__slots__ = ("_raw", "_cumulative_per_share")` and the four-method surface (`apply_permanent_impact`, `adjust_price`, `cumulative_for`, `reset`). It does NOT declare `class ImpactedPriceSource(PitDataSource):`. The `get_price` stub is removed; M3 will wire `get_price` when the per-row PitDataSource path goes live. This addresses the reviewer's gotcha on Protocol satisfaction.

13. **Exception hierarchy: shared MatchingError base.** `MatchingError(ValueError)` is added as the base; `UnsupportedFillPriceModelError(MatchingError)` and `MultipleFillsPerBarError(MatchingError)` derive from it. Users can catch `MatchingError` for cross-cutting handling; named exceptions remain shallow. This addresses the reviewer's gotcha on exception hierarchy.

14. **Decimal-at-boundary uses _to_boundary_decimal everywhere.** The matcher imports `_to_boundary_decimal` from `pit_backtest.execution.cost.impact` and uses it for the `fill_price = _to_boundary_decimal(arrival_float * (1 + signed_temp_fraction))` conversion and the `signed_perm_decimal = _to_boundary_decimal(signed_perm)` conversion. Duplicated `Decimal(repr(...))` literals are removed. This addresses the reviewer's gotcha on boundary discipline.

### Contested

1. **Splitting PR B into B1 + B2.** Rejected. The four-PR M2 split (A/B/C/D) already addressed the too-big risk; B is at ~600 LOC plus tests, under the 700-LOC ceiling the M1 day 3 precedent set. Further splitting produces two ordering-coupled doc-only commits with the matcher importing the decorator across PR boundaries; the review surface savings are small because B2 still carries the matcher's full complexity. The single-PR plan with four feature commits and one doc commit preserves the M1 day 3 precedent without adding ceremony.

### Final locked decisions

These decisions are binding on the M2 PR B PR. Revisiting any requires a superseding ADR.

1. **`ImpactedPriceSource` is a standalone class** (`__slots__`, no Protocol inheritance at M2). Methods: `apply_permanent_impact(asset_id, per_share_signed) -> None`, `adjust_price(asset_id, raw_price) -> Decimal`, `cumulative_for(asset_id) -> Decimal`, `reset() -> None`. Volume reads bypass adjustment by contract (callers never route volume through the decorator). `get_price` is NOT implemented at M2.
2. **`apply_permanent_impact_to_valuation: bool = True`** on `BarLoop.__init__`; the snapshot/MTM step routes `prices_today[ticker]` through `impacted_source.adjust_price(...)` when the flag is True. Default ON.
3. **`apply_permanent_impact_to_signal_pit_view: bool = False`** is the documented policy. The flag does NOT exist as a BarLoop ctor arg at M2 because the v1 `EqualWeightSignal` does not consume `pit_view`. M3's BacktestConfig ADR lands the field with its own rationale.
4. **`SquareRootImpactMatchingEngine` supports OPEN / CLOSE / ARRIVAL**. NEXT_BAR_OPEN raises `UnsupportedFillPriceModelError("M3 deliverable; deferred-fill mechanism per ADR 0009")`. VWAP raises `UnsupportedFillPriceModelError("v1.1 intraday data required")` per ADR 0005 step 4. No `next_open` field on `MarketState`.
5. **`MarketState` gains `prior_close: Decimal | None = None`**. `MarketState` does NOT gain `next_open`. Both the BarLoop and the matcher use keyword construction; `tests/lint/test_market_state_keyword_only.py` locks this.
6. **`MatchingEngine` Protocol extended with `on_bar_start(self, bar_dt: date) -> None`**. `CloseFillMatchingEngine.on_bar_start` is a no-op. `SquareRootImpactMatchingEngine.on_bar_start` clears `_fills_this_bar`. BarLoop calls `matching_engine.on_bar_start(bar_dt)` unconditionally after `clock.advance_to(bar_dt)`.
7. **One-fill-per-(asset, dt)** enforced at the matcher via `_fills_this_bar: set[tuple[AssetId, date]]` (membership-only; determinism invariant allows). Reset on `on_bar_start`. Raises `MultipleFillsPerBarError` on second submit for the same (asset_id, `_et_date(market_state.dt)`) within the same bar.
8. **Tolerance contract NOT actively enforced at M2.** `CostEstimateVsFillMismatchError` is NOT added. `docs/methodology/cost_model_tolerance.md` documents the formula, the worked SPY example, the first-bar fallback (open-as-mid), and "Active enforcement deferred to PR C via Order.estimate_bps_at_submit." `tests/integration/test_cost_estimate_vs_fill_tolerance.py` exercises the formula symbolically (constructs two cost models with different `MarketStateRow` sigma_D values, computes `estimate` on one and `compute` on the other, asserts the difference matches the locked formula).
9. **Fill construction.**
   - `arrival = market_state.open / close / prior_close` per fill_price_model
   - `signed_temp_fraction = float(breakdown.temporary_impact_bps) / 10_000.0 * sign(order.quantity)` where `sign(positive)=+1`, `sign(negative)=-1`
   - `fill_price_float = float(arrival) * (1.0 + signed_temp_fraction)`
   - `fill_price = _to_boundary_decimal(fill_price_float)` (uses the helper from `execution/cost/impact.py:94-102` for boundary-precision consistency)
   - `signed_perm = float(breakdown.permanent_impact_bps) / 10_000.0 * float(fill_price) * sign(order.quantity)` (uses fill_price not arrival per reviewer Medium #7)
   - `commission = self._commission.commission_for(shares=order.quantity, notional=order.quantity * fill_price)`
   - `Fill(..., slippage_bps=Decimal("0"), temporary_impact_bps=breakdown.temporary_impact_bps, permanent_impact_per_share=_to_boundary_decimal(signed_perm), commission, dt=market_state.dt)`
   - `impacted_source.apply_permanent_impact(asset_id, fill.permanent_impact_per_share)` AFTER the Fill is constructed
10. **BarLoop wires `cost_estimator: PreTradeCostEstimator | None = None`** through to `Policy.target_positions(cost_estimator=...)`. If None, `_NoopCostEstimator()` is used (M1 behavior). `tests/integration/test_cost_estimator_wired_to_policy.py` asserts the wiring is observable.
11. **Exception hierarchy**: `MatchingError(ValueError)` base; `UnsupportedFillPriceModelError(MatchingError)`; `MultipleFillsPerBarError(MatchingError)`. Shallow named exceptions; users can catch the base for cross-cutting handling.
12. **Trust boundary table grows to 12 items** in `docs/methodology/determinism.md`. Item 12: ImpactedPriceSource mutable state; Signal/Policy must NOT import the decorator. Lint test `tests/lint/test_determinism_invariants.py` is extended to flag the import in `src/pit_backtest/signal/` or `src/pit_backtest/policy/`.
13. **Layer 2 1e-10 invariant test split into two**: `test_zero_cost_matcher_path_equals_close_fill_matcher_path_to_1e_minus_10` (matcher-vs-matcher) and `test_zero_cost_matcher_path_equals_reference_function_to_1e_minus_10` (matcher-vs-reference). Both pass at 1e-10 per bar on the synthetic 2-year fixture.
14. **MarketState keyword-only lint** at `tests/lint/test_market_state_keyword_only.py` AST-walks `src/pit_backtest/` and `tests/` for any positional `MarketState(...)` call and fails the suite.
15. **Numeric-pin sign-convention tests** at `tests/execution/test_matching.py`: `test_square_root_matcher_buy_lifts_fill_price_above_arrival` and `test_square_root_matcher_sell_lowers_fill_price_below_arrival` assert specific Decimal fill prices; `test_buy_and_sell_cash_flows_are_symmetric_about_arrival_notional` asserts the round-trip cost identity.
16. **Test count target**: PR B adds roughly 30-40 new tests across the 7 new test files plus the extensions to `tests/execution/test_matching.py` and `tests/integration/test_constant_weight_demo.py`. Full suite (currently 190 tests) ends at ~220-230 tests, all green; mypy strict clean.
17. **Single PR with 5 commits** per the four-PR M2 split. PR B does not split further. Commits:
   1. `feat(data): ImpactedPriceSource decorator with permanent-impact register`
   2. `feat(execution): SquareRootImpactMatchingEngine and MatchingEngine.on_bar_start Protocol extension`
   3. `feat(engine): BarLoop wires impacted source, cost estimator, prior close, on_bar_start`
   4. `test(integration): Layer 2 1e-10 invariant + golden fixture E2E + permanent impact next-bar mid drops + cost-estimator wiring`
   5. `docs(decisions+methodology+changelog): ADR 0009 + tolerance methodology + trust boundary item 12 + roadmap + readme + changelog`

## What this ADR does NOT do

- **Does NOT reserve a BacktestConfig field name.** M3's BacktestConfig ADR will land `apply_permanent_impact_to_signal_pit_view` with its own rationale. ADR 0009 only documents the BarLoop ctor arg `apply_permanent_impact_to_valuation: bool = True` (the M2 surface).
- **Does NOT ship NEXT_BAR_OPEN as a working fill-price model.** NEXT_BAR_OPEN is M3 deliverable per the deferred-fill mechanism that requires Order plumbing.
- **Does NOT actively enforce the cost-model tolerance at the matcher.** Active enforcement requires `Order.estimate_bps_at_submit` plumbing that is PR C scope per the four-PR M2 split. The tolerance contract is documented and tested symbolically at M2.
- **Does NOT modify ADR 0005 step 18 in place.** ADR 0005 stays read-only history per the project convention; ADR 0009 supersedes step 18's "ADR 0007" placeholder by cross-reference. ADR 0005 step 18 is updated by a single cross-reference line if any future ADR needs it.

## Status

Accepted. M2 PR B implements the 17 locked decisions above. Deviations require a superseding ADR.
