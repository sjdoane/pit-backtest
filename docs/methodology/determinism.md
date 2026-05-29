# Determinism invariant

Status: locked for M1.
ADR cross-references: ADR 0003 decisions 11 and 12 (the invariant itself and the 11-item trust boundary list); ADR 0001 decisions 17 and 18 (reproducibility per platform; cross-platform is a documented limitation).
Audience: every implementer; reviewers verifying that a backtest result is reproducible.

## The invariant

Two backtest runs with the same inputs produce bit-identical outputs.

"Same inputs" means: same git commit SHA, same `uv.lock`, same data snapshot bundle (verified by SHA256 per [`dataset_versioning.md`](dataset_versioning.md)), same config object, same RNG seed, same platform-BLAS tuple.

"Bit-identical outputs" means: the same Markdown scorecard, the same `BacktestResult.model_dump_json()` string, the same equity curve floats, the same fill sequence in the same order. Float comparisons are exact, not approximate. Hash of the output JSON is the same byte-for-byte.

## What this invariant does and does not promise

It promises:

- The M1 SPY reconciliation that passes on Sam's laptop today passes again on Sam's laptop in six months from a fresh checkout, given the same snapshot bundle.
- A peer who clones the repo, pulls the same Sharadar snapshot (SHA256 matches), and runs the same command, gets the same number.
- A CPCV run that produces 25 paths today produces the same 25 paths in the same order tomorrow, with the same per-path SR and DSR values.
- A regression in the engine that changes a downstream number is detected by a single hash comparison, not by an approximate equality check that might mask the change.

It does not promise:

- Cross-platform determinism. A run on Sam's Windows laptop and a run on a Linux server may differ at the floating-point ULP level due to BLAS implementation differences (MKL vs OpenBLAS), CPU SIMD instruction differences (x86 AVX2 vs ARM NEON), and Python interpreter differences (CPython on Windows vs CPython on Linux can differ in float repr in edge cases). The platform of record for the M1 kill-early gate is the platform on which the gate was run, recorded in the PR description.
- Determinism across Polars major versions. Polars's `group_by` aggregation order, lazy-frame plan optimization, and parallel-reduce strategy have changed across versions. The pinned version (`uv.lock`) is the protection; bumping Polars is an explicit change that requires re-running the SPY reconciliation as a regression check.
- Determinism in the face of user code that violates the trust boundaries (see [Trust boundaries](#trust-boundaries) below). The engine cannot enforce a user's signal callback to not use `random.random()`; it can only document the boundary and provide a Generator instance the user is expected to consume.

## The five determinism requirements

Per ADR 0003 decision 11, these are the mechanisms that combine to deliver the invariant. All five must hold; failing any one breaks the invariant.

### Requirement 1: Polars version is pinned

Polars is the tabular workhorse. Its parallel-reduce order, lazy-plan optimization, and `group_by` aggregation order are version-dependent. The `pyproject.toml` constraint and `uv.lock` together pin the exact version.

Implementation:

- `pyproject.toml` specifies `polars == X.Y.Z` (no version range; pin to the exact patch).
- `uv.lock` is committed to git and reflects the resolved version.
- A unit test in `tests/lint/test_determinism_invariants.py::test_polars_version_pinned` reads `polars.__version__` and asserts it equals the value declared in `pyproject.toml`.
- The pinned version is updated only via a PR with a "Polars version bump" subject; that PR re-runs the SPY reconciliation locally and includes the delta in the PR description (must be zero; non-zero is a regression and the bump is rejected).

The version on which v1 develops will be pinned at M1 scaffolding time. Pre-pin recommendation: the latest 1.x release at the time of M1, with bumps deferred until necessary.

### Requirement 2: `numpy.random.Generator` is plumbed through

Every consumer of randomness in the engine takes a `numpy.random.Generator` instance as a constructor argument. The engine never calls `random.random()`, `np.random.rand()`, or any module-level RNG function; those route through Python's and NumPy's global state, which is process-global and not deterministic across re-imports or thread contexts.

Implementation:

- The CLI config (`BacktestConfig.seed: int`) is the user-facing seed.
- At engine start, `numpy.random.default_rng(seed)` produces the root Generator.
- The root Generator is passed to the `Backtest` constructor. The Backtest then spawns child generators via `root.spawn(N)` for each consumer that needs independence (per-CPCV-path, per-strategy-family, per-sweep-trial).
- Any signal or policy that needs randomness (e.g., a noisy-feature regularizer) takes a `Generator` argument; the engine refuses to construct a signal that does not declare its RNG dependency.
- A lint test under `tests/lint/test_determinism_invariants.py` walks `src/pit_backtest/` for any `import random` or `np.random.<func>()` call outside an explicit allowlist (the allowlist contains only the engine-startup module that calls `np.random.default_rng`).

The `Generator.spawn()` API (NumPy 1.25+) produces statistically independent generators from a parent, which is the correct primitive for parallel CPCV paths. Each path's generator is fully determined by the root seed and the path index.

### Requirement 3: Output frames are sorted at every step

Polars `group_by`, joins, and lazy aggregations do not guarantee row order. Two runs of the same query can produce frames with rows in different orders, which then produces different hashes downstream even though the data is logically identical.

The mitigation: every frame that exits a non-trivial Polars operation is explicitly sorted before it is consumed by the next stage. The sort key is the natural key of the frame (typically `(asset_id, dt)` for per-asset-bar frames; `(dt, asset_id)` for cross-sectional frames; `(asset_id, period_end_dt, available_dt)` for fundamentals).

Implementation:

- A helper `pit_backtest.utils.frames.sorted_by(df, *keys)` wraps `df.sort(*keys)` and is the only API the engine code uses to commit to a sort order. Reviewers grep for `sorted_by` to verify discipline.
- The `Signal.compute()` return is a `dict[AssetId, float]` which has insertion order semantics in Python 3.7+; the engine canonicalizes by sorting on `AssetId` before iterating in the policy layer. The canonicalization wrapper lives in `pit_backtest.engine.bar_loop.BarLoop._canonical_signal_iter()`.
- The `MatchingEngine.submit()` iteration order over orders is sorted by `(submit_dt, asset_id, order_id)`. The Runner aggregation of per-path results is sorted by path index.

### Requirement 4: No `set` iteration in policy or signal layers

Python's `set` and `frozenset` types use hash-based ordering, which depends on `PYTHONHASHSEED`. Even with `PYTHONHASHSEED=0` (which we set in test runs), set iteration order is implementation-defined and can differ across Python patch releases.

The mitigation: policy and signal code uses `dict` (insertion-ordered since 3.7) or `list` for collections that need iteration. `set` is permitted only for membership tests where iteration order does not matter (the engine does not iterate sets in any code path that affects outputs).

Implementation:

- A lint test under `tests/lint/test_determinism_invariants.py::test_no_set_iteration_in_policy_or_signal` walks the AST of `src/pit_backtest/signal/` and `src/pit_backtest/policy/` for `for ... in <set-typed-expression>` and `list(<set-typed-expression>)` patterns. Detection is conservative: any local variable whose type annotation is `set[...]` triggers the check.
- The `PYTHONHASHSEED=0` environment is set in `pyproject.toml`'s `[tool.pytest.ini_options]` section so the test runner uses a fixed seed; production runs of the engine via the CLI also set `PYTHONHASHSEED=0` unless the user opts out.

### Requirement 5: Per-process Polars thread pool sized to 1 inside Runner workers

The Polars thread pool is process-global. With multiprocessing.spawn (the default on Windows; explicit on Linux for safety), each child process has its own thread pool, but within a single process Polars's parallel reductions are non-deterministic in the order of partial-sum accumulation. Float addition is not associative, so different reduction orders produce different last-bit results.

The mitigation, per ADR 0003 decision 11:

- Inside each `Runner` worker process, set `POLARS_MAX_THREADS=1` before importing Polars. The worker bootstrapper does this via `os.environ` before any other import.
- The main process (which orchestrates the workers but does not run heavy Polars operations) can use the full thread pool. The performance cost of single-threaded workers is offset by running multiple workers concurrently via multiprocessing.
- A unit test verifies that two consecutive single-process backtest runs (no Runner) produce identical outputs and that two consecutive Runner-orchestrated CPCV runs produce identical outputs.

The trade-off: per-worker single-threading slows each path's computation. For CPCV with 25 paths on a 16-core machine, running 16 workers each single-threaded is faster than one worker using 16 threads (because of Polars's marginal per-thread synchronization overhead at the small problem sizes of a single backtest). For a single backtest (M1), the cost is acceptable.

## Trust boundaries

These are places the engine cannot enforce the invariant by itself; user discipline is required, and the engine documents and (where possible) lints. Per ADR 0003 decision 12 the list started at 11 of these; ADR 0009 lock #12 extended to 12 with the ImpactedPriceSource entry below. The table below combines the boundary, the engine's mechanism for catching violations, and what the user must do.

| # | Boundary | What the engine does | What the user must do |
|---|---|---|---|
| 1 | Arbitrary Python in `Signal.compute()` | `pit_view` exposes only `available_dt < dt` data. AST lint flags `import requests`, `urllib`, `httpx`, `socket` in `src/pit_backtest/signal/` and any module under `examples/signals/`. | Do not make network calls or read non-PIT files from inside `compute()`. Use only the engine-supplied `pit_view`. |
| 2 | Arbitrary Python in `Policy.target_positions()` | Same `pit_view` discipline. Same AST lint. | Same. |
| 3 | External feature joins via `additional_data` | The data layer rejects frames passed to `additional_data` that lack an `available_dt` column. A contract check verifies that `available_dt` values are not in the future relative to any `period_end_dt` in the same frame. | If feeding alternative data, ensure the data has correctly populated `available_dt` from the original ingest source, not from a wall-clock fillforward. |
| 4 | Polars DataFrame mutation | Engine returns Polars frames that have not been `.clone()`-ed; Polars is copy-on-write under the hood for many operations. The engine never relies on user frames being unmodified. | Treat any frame returned by the engine as immutable. If you must modify, clone first. |
| 5 | Closure variables in user code | Engine cannot inspect user closures. | Do not close over module-level mutable state in signal or policy callbacks. The trial registry's dataset fingerprint check (see [`dataset_versioning.md`](dataset_versioning.md)) detects the most common cases at the next backtest run. |
| 6 | `@lru_cache` on user-defined helpers | Engine cannot prevent. The trial registry's dataset fingerprint detects when a cached helper returns stale results across backtest runs by comparing the input hash to the cache hit. | Do not cache results across backtests in a global cache. Within a single backtest, use the engine's own per-bar memoization, not a user-level `lru_cache`. |
| 7 | Direct `pandas.read_csv` or `polars.read_csv` of a non-PIT file | Engine cannot intercept arbitrary user file reads. The `Universe` API requires explicit membership declaration; the data quality contracts flag inconsistencies between user-supplied data and the engine's PIT view. | Use the engine's `PitDataSource` for all data reads. If you need a non-PIT source, pass it through `additional_data` so the contracts in row 3 fire. |
| 8 | RNG (`random.random`, `np.random.*` without injected Generator) | AST lint in `tests/lint/test_determinism_invariants.py` catches `import random`, `from random import *`, `np.random.<func>()` calls outside an explicit allowlist. The allowlist contains only the engine-startup module that calls `np.random.default_rng(seed)`. | If your signal or policy needs randomness, declare a `Generator` argument; the engine will pass one. Never call module-level RNG functions. |
| 9 | Polars global thread pool with concurrent Runner workers | The Runner worker bootstrapper sets `POLARS_MAX_THREADS=1` before importing Polars. A unit test verifies that two consecutive Runner runs produce identical outputs. | Do not override `POLARS_MAX_THREADS` inside a signal or policy. If you spawn your own threads, the engine's determinism guarantee does not extend to them. |
| 10 | Mutating frames inside plotting / notebook helpers | Engine cannot prevent. Plotting helpers in `src/pit_backtest/utils/plotting.py` (if added in M5) always clone before mutating. | Do not call `df.with_columns(...)` on a frame returned by `BacktestResult.equity_curve()` if you intend to compare runs. Clone first. |
| 11 | Module-level `import` with network side effects | AST lint flags `requests.get(...)`, `urllib.request.urlopen(...)`, etc. at module top level in `src/pit_backtest/`. | Do not import packages that make network calls at import time. If a dependency does (rare in Python; HTTP-client libraries do not), wrap the import inside a function that the engine never calls at startup. |
| 12 | `ImpactedPriceSource` mutable per-asset register | AST lint at `tests/lint/test_determinism_invariants.py::test_no_impacted_price_source_import_in_signal_or_policy` flags `from pit_backtest.data.sources.base import ImpactedPriceSource` in `src/pit_backtest/signal/` or `src/pit_backtest/policy/`. | Signal.compute() and Policy.target_positions() must read prices only via the engine-supplied `pit_view` (which is the cumulative-impact-aware view managed by the BarLoop), never via direct decorator access. Reading the decorator's register from inside signal/policy would couple the determinism invariant to within-bar fill order; v1's one-fill-per-(asset, dt) makes this M2-safe, but v1.1 intraday slicing would silently break determinism. |

The `SquareRootImpactMatchingEngine` itself carries a mutable `_fills_this_bar: set[tuple[AssetId, date]]` for one-fill-per-(asset, dt) enforcement per ADR 0009 lock #7. The set is used membership-only (no iteration); the `on_bar_start(bar_dt)` hook clears it per-bar. Membership-only usage preserves Requirement 4 (no `set` iteration in signal/policy layers) at M2; any v1.1 change that iterates the set (e.g., partial-fill rollover ordering) must update Requirement 4's enforcement to cover the matcher.

The twelve items above are the full enumerated list. The trust boundaries that the engine does enforce (single-process BarLoop ordering, `pit_view` strict-less-than, `FillPriceModel` required on `Order`) are structural; they are documented in ADR 0003 but are not "trust boundaries" in the sense of this list, because the engine catches violations at construction or call time rather than relying on user discipline.

## Cross-platform reproducibility

Per ADR 0001 decision 18: float reproducibility is promised within a single platform-BLAS tuple, not across platforms. The "platform of record" for a result is recorded in the `BacktestResult.environment` field:

```python
class Environment(BaseModel):
    python_version: str           # "3.11.9"
    polars_version: str           # "1.27.0"
    numpy_version: str            # "1.26.4"
    blas_implementation: str      # "openblas-0.3.27", "mkl-2024.0", etc.
    cpu_arch: str                 # "x86_64", "arm64"
    os: str                       # "Linux-5.15.0", "Windows-10.0.26200", "Darwin-23.4.0"
    hostname_hash: str            # SHA256 of socket.gethostname(), so we can spot host changes without leaking the hostname
```

The M1 PR's reconciliation evidence line is:

```
M1 SPY reconciliation: PASS (delta = 2.3 bps annualized, snapshot = sharadar_2026-06-15,
environment = python-3.11.9 + polars-1.27.0 + numpy-1.26.4 + openblas-0.3.27 on Windows-10.0.26200, x86_64)
```

A reviewer running on a different platform may see a different delta in the lowest-significance bits. The 5-bps tolerance is sized for these cross-platform deltas; the test does not assert bit-identical equity curves cross-platform, only within-tolerance on the annualized return.

## CI enforcement

The following tests run on every push to a branch with an open PR:

- `tests/lint/test_pydantic_boundary.py`: enforces the Pydantic-only-at-three-surfaces rule from [`pydantic_polars_boundary.md`](pydantic_polars_boundary.md).
- `tests/lint/test_determinism_invariants.py`: enforces the five requirements above (Polars pin, no module-level RNG, no `set` iteration in signal/policy, sorted-frame helper usage, Polars thread pool environment).
- `tests/data/test_tr_reconstruction.py`: the toy three-day fixture (no snapshot needed).
- `tests/integration/test_spy_one_quarter_preflight.py` and `tests/integration/test_spy_reconciliation.py`: gated on snapshot availability; skipped in CI with a clear "skipped: snapshot not available in CI" message until the v1.1 Git LFS work lands.
- `tests/integration/test_runner_determinism.py`: runs a small (3-name, 1-year) Runner-orchestrated CPCV with N=4, k=2 twice in succession, asserts the two runs produce identical `BacktestResult.model_dump_json()` strings.

The full M1 reconciliation gate runs locally and the PR description carries the evidence line above. Reviewers can re-run locally to verify.

## What this invariant catches

Concrete failures that the invariant catches early, before they reach the M5 worked study or any external reader of the engine:

1. **A Polars upgrade subtly reorders a `group_by` reduction.** The bit-identical test fails; the upgrade is gated behind a re-run of the SPY reconciliation and an explicit human decision.
2. **A new signal added in M5 silently uses `np.random.normal()` without injecting a Generator.** The lint test catches at PR time; the M5 momentum study cannot be merged until the violation is fixed.
3. **A Runner refactor accidentally enables Polars multi-threading inside worker processes.** The `test_runner_determinism.py` test detects the resulting non-determinism; the PR is rejected.
4. **A user passes a non-PIT alternative-data frame to a backtest.** The `additional_data` contract check fails at backtest construction; the user sees a diagnostic pointing at the boundary they violated.
5. **A drive-by `from itertools import product` import is replaced with a hash-randomized `set` comprehension.** The signal layer lint catches the `set` iteration.

The invariant's value is at moments like these: small changes that would otherwise drift the engine's behavior without anyone noticing.

## Cross-references

- ADR 0003 decision 11: the determinism invariant.
- ADR 0003 decision 12: the 11-item trust boundary list.
- ADR 0001 decisions 17 and 18: per-platform reproducibility; cross-platform is documented as a known limitation.
- [`docs/methodology/pydantic_polars_boundary.md`](pydantic_polars_boundary.md): the Pydantic boundary; contributes to determinism by removing Pydantic's per-instance validation from the inner loop.
- [`docs/methodology/dataset_versioning.md`](dataset_versioning.md): the snapshot SHA256 commitment; the data-layer instance of the determinism invariant.
- [`docs/methodology/total_return_reconstruction.md`](total_return_reconstruction.md): the M1 reconciliation that the invariant is sized to keep stable across runs.
