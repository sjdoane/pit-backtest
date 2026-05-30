# ADR 0015: `BacktestPathDistribution[T]` TypeVar Protocol bound, `BacktestResult.__lt__`, and `Split.test_groups` field

Status: Accepted.
Date: 2026-05-30.
Authors: Sam Doane (with M4 PR 3 Plan + Plan-reviewer pass; the Plan-reviewer surfaced the cascade through `analytics/distribution.py`, `analytics/scorecard.py`, and `validation/cv.py` that the original plan's "do it inline" choice did not address).

## Context

The M4 PR 2 implementation of `BacktestPathDistribution[T].percentiles` (PR #34, merged at `5e0e136`) shipped with `T = TypeVar("T")` UNBOUNDED at `src/pit_backtest/analytics/distribution.py:42`. The `sorted(self._paths)` call at `analytics/distribution.py:131` carries a `# type: ignore[type-var]` because mypy cannot prove an unbounded `T` is sortable, and the docstring at `analytics/distribution.py:9-21` documents the deviation: the bound was deferred because adding `bound=float` would have broken the existing `engine/runner.py:92` stub `run_cpcv -> BacktestPathDistribution[BacktestResult]`. The TODO at `analytics/distribution.py:126-128` reads:

```
# TODO(M4 PR 3): when the per-path emission type is fixed, swap
# the unbounded TypeVar for either `bound=float` or a
# `bound=SupportsRichComparison` Protocol and remove this ignore.
```

The M4 PR 3 Plan agent proposed bundling the TypeVar swap, the `BacktestResult.__lt__` definition, the `Split.test_groups` field addition, and the three splitter bodies into one PR.

The Plan-reviewer pass rejected the all-in-one approach on the facts: the contract change cascades across FOUR files (each with its own callers and tests), mirrors the ADR 0013 and ADR 0014 prep-PR precedents (PR #31 for PSR/DSR/MinTRL contract correction; PR #33 for `DrawdownDurationReport`), and the implementation-PR risk is high because a TypeVar bound error surfaces only at mypy time on the consuming PR's full edit surface. The reviewer also surfaced a CV-stub-docstring misattribution (`src/pit_backtest/validation/cv.py:5` reads "Per ADR 0003 decision 17" but the canonical decision is ADR 0002 decision 17; ADR 0003's decision 17 is "Single-currency USD assumption", unrelated) and a load-bearing `Split` shape gap (the future `Runner.run_cpcv` body needs to know which group each contiguous chunk of `Split.test_indices` came from to stitch per-fold test predictions into per-path equity curves).

This ADR locks the four contract changes so M4 PR 3b implements `PurgedKFoldSplitter`, `WalkForwardSplitter`, and `CPCVSplitter` against a frozen target with no cascade risk.

## Locked decisions

### `SupportsRichComparison` Protocol

1. **`SupportsRichComparison` is a `typing.Protocol` declared in `src/pit_backtest/analytics/distribution.py`** above the `T` TypeVar declaration. Shape:

```python
class SupportsRichComparison(Protocol):
    def __lt__(self, other: object, /) -> bool: ...
```

   Semantics: the Protocol is structural; any type with `__lt__` defined satisfies it. `float`, `int`, `Decimal`, and `BacktestResult` (per decision 3 below) all satisfy it. The Protocol is NOT `runtime_checkable`: there is no `isinstance(p, SupportsRichComparison)` check (the M4 PR 2 Plan-reviewer Medium 5 rejected `runtime_checkable Protocol Comparable` as over-engineered). mypy-time enforcement is the only enforcement; runtime errors from `sorted()` on a non-sortable `T` raise `TypeError` from the standard library, which the caller's contract is to avoid.

2. **`T = TypeVar("T", bound=SupportsRichComparison)`** replaces the unbounded `T = TypeVar("T")` at `analytics/distribution.py:42`. The `# type: ignore[type-var]` on `sorted(self._paths)` at `analytics/distribution.py:131` is removed; mypy now proves sortability from the bound.

### `BacktestResult.__lt__`

3. **`BacktestResult.__lt__` is defined on the Pydantic v2 model at `src/pit_backtest/analytics/scorecard.py:92-119`** with `self.sr_hat: float` as the ordering key. Implementation:

```python
def __lt__(self, other: object) -> bool:
    if not isinstance(other, BacktestResult):
        return NotImplemented
    return self.sr_hat < other.sr_hat
```

   Justification for `sr_hat` as the ordering key:
   - LdP 2018 chapter 13 + 14 use per-path Sharpe as the canonical CPCV path-ranking surface. The methodology research note at `docs/research/sources/methodology-afml-backtesting.md:177-181` quotes the convention: "Computation of the 10th-percentile SR as a worst-case estimate". The `BacktestPathDistribution.p10()` consumer accesses this rank via `sorted()`.
   - Pydantic v2 frozen models accept user-defined `__lt__` without breaking the auto-generated `__hash__` and `__eq__` methods (empirically verified at Pydantic v2.13.4 in the project venv). The model's `frozen=True` configuration locks `__setattr__` and `__delattr__` only; method addition on the class is orthogonal. Orthogonally and pre-existing to ADR 0015, `BacktestResult` instances are NOT hashable at runtime because the nested `Attribution.by_year: dict[int, float]` field is itself unhashable (Python's stdlib unhashable-dict rule cascades through Pydantic's auto-`__hash__`). A future consumer that needs `BacktestResult` in a `set` or as a `dict` key must replace `dict` with a tuple-of-pairs at the `Attribution` boundary; out of scope for ADR 0015.
   - `NotImplemented` is returned for cross-type comparisons so Python's comparison machinery raises `TypeError` cleanly rather than silently returning a misleading bool (matches the Python data-model convention for `__lt__`).

4. **The Pydantic `model_config` is unchanged.** No `arbitrary_types_allowed`, no `frozen` toggle. `__lt__` is a Python method on the class, not a Pydantic field; the existing `_SCORECARD_CONFIG = ConfigDict(frozen=True, arbitrary_types_allowed=True)` at `scorecard.py:17` carries through.

### NaN-guard contract obligation

5. **The `BacktestPathDistribution.__init__` NaN guard at `analytics/distribution.py:79-93` is unchanged** by this ADR. The guard still fires only for `T = float` via `isinstance(p, float) and math.isnan(p)`. For `BacktestResult`, NaN in `sr_hat` is undefined under the sort path; the contract obligation (per the M4 PR 2 post-impl reviewer H2 finding and the docstring at `analytics/distribution.py:75-86`) is on the call site that constructs `BacktestPathDistribution[BacktestResult]` to gate `result.sr_hat == math.nan` before construction.

   The Runner.run_cpcv body (deferred to a later M4 PR) is the canonical call site; this ADR documents the obligation but does NOT add the runtime gate to `BacktestPathDistribution.__init__`. Adding it would couple `analytics/distribution.py` to `analytics/scorecard.py` (the consumer's specific field), which violates the analytics-module-split layering at ADR 0003 dec 14.

### `Split.test_groups` field

6. **`Split` at `src/pit_backtest/validation/cv.py:17-24` is widened with a fifth tuple field `test_groups: tuple[int, ...]`** carrying the group indices that the `test_indices` chunks came from. New shape:

```python
@attrs.frozen(slots=True)
class Split:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]
    embargo_indices: tuple[int, ...]
    test_groups: tuple[int, ...]
```

   Semantics per splitter (locked here; bodies implemented in M4 PR 3b):
   - `PurgedKFoldSplitter`: `test_groups` is a length-1 tuple `(fold_index,)` (each Split tests exactly one fold).
   - `WalkForwardSplitter`: `test_groups` is the empty tuple `()` (single-window walk-forward has no group structure; ADR 0002 dec 17 frames it as a "sanity-check baseline").
   - `CPCVSplitter`: `test_groups` is a length-`k_test` tuple of the held-out group indices in ascending order (matching the `sorted(itertools.combinations(range(n_groups), k_test))` enumeration order). This is the field the Runner.run_cpcv body reads to join per-fold `BacktestResult`s into per-path equity curves via `CPCVSplitter.path_assignments()`.

7. **`Split.test_groups` ordering invariant**: the tuple is always sorted ascending (the implementation pulls from `itertools.combinations`, which produces lexicographic-sorted tuples). The test suite pins this in M4 PR 3b.

### `validation/cv.py` ADR misattribution fix

8. **`src/pit_backtest/validation/cv.py` docstring ADR misattribution corrected at two sites**: the module docstring at line 5 and the `WalkForwardSplitter` class docstring at line 76 both formerly read "ADR 0003 decision 17". Both now correctly read "ADR 0002 decision 17". ADR 0003 decision 17 is "Single-currency USD assumption" (`docs/decisions/0003-architecture.md:890`); ADR 0002 decision 17 is the canonical WalkForwardSplitter binding (`docs/decisions/0002-roadmap-review.md:303`). The cross-reference at `docs/decisions/0003-architecture.md:583` correctly cites ADR 0002 dec 17, confirming the canonical location. The two-site fix was surfaced by the prep PR 3a post-impl reviewer; the original ADR text mentioned only line 5.

## What this ADR does NOT do

- Does NOT implement `PurgedKFoldSplitter`, `WalkForwardSplitter`, or `CPCVSplitter` bodies. The three splitter stubs stay as `NotImplementedError("M4 deliverable")` after this ADR's prep PR lands; M4 PR 3b implements them against the now-locked `Split` shape.
- Does NOT implement `Runner.run_cpcv`. The stub at `src/pit_backtest/engine/runner.py:88-93` stays as `NotImplementedError("M4 deliverable")`. A subsequent M4 PR (3c or 5) lands the body; this ADR ensures the body can read `Split.test_groups` and call `CPCVSplitter.path_assignments()` without further contract churn.
- Does NOT add `CPCVSplitter.path_assignments()` or `CPCVSplitter.expected_path_count()` methods. Those land in M4 PR 3b alongside the splitter bodies; the methods' shapes are documented in the M4 PR 3 plan but not locked here. M4 PR 3b is free to refine the shapes as implementation reveals edge cases (e.g., `expected_path_count()` may become a classmethod if it turns out to be a pure function of construction args).
- Does NOT touch `trial_registry.py`, `analytics/scorecard.py::to_markdown()`, or `confidence_tier.py`. Those land in subsequent M4 PRs against this ADR's frozen `BacktestPathDistribution[T]` contract.
- Does NOT make `SupportsRichComparison` a `runtime_checkable` Protocol. The decision matches the M4 PR 2 Plan-reviewer Medium 5 rejection of `runtime_checkable Protocol Comparable`; runtime sortability errors raise from `sorted()` at the call site, not from a redundant `isinstance` gate.
- Does NOT introduce `__le__`, `__gt__`, `__ge__`, `__eq__`, or `__hash__` on `BacktestResult`. Only `__lt__` is required for `sorted()`; the existing Pydantic-generated `__eq__` is sufficient for sort stability and unchanged. The auto-generated `__hash__` exists but is non-functional at runtime because `Attribution.by_year: dict[int, float]` is unhashable (pre-existing; flagged in decision 3); ADR 0015 does NOT fix the hashability gap, which remains out of scope. Sorting via `sorted()` only requires `__lt__`; the rest of the rich-comparison set is unused.
- Does NOT amend ADR 0001, ADR 0002, ADR 0003, ADR 0013, or ADR 0014. The corrections in this ADR are localized to `validation/cv.py:5` (docstring fix) and the analytics+validation module shapes.
- Does NOT add scipy, numpy, or scikit-learn to `pyproject.toml`. ADR 0013 decision 11's stdlib-only constraint for analytics stands; ADR 0001 dec 3's stdlib-plus-Polars constraint for validation stands.

## Cross-references

- ADR 0001 decision 3 (CPCV primary; walk-forward as a CPCV(N=T, k=1) configuration). UNCHANGED.
- ADR 0001 decision 4 (PSR/DSR/MinTRL non-optional; the analytics layer is the LdP chapter 14 scorecard surface). UNCHANGED.
- ADR 0002 decision 2 (M4 acceptance criterion: CPCVSplitter(N=6, k=2) yields 5 paths). UNCHANGED.
- ADR 0002 decision 17 (`WalkForwardSplitter` as a separate primitive alongside `PurgedKFoldSplitter` and `CPCVSplitter`; the canonical attribution that `validation/cv.py:5` now points at correctly). UNCHANGED.
- ADR 0003 decision 14 (analytics module split). UNCHANGED.
- ADR 0013 decision 7 (loud-failure discipline; any domain violation raises `ValueError`). UNCHANGED.
- ADR 0013 decision 11 (no scipy in analytics; stdlib-only Phi via `math.erf`, Phi_inv via Acklam 1998). UNCHANGED.
- ADR 0013 + ADR 0014: precedent for the prep-PR-before-implementation cascade pattern this ADR follows.
- `docs/research/sources/methodology-afml-backtesting.md:120-200` (LdP 2018 ch 7 + 12 purged-k-fold + CPCV pseudocode).
- `docs/research/sources/methodology-afml-backtesting.md:177-181` (per-path SR ranking convention; the `sr_hat` ordering-key justification).
- `src/pit_backtest/analytics/distribution.py:9-21` (M4 PR 2 docstring that documented the unbounded TypeVar as a deferral).
- `src/pit_backtest/analytics/distribution.py:75-86` (M4 PR 2 post-impl reviewer H2 NaN-guard contract obligation, unchanged by this ADR).
- `src/pit_backtest/analytics/distribution.py:126-128` (the TODO(M4 PR 3) the swap fulfills).
- `src/pit_backtest/analytics/scorecard.py:92-119` (the `BacktestResult` model where `__lt__` lands).
- `src/pit_backtest/validation/cv.py:5` (the docstring ADR misattribution fix).
- `src/pit_backtest/validation/cv.py:17-24` (the `Split` record where `test_groups` lands).
- `src/pit_backtest/engine/runner.py:88-93` (the future Runner.run_cpcv body that reads `Split.test_groups`).
- `docs/methodology/pydantic_polars_boundary.md` (the Pydantic-at-boundary convention that admits `BacktestResult.__lt__` without violating the model-as-record contract).

## Status

Accepted. M4 PR 3b implements `PurgedKFoldSplitter`, `WalkForwardSplitter`, and `CPCVSplitter` against the `Split` shape and the bounded `BacktestPathDistribution[T]` contract locked above. Revisiting any decision requires a superseding ADR.
