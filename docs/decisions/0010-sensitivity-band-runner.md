# ADR 0010: Sensitivity-band runner and Runner.run_sweep multiprocessing architecture

Status: Accepted.
Date: 2026-05-29.
Authors: Sam Doane (with Plan + skeptical-reviewer pass per project rule 2).

## Context

ADR 0005 step 14 reserved the `SensitivityBand` attrs container as the rendering surface for the eta sweep over `[0.05, 0.10, 0.142, 0.20, 0.30]`. ADR 0002 M2 acceptance criterion 2 commits the sweep produces five SPY equity curves on one plot; ADR 0007 makes the central `eta=0.142` falling inside the formula-derived band the cost-realism acceptance gate. ADR 0005 step 16 binds the four-PR M2 split (PRs A/B/C/D); PR A (#17) shipped the cost-model math; PR B (#19) shipped the matcher + ImpactedPriceSource + BarLoop wiring; PR C/PR D remain.

The original PR C plan landed BOTH the sensitivity-band runner (Chunk 2) AND active enforcement of the cost-model tolerance contract (Chunk 1). The skeptical-reviewer pass on the Plan surfaced 3 Critical findings on Chunk 1: (a) `cost_model.estimate(...)` and `cost_model.compute(fill_state)` produce bit-identical outputs under shared-instance dispatch (which is the default M2 wiring per `tests/integration/test_cost_estimator_wired_to_policy.py:141`), so the proposed tolerance check is structurally dead; (b) the proposed `mid_at_estimate` candidate (`prices[ticker]` from `price_lookup`) is today's close, not the prior-close start-of-bar mid the methodology doc names; (c) the `Almgren` cost model at M2 is mid-INSENSITIVE so the tolerance check measures a quantity the cost model does not depend on. Sam confirmed the split: Chunk 1 (active tolerance enforcement) is deferred to a future PR C2 with a separate ADR 0011 that picks one of three architectural options for how the policy and matcher get distinct estimate inputs; Chunk 2 (sensitivity-band runner) ships as PR C1 today.

ADR 0010 captures Chunk 2 only. The 8-lock plan from the original PR C draft collapses to 6 locks scoped to the sensitivity-band runner.

## The plan (summarized)

The Plan agent's deliverable for Chunk 2, condensed:

### Files

| File | Status | Kind |
|---|---|---|
| `src/pit_backtest/analytics/sensitivity.py` | new | `SensitivityBand` attrs.frozen + `from_run_sweep` factory + render helpers + optional plot |
| `src/pit_backtest/engine/runner.py` | rewrite Runner class | `run_sweep` multiproc; `_worker_run_one_param` module-level worker |
| `examples/spy_cost_sensitivity.py` | new | CLI demo; `SpyCostSensitivityRecipe` attrs.frozen for picklable inputs; module-level `build_bar_loop_for_eta` factory |
| `tests/analytics/__init__.py` | new | package init |
| `tests/analytics/test_sensitivity.py` | new | SensitivityBand construction + validation + render byte-for-byte + accessors |
| `tests/engine/test_runner.py` | new | Runner.run_sweep determinism + picklability + POLARS_MAX_THREADS=1 probe |
| `tests/integration/test_spy_cost_sensitivity.py` | new | CLI E2E + monotone-eta-PnL ordering + central-in-band + missing-snapshot exit |
| `docs/decisions/0010-...md` | new | this ADR |
| `docs/methodology/determinism.md` | extend | document Runner enforcement of Requirement 5 |
| `CHANGELOG.md`, `docs/ROADMAP.md`, `README.md` | extend | PR C1 shipped notes |

Net new positive code is approximately 500 LOC across production code and ~30 new tests.

### API shape (locked)

```python
# src/pit_backtest/analytics/sensitivity.py

@attrs.frozen(slots=True)
class SensitivityBand:
    parameter_name: str
    parameter_values: tuple[Decimal, ...]
    per_parameter_equity: dict[Decimal, pl.DataFrame]
    per_parameter_final_pnl: dict[Decimal, float]
    central_value: Decimal
    confidence_tier: ConfidenceTier
    tickers: tuple[str, ...]
    start_dt: date
    end_dt: date
    initial_capital: float
    sharadar_bundle: str

    def __attrs_post_init__(self) -> None: ...     # validates
    def render_summary_line(self) -> str: ...
    def render_band_table(self) -> str: ...
    def equity_curve_at(self, parameter_value: Decimal) -> pl.DataFrame: ...
    def to_plot_frame(self) -> pl.DataFrame: ...

    @classmethod
    def from_run_sweep(
        cls,
        results: list[ConstantWeightDemoResult],
        parameter_name: str,
        parameter_values: tuple[Decimal, ...],
        central_value: Decimal,
    ) -> "SensitivityBand": ...
```

```python
# src/pit_backtest/engine/runner.py

class Runner:
    def __init__(self, num_workers: int | None = None) -> None: ...

    def run_sweep(
        self,
        param_grid: list[dict[str, object]],
        bar_loop_factory: Callable[[dict[str, object]], BarLoop],
        *,
        start_dt: date,
        end_dt: date,
    ) -> list[ConstantWeightDemoResult]: ...

    def run_cpcv(...) -> BacktestPathDistribution: ...  # stays NotImplementedError (M4)


def _worker_run_one_param(
    params: dict[str, object],
    bar_loop_factory: Callable[[dict[str, object]], BarLoop],
    start_dt: date,
    end_dt: date,
) -> ConstantWeightDemoResult:
    # MUST set os.environ["POLARS_MAX_THREADS"] = "1" BEFORE any Polars-importing module
    import os
    os.environ["POLARS_MAX_THREADS"] = "1"
    # ... build BarLoop via the factory, run, return
```

## Skeptical reviewer's response

The senior multi-strat-fund quant reviewer (same persona as ADRs 0001-0009) returned a `restructure` verdict on the OVERALL PR C plan, but the verdict is driven entirely by Chunk 1 findings (which Sam confirmed deferring to PR C2 / ADR 0011). The reviewer's Chunk 2 findings are condensed below; all are addressed in this ADR.

### Top 5 things the plan gets RIGHT (Chunk 2)

1. `SensitivityBand` as `attrs.frozen` carrying `confidence_tier=SWEEP_SELECTED_NO_CORRECTION` not a `BacktestPathDistribution`; matches ADR 0005 step 14 / step 17 lock #14.
2. Spawn-only multiproc on both platforms honors `docs/methodology/determinism.md:84-94` Requirement 5 and trust boundary #9 without relying on user discipline.
3. `SensitivityBand` validates `central_value in parameter_values` and key consistency at construction.
4. Module-level worker function `_worker_run_one_param` is required for spawn pickling on Windows; routing the factory in as an argument keeps the worker side stateless.
5. OUT-OF-SCOPE list honestly defers PR D items (Bouchaud CLI, bench, perf-budget workflow).

### [Critical] Runner.run_sweep return-type contract diverges across ADR 0005, the stub, and the plan

`docs/decisions/0005-m2-cost-realism-plan.md:37` says `SensitivityBand`; `src/pit_backtest/engine/runner.py:38-43` (the stub) says `pl.DataFrame`; the plan said `list[ConstantWeightDemoResult]`. Three contracts; pick one.

### [Medium] Spawn-only multiproc cost on a 2-year SPY fixture is comparable to workload, not amortized

5-parameter sweep at 5 spawns ~= 5 * (30s bootstrap + ~3s workload) ~= 165s with spawn; fork-on-Linux + spawn-on-Windows ~= 15s on Linux. The plan oversold the amortization claim; the cost is real and 10x on M2 fixtures.

### [High] Picklability probe `pickle.dumps(bar_loop_factory)` does not catch the failure class it is sold as catching

Pickling a module-level function reference always succeeds; the function's free variables are resolved by name in the worker after spawn. The probe gives false confidence. Real check is `pickle.dumps((bar_loop_factory, params_grid[0]))` AND a synchronous dry-run of `bar_loop_factory(params_grid[0])` in the parent before submitting to the pool.

### Gotchas before the first line of code

1. `SensitivityBand.confidence_tier` defaulting to `SWEEP_SELECTED_NO_CORRECTION` is correct, but the container should REJECT `CPCV_WITH_DSR_CORRECTION` at construction so a future caller cannot accidentally use the wrong container.
2. `Runner.run_sweep`'s `num_workers` default does not account for the parent process; on a 4-vCPU runner with `param_grid=5`, the default spawns 4 workers and the parent competes with them. Document.
3. `os.environ["POLARS_MAX_THREADS"] = "1"` set in the worker is correct only if no module in the import chain transitively imports Polars BEFORE that line runs. The plan does not specify import order; lock it in `_worker_run_one_param` as the FIRST line of the function body.
4. The picklability probe should fire under pytest's parent-process Polars import; the `tests/engine/test_runner.py` factory picklability test must run under pytest, not a manual `python -c` script.

### ADR-naming recommendation

Counter the original plan: split ADR 0010 (sensitivity-band runner) from ADR 0011 (active tolerance enforcement). ADR 0010 ships with PR C1; ADR 0011 ships with PR C2.

### Splitting recommendation

Counter: split PR C into PR C1 (sensitivity band; clean win) and PR C2 (active tolerance; structurally blocked until architecture decided). Sam confirmed.

## Author's response

The reviewer is right on every Chunk 2 finding. The Critical C3 (Runner return type) is the kind of three-contract drift that produces year-long doc-vs-code disagreements; locking the return type as `list[ConstantWeightDemoResult]` at the runner with `SensitivityBand.from_run_sweep(...)` factory at the analytics layer is the cleanest separation. The Medium on spawn cost is fair: the plan oversold amortization, the true ratio is 10x on M2 fixtures, and the determinism doc has already accepted the trade per Requirement 5. The High on the picklability probe correctly identifies that pickle.dumps of a module-level function reference is theater; the dry-run + tuple-pickle gate is the right pattern.

Sam's split decision means Chunk 1's Critical findings (C1 dead code, C2 wrong mid_at_estimate, the H findings on Decimal precision and tolerance-check meaning) all defer to ADR 0011 / PR C2. ADR 0010 captures Chunk 2 only.

### Accepted

1. **`Runner.run_sweep` return type = `list[ConstantWeightDemoResult]`** (raw-results API). `SensitivityBand.from_run_sweep(results, parameter_name, parameter_values, central_value) -> SensitivityBand` factory at `analytics/sensitivity.py` does the wrapping with validation. Reasoning: `Runner.run_sweep` may be used for non-sensitivity-band sweeps in M3+ (e.g., grid search over `eta` and `beta` jointly, which is not a 1D band); coupling the runner to `SensitivityBand` overconstrains the future API. The runner's contract is "raw results in param_grid order"; the analytics layer owns the validation.
2. **Spawn-only multiproc on both Linux and Windows.** Honors `docs/methodology/determinism.md` Requirement 5 ("multiprocessing.spawn (the default on Windows; explicit on Linux for safety)"). The wall-clock cost trade is honest: 5-param sweep on 2y SPY is approximately 165s with spawn vs 15s with fork-on-Linux (10x slowdown), accepted because the determinism doc already committed to spawn on both platforms. The PR description and the ADR's "Cost of spawn-only" section state this explicitly; no amortization claim.
3. **Picklability gate at submit time: BOTH `pickle.dumps((bar_loop_factory, param_grid[0]))` AND a synchronous dry-run of `bar_loop_factory(param_grid[0])` in the parent.** The dry-run catches non-picklable closure capture, module-level side-effect ordering, and per-worker reconstruction cost surprises. Fail-fast at submit; the user sees the exact failure (PicklingError, AttributeError on missing module-level state, etc.) at the construction site, not 30 seconds later from inside a worker.
4. **`POLARS_MAX_THREADS=1` set as the FIRST line of `_worker_run_one_param`.** No imports above the line; no module-level Polars import in the worker bootstrap. A comment on the function locks this. A lint test `tests/lint/test_runner_worker_polars_threads_first.py` (new) AST-walks the function body and asserts the first executable statement is the env-var assignment.
5. **`SensitivityBand.__attrs_post_init__` rejects `confidence_tier != SWEEP_SELECTED_NO_CORRECTION`.** Per the reviewer's gotcha: a future caller passing `CPCV_WITH_DSR_CORRECTION` accidentally would silently bypass the analytics-layer's correct container (`BacktestPathDistribution`). Raises `ValueError` at construction with the message "SensitivityBand is for sweep results; CPCV-corrected paths use BacktestPathDistribution per ADR 0005".
6. **`num_workers` default = `min(len(param_grid), max(1, multiprocessing.cpu_count() - 1))`.** The parent process is single-threaded by design but it does compete with workers for CPU; reserving one core for the parent prevents the 4-vCPU saturation case the reviewer named. Documented in the docstring.
7. **Determinism doc extension.** `docs/methodology/determinism.md` Requirement 5 gains a sentence: "The Runner enforces this contract: `_worker_run_one_param` sets `os.environ['POLARS_MAX_THREADS'] = '1'` as the first line of its body before any Polars-importing module is loaded." The lint test at `tests/lint/test_runner_worker_polars_threads_first.py` cross-references this paragraph.

### Contested

None. The reviewer's findings on Chunk 2 are fully accepted.

### Final locked decisions

These 6 decisions bind the PR C1 implementation. Revisiting any requires a superseding ADR.

1. **`Runner.run_sweep(param_grid, bar_loop_factory, *, start_dt, end_dt) -> list[ConstantWeightDemoResult]`.** Results in param_grid order. No analytics validation at the runner.
2. **`SensitivityBand.from_run_sweep(results, parameter_name, parameter_values, central_value) -> SensitivityBand`** class method at `analytics/sensitivity.py`. Does the wrapping with validation: parameter_values sorted ascending; central_value in parameter_values; len(results) == len(parameter_values); confidence_tier set to `SWEEP_SELECTED_NO_CORRECTION` automatically.
3. **`SensitivityBand.__attrs_post_init__` rejects** `confidence_tier != SWEEP_SELECTED_NO_CORRECTION`; raises `ValueError` at construction. A caller cannot construct a `SensitivityBand` with a CPCV tier; that would be a `BacktestPathDistribution` (M4 work).
4. **Spawn-only multiproc on both platforms** via `multiprocessing.get_context("spawn")` regardless of OS. Wall-clock cost is approximately 10x on M2 fixtures; accepted per the determinism doc commitment. No fork fallback.
5. **`_worker_run_one_param` sets `POLARS_MAX_THREADS=1` as the first line of its body.** Lint test at `tests/lint/test_runner_worker_polars_threads_first.py` AST-walks and asserts the first executable statement is the env-var assignment.
6. **Picklability gate at `Runner.run_sweep` submit time** runs BOTH `pickle.dumps((bar_loop_factory, param_grid[0]))` AND a synchronous dry-run of `bar_loop_factory(param_grid[0])` in the parent process BEFORE submitting to the pool. The dry-run catches non-picklable closure capture, module-level side-effect ordering, and per-worker reconstruction surprises. Raises `RuntimeError` with a diagnostic message naming which probe failed.

### Additional binding decisions

7. **`num_workers` default = `min(len(param_grid), max(1, cpu_count() - 1))`.** Reserves one core for the parent process to avoid 4-vCPU saturation. Documented in the docstring.
8. **`SpyCostSensitivityRecipe` attrs.frozen** at `examples/spy_cost_sensitivity.py` carries picklable bundle info (snapshots_root as str, bundle_name, ticker, start_dt, end_dt, initial_capital). The `build_bar_loop_for_eta(params: dict, recipe: SpyCostSensitivityRecipe) -> BarLoop` factory is module-level (picklable).
9. **`examples/spy_cost_sensitivity.py` CLI** flags: `--start-dt`, `--end-dt`, `--ticker SPY`, `--initial-capital 1000000`, `--bundle-prefix sharadar`, `--snapshots-root data/snapshots`, `--workers N`, `--log-level`. Exit codes: 0 on success; 1 on cost-model-not-applicable (universe lacks SPY); 2 on missing snapshot.
10. **Eta grid is locked at `(Decimal("0.05"), Decimal("0.10"), Decimal("0.142"), Decimal("0.20"), Decimal("0.30"))`** in the example CLI default per ADR 0005 step 16's "five eta values" headline. The user can override via a future CLI flag if needed (not in PR C1 scope).

## What this ADR does NOT do

- **Does NOT add `Order.estimate_bps_at_submit` or `Order.mid_at_estimate` fields.** Active tolerance enforcement is PR C2 / ADR 0011 scope. The methodology doc at `docs/methodology/cost_model_tolerance.md` continues to name PR C as the active-enforcement landing site; the doc will be re-pointed to PR C2 when ADR 0011 lands.
- **Does NOT add `CostEstimateVsFillMismatchError`.** Same reason. Defer to ADR 0011.
- **Does NOT extend `TargetPositions` with estimate dicts.** Same reason.
- **Does NOT modify ADR 0005 step 14 in place.** ADR 0005 stays read-only history; ADR 0010 supersedes step 14's `SensitivityBand` description by cross-reference. ADR 0005 step 14 is referenced from this ADR's Context section.
- **Does NOT ship the `--impact-model=bouchaud` CLI flag, `bench/spy_20y.py`, `.bench-baseline.json`, or `.github/workflows/perf-budget.yml`.** PR D scope per ADR 0005 step 16.
- **Does NOT activate the `Runner.run_cpcv(...)` path.** Stays `NotImplementedError("M4 deliverable")`.

## Status

Accepted. PR C1 implements the 10 locked decisions above (6 in the "Final locked decisions" section; 4 in "Additional binding decisions"). Deviations require a superseding ADR. PR C2 (active tolerance enforcement) opens with its own Plan + reviewer + ADR 0011 cycle once Sam decides the architectural option (two cost-model instances vs shifted-dt lookup vs dormant-scaffold) for tolerance enforcement.
