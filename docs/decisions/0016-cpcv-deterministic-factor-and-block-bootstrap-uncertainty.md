# ADR 0016: `Runner.run_cpcv` redesign, CPCV degeneracy for deterministic factors, and block-bootstrap path uncertainty

Status: Accepted.
Date: 2026-05-31.
Authors: Sam Doane (with the M5 Plan + Plan-reviewer pass, and a CPCV-degeneracy finding surfaced while drafting this ADR that reshaped the M5 uncertainty methodology; Sam ratified the block-bootstrap direction).

## Context

`Runner.run_cpcv` was deferred from M4 to M5 (ADR 0003 M4 PR 5 amendment footer). The stub at `src/pit_backtest/engine/runner.py:88-107` is underspecified: it takes `(cv_splitter, bar_loop_factory)` with no `observations`/`label_horizons` (so it cannot call `cv_splitter.split()`) and a no-arg `bar_loop_factory` (so it cannot be parameterized per split). M5 must redesign the signature and implement the body.

While drafting the redesign, a conceptual problem surfaced that changes what `run_cpcv` should produce for the M5 study, and how the study quantifies uncertainty.

**CPCV path dispersion comes from model-fit variance, which a deterministic factor does not have.** Combinatorial Purged Cross-Validation (Lopez de Prado 2018, AFML chapter 12; `docs/research/sources/methodology-afml-backtesting.md:120-200`) generates `phi(N, k) = (k/N) * C(N, k)` backtest paths whose dispersion, in the ML setting the method was designed for, arises because the model is RETRAINED on each combination's train fold, so different train folds produce different test-fold predictions. The M5 worked study is single-factor JT1993 12-1 momentum (ADR 0002 dec 20): at each monthly rebalance the score is a deterministic function of PIT prices (`Momentum12_1Signal.compute`, M5 PR 1 / PR #39) and the top-quintile selection is mechanical. There is no fitted model. Consequently:

- The per-month out-of-sample return of the strategy is identical regardless of which groups are train vs test (purge/embargo remove only TRAINING observations adjacent to the test set; they never alter the test returns).
- The `phi(N, k)` reconstructed full-period paths, each tiling the full timeline from per-group test segments via `CPCVSplitter.path_assignments()`, are therefore IDENTICAL for a deterministic factor.
- A literal CPCV fan chart of those paths is a single line. Presenting it as a distribution would misrepresent the methodology, which violates the project's intellectual-honesty thesis (ADR 0002's "checkboxes that survive scrutiny").

The meaningful uncertainty for a deterministic factor lives in: (1) the sampling distribution of the Sharpe given the finite realized return series, captured rigorously by PSR/DSR (M4 PR 1; `analytics/sharpe.py`); (2) the cost-sensitivity band (M2 PR C1; `analytics/sensitivity.py`); (3) the year-by-year regime decomposition; and (4) genuine path uncertainty via a STATIONARY BLOCK BOOTSTRAP of the return series, which preserves short-range serial dependence and is the statistically correct path-uncertainty tool for a strategy with no estimated parameters.

This ADR locks the `run_cpcv` contract, scopes its M5 semantics, records the degeneracy finding, and adds the block-bootstrap module as the study's headline path-uncertainty deliverable. M5 PR 2 implements `run_cpcv` + the bootstrap against this frozen contract.

## Locked decisions

### `Runner.run_cpcv` signature

1. **`Runner.run_cpcv` is redesigned to the following signature** at `src/pit_backtest/engine/runner.py`:

```python
def run_cpcv(
    self,
    cv_splitter: CPCVSplitter,
    observations: pl.DataFrame,                       # one row per rebalance; carries a 'dt' (pl.Date) column, sorted ascending
    label_horizons: pl.Series,                        # per-observation label end date (pl.Date), for purge/embargo
    bar_loop_factory: Callable[[date, date], BarLoop],  # builds a backtest scoped to a contiguous [start_dt, end_dt] window
) -> BacktestPathDistribution[BacktestResult]:
```

   The two stub defects are fixed: `observations` + `label_horizons` are now present (so `cv_splitter.split(observations, label_horizons)` is callable), and the factory is parameterized by a contiguous date window (replacing the no-arg factory). The factory takes `(start_dt, end_dt)` rather than a `Split` because the M5 execution model runs the strategy per contiguous GROUP (decision 2), and a window is the natural unit; the richer per-`Split` factory an ML strategy would need is deferred (decision 7).

### CPCV semantics for a deterministic factor (M5 scope)

2. **`run_cpcv` evaluates the strategy per contiguous group and stitches per-path equity curves via `path_assignments()`.** The mechanics locked here (precise Polars details deferred to PR 2):
   - `cv_splitter.split(observations, label_horizons)` yields `C(N, k)` `Split`s; the N contiguous groups partition `observations`, and each group `g` maps to the contiguous date window `[dt of g's first observation, dt of g's last observation]`.
   - For each group `g`, `bar_loop_factory(group_start_dt, group_end_dt).run(...)` produces a per-group out-of-sample equity segment. Observations in a combination's `purged_indices` / `embargo_indices` are excluded from contributing to the stitched curve (they are the leaked-bar boundary that CPCV exists to remove).
   - **Per-combination-to-per-group collapse (Plan-reviewer F2; the body runs N group-backtests, NOT C(N, k)).** `cv_splitter.path_assignments()` maps each of the `phi(N, k)` paths to one combination index per group. For a deterministic factor the segment named by `path_assignments()[j][g]` equals the group-`g` segment for EVERY combination (decision 4), so the body runs exactly N group-backtests, builds a `group_index -> segment` map, and for path `j` concatenates `segment[g]` for `g` in `0..N-1`. The `path_assignments` combination indices are used ONLY as a correctness cross-check that every path tiles all N groups exactly once (the cell-partition invariant from M4 PR 3b); they do NOT select distinct segments. An implementer must not run the `C(N, k)` combination-backtests (15 for N=6 k=2) and wonder why they are redundant; they are redundant precisely because the strategy is deterministic.
   - **Seam-cost artifact (Plan-reviewer F1; the stitched path is NOT the return-level reference).** Each per-group `BarLoop` initializes its `PortfolioState` to all-cash at `group_start_dt` (`bar_loop.py:160-165`), so the stitched CPCV path charges a full liquidation plus full re-entry at each of the N-1 group seams (5 phantom round-trips for N=6). A single contiguous full-period backtest instead trades only the low-turnover month-boundary delta. This boundary turnover is a stitching artifact, NOT genuine path uncertainty; it is IDENTICAL across all `phi` paths (so the degeneracy of decision 4 is unaffected), but it biases the CPCV per-path return LEVEL downward versus the real strategy, and the bias scales with N and the commission rate. The headline study therefore takes the CONTIGUOUS full-period backtest as the return-level and Sharpe reference; the CPCV run is reported per decision 4 for its (near-zero) dispersion, not for its level. PR 2/PR 3 must not mistake the CPCV-below-contiguous Sharpe gap for a bug.
   - Each stitched per-path equity curve becomes a `BacktestResult` via the existing `analytics.result_adapter.to_backtest_result` adapter (M4 PR 5), fed a per-path `ConstantWeightDemoResult` wrapper constructed from the stitched curve (the wrapper is the right shape already; no retype cascade).

3. **`run_cpcv` returns `BacktestPathDistribution[BacktestResult]` with the NaN gate honored at construction.** Per the M4 PR 2 post-impl reviewer H2 obligation and ADR 0015 decision 5, the call site (here) gates any path whose `BacktestResult.sr_hat` is NaN before constructing the distribution; `analytics/distribution.py` is not coupled to the scorecard field.

4. **CPCV is the wrong path-uncertainty tool for a deterministic single-factor strategy; the study states this outright, and `run_cpcv` reports the degeneracy rather than hiding it.** The M5 study leads with the tool-mismatch: CPCV's `phi(N, k)` reconstructed paths coincide for momentum because there is no estimated parameter whose retraining could vary the paths, so CPCV cannot produce a meaningful fan here. The study runs `run_cpcv` on momentum, observes the near-zero path dispersion (the paths differ at most through floating-point reassociation of identical segments plus the constant seam-cost bias of decision 2), and reports it as an instructive finding rather than dressing a single line up as a distribution. This is a passing-milestone result that demonstrates correct understanding of CPCV's scope, NOT a failure. `run_cpcv` remains a real, tested engine capability that exists for the future ML-strategy case, where retrained-model variance makes the path distribution non-degenerate; it is built and exercised now so the capability is in place and so the deterministic-factor degeneracy can be shown empirically rather than merely asserted.

### Block-bootstrap path uncertainty (the study's headline fan)

5. **A new module `src/pit_backtest/analytics/bootstrap.py` provides a stationary block bootstrap** (Politis-Romano 1994) as the study's genuine path-uncertainty tool. Locked contract:

```python
def stationary_block_bootstrap(
    returns: Sequence[float],
    n_paths: int,
    *,
    expected_block_length: float,
    seed: int,
) -> list[list[float]]:
    ...
```

   - Resamples the per-period return series in geometrically-distributed-length blocks (mean `expected_block_length`) to generate `n_paths` synthetic return sequences of the same length, preserving short-range serial dependence (momentum returns are autocorrelated, so an iid bootstrap would understate path variance). The stationary variant is chosen over the moving-block (Kunsch 1989) and circular-block bootstraps because its random geometric block lengths make the resampled series strictly stationary, avoiding the fixed-block-length artifacts of the former and the period-wrap distortion of the latter.
   - Determinism: uses Python stdlib `random.Random(seed)` (no numpy/scipy; consistent with the analytics layer's stdlib-only discipline at ADR 0013 dec 11, and with the determinism requirement that randomness be explicitly seeded). The geometric block continuation is a single `rng.random() < p` test with `p = 1 / expected_block_length`; the uniform start is `rng.randrange(n)` with wrap-around. These two draws are the only randomness.
   - Loud-fail per ADR 0013 dec 7: raises `ValueError` on empty `returns`, `n_paths < 1`, `expected_block_length <= 1.0` (`p = 1/L` must be in `(0, 1)`; `L = 1` degenerates to the iid bootstrap), or `seed` of a non-int.
   - **Block-length selection (Plan-reviewer F3)**: the principled choice of `expected_block_length` for a serially-dependent series is the Politis-White (2004) automatic selection. M5 does NOT implement automatic selection; PR 3 MUST document its chosen `expected_block_length` and the justification (tied to the momentum return autocorrelation horizon, e.g. a small multiple of the monthly rebalance), so the study reports a defended value rather than an unjustified magic number. Automatic selection is a v1.1 refinement.
   - The synthetic sequences feed an equity-path fan chart (M5 PR 4) and a bootstrap Sharpe distribution that complements PSR/DSR.

6. **The M5 study's uncertainty methodology is locked as: PSR/DSR (sampling) + the stationary block-bootstrap fan (path) + the cost-sensitivity band (cost) + the year-by-year regime decomposition.** CPCV is run and reported per decision 4, but it is NOT the headline uncertainty surface for the deterministic momentum factor. The honest DSR conclusion (ADR 0002 M5 acceptance: passing whether or not the strategy clears DSR >= 0.95) is computed from the single realized return series via the trial registry (M4 PR 4) feeding `dsr`.

## What this ADR does NOT do

- Does NOT implement `run_cpcv`, `TopQuintileLongPolicy`, the real `PitView` BarLoop wiring, or `stationary_block_bootstrap`. Those land in M5 PR 2 against this frozen contract.
- Does NOT generalize `run_cpcv` to the ML per-combination-fit case. M5's strategy is deterministic; the per-`Split` factory and per-combination model training that an ML strategy needs are a future extension (a superseding ADR when an ML strategy is added). The signature's `Callable[[date, date], BarLoop]` factory is deliberately the deterministic-window shape. **Adding an ML strategy later is an ACCEPTED future breaking change** (the window factory cannot express a per-combination model fit; that needs `Callable[[Split], BarLoop]` or a fit/predict pair). Deferring is correct rather than churn-inviting: no ML strategy is on any current roadmap, and future-proofing the signature now would bake in an unused `Split` dependency plus a fit/predict protocol with zero test coverage, which is exactly the speculative generality the M4 PR 5 deferral was avoiding. The narrow signature is locked deliberately, not by oversight.
- Does NOT remove or weaken CPCV. `CPCVSplitter` (M4 PR 3b), `path_assignments`, and the M4 acceptance (N=6 k=2 -> 5 paths as `BacktestPathDistribution`) all stand. `run_cpcv` is built and tested; decision 4 only governs how its OUTPUT is reported for a deterministic factor.
- Does NOT add numpy, scipy, or scikit-learn to the analytics layer. The block bootstrap is stdlib `random`-only. ADR 0013 dec 11 stands.
- Does NOT change PSR/DSR/MinTRL, the trial registry, the scorecard, or the cost-sensitivity band. The study composes the existing M2/M4 surfaces.

## Cross-references

- ADR 0001 dec 3 (CPCV primary; walk-forward as CPCV(N=T, k=1)). UNCHANGED; this ADR scopes how CPCV output is interpreted for a deterministic factor.
- ADR 0002 dec 20 (single-factor JT1993 momentum worked study). The study this ADR's methodology serves.
- ADR 0002 M5 acceptance (momentum study Markdown with the honest DSR conclusion; CPCV fan chart). The fan chart is now the block-bootstrap fan per decision 5; CPCV is reported per decision 4.
- ADR 0003 M4 PR 5 amendment footer (the run_cpcv deferral this ADR resolves).
- ADR 0013 dec 7 (loud-failure) + dec 11 (stdlib-only analytics; the block bootstrap honors it via stdlib `random`).
- ADR 0015 dec 5 (the NaN-gate contract obligation at the `BacktestPathDistribution[BacktestResult]` construction site, which `run_cpcv` is).
- `analytics/result_adapter.py` (M4 PR 5; the equity-curve -> BacktestResult adapter `run_cpcv` reuses per path).
- `validation/cv.py` (M4 PR 3b; `CPCVSplitter.split`, `path_assignments`, `expected_path_count`).
- `docs/research/sources/methodology-afml-backtesting.md:120-200` (CPCV pseudocode; the source whose ML-strategy scope this ADR makes explicit for the deterministic case).
- Politis, D. N. and Romano, J. P. (1994), "The Stationary Bootstrap", JASA 89(428):1303-1313 (the block-bootstrap method decision 5 implements).

## Status

Accepted. M5 PR 2 implements `Runner.run_cpcv`, `TopQuintileLongPolicy`, the real `PitView` BarLoop wiring, and `analytics/bootstrap.py::stationary_block_bootstrap` against the contract locked above. The M5 study (PR 3) composes them with PSR/DSR + the cost band + year-by-year into the honest momentum report. Revisiting any decision requires a superseding ADR.

## Amendment 2026-06-01 (M5 PR 2c implementation)

Recorded when implementing the `Runner.run_cpcv` body. The four ADR-locked
positionals and the CPCV semantics (decisions 1 to 4) are unchanged; this
amendment records the small additions the body required.

1. Signature drift (keyword-only analytics parameters). The implemented
   signature carries the four locked positionals (`cv_splitter`,
   `observations`, `label_horizons`, `bar_loop_factory`) PLUS four keyword-only
   parameters: `registry: TrialRegistry`, `strategy_family: str`,
   `universe_id: str`, and `periods_per_year: int = 252`. Justified by
   decision 3: the body feeds each stitched per-path equity curve to
   `analytics.result_adapter.to_backtest_result`, which requires a registry
   (the DSR record-then-query), a strategy family and universe id (the registry
   partition keys), and a periods-per-year for the annualization. These were
   not enumerable when decision 1 fixed the four-positional shape. Note that
   `label_horizons` is validated by `cv_splitter.split` but does NOT affect the
   deterministic-factor output (the embargo-invariance test proves this); it is
   the purge/embargo input for the future ML per-combination-fit case
   (decision 7), which `run_cpcv` does not run.

2. Registry namespacing for the CPCV-path trials (resolves the Plan-reviewer
   Critical 2). The phi reconstructed paths are byte-identical for a
   deterministic factor (decision 4), so their `sr_hat` values are identical
   and their within-family Sharpe variance `v_sr` is exactly 0. Were those path
   trials recorded into the study's DSR family, a later query of that family
   with `naive_effective_n > 1` would read a near-zero `v_sr` and INFLATE the
   Deflated Sharpe Ratio (a smaller cross-sectional variance deflates less),
   corrupting the study's headline honesty metric. `run_cpcv` therefore isolates
   the path trials: it opens a derived sibling `TrialRegistry` over the SAME db
   file as the passed registry (`TrialRegistry(registry.db_path,
   naive_effective_n=1)`) and records each path under the namespaced family
   `f"{strategy_family}::cpcv_paths"`. The forced `naive_effective_n=1`
   degenerates the per-path DSR query to PSR and sidesteps the
   single-trial-with-`naive>1` loud failure in `effective_n_and_sr_variance`;
   the `::cpcv_paths` namespace keeps the study family's `(n_effective, v_sr)`
   untouched (isolation is by `strategy_family`, since the path trials share the
   study's `dataset_fingerprint`, which is `demo.sharadar_bundle`). A public
   `TrialRegistry.db_path` property was added to support this.

3. Helper promotions. The private contiguous-fold helper `_contiguous_folds`
   was promoted to public `contiguous_folds` in `validation/cv.py` (the ADR and
   PR spec both referred to it without the underscore), and a public
   `CPCVSplitter.n_groups` property was added, so `run_cpcv` derives the N
   per-group windows from the same source of truth the splitter uses internally
   rather than re-implementing the remainder-front partition.

4. Seam-cost honesty (zero-cost fixture scope). Decision 2's seam-cost artifact
   (each per-group BarLoop re-enters from all-cash, charging a commission seam
   that biases the CPCV path level below a contiguous run) is NOT demonstrated
   in PR 2c: the unit tests wire the zero-cost `CloseFillMatchingEngine`, so the
   only stitched-vs-contiguous difference is the omitted inter-group gap-day
   bars, NOT a commission bias. The `_stitch_path` unit test pins the
   running-level carry and the injected 0% seam return (the engine-agnostic
   invariant). The commission seam artifact and the contiguous full-period level
   reference are PR 3 deliverables against the real cost-bearing bundle, per the
   decision 2 warning not to mistake (or manufacture) the CPCV-below-contiguous
   gap.

## Amendment 2026-06-01 (M5 PR 3a: the signal-compute perf gate)

Recorded when adding the `BarLoop.signal_calendar` performance gate, the
prerequisite for tractably running the real S&P 500 momentum study (PR 3b).

Under `use_real_pit_view=True` the BarLoop rebuilt the PitView and called
`signal.compute` on every trading day, but the rebalance policy no-ops off its
own monthly calendar, so ~95% of those computes (each a full-SEP-slice PitView
rebuild plus a per-member total-return reconstruction) were wasted. On the real
502-name 2005-2024 universe a single `signal.compute` measures ~2.8 s, so the
un-gated run is ~3.9 hours of signal compute versus ~11 minutes gated (a ~21x
reduction; measured on `sharadar_2026-05-31`).

The gate is an additive keyword-only constructor flag
`signal_calendar: frozenset[date] | None = None`. When `None` (the default) the
signal fires every bar (M1/M2/PR-2c behavior, byte-identical). When set (the
study passes the policy's rebalance calendar) the PitView rebuild and
`signal.compute` fire ONLY on calendar bars; off-calendar bars reuse the prior
`signal_output`.

LOAD-BEARING INVARIANT (the actual contract, not merely "the policy no-ops off
its calendar"): `signal_calendar`, when set, MUST be a SUPERSET of every bar on
which the policy can emit non-empty targets. The gate is behavior-preserving
precisely because, when this holds, the off-calendar `signal_output` is never
consumed by an order (the policy returns empty targets off its trade calendar,
so the order block is skipped regardless of `signal_output`). If a caller passes
a calendar that omits a bar the policy trades on, the gate would skip the signal
that should have driven that trade and silently change the equity curve;
`BarLoop.run` therefore RAISES loudly (`RuntimeError`) the moment the policy
emits non-empty targets on a non-signal bar. This converts the latent trap into
a loud failure and makes the flag safe for general use.

This is an additive, default-off, behavior-preserving change to the BarLoop
public constructor, recorded as a footer rather than a standalone ADR (the
precedent: ADR 0017 universe rework, M3 PR 1-3). It does NOT weaken ADR 0004
(the policy still owns the rebalance calendar; the flag is a caller-supplied
performance hint). The byte-identical guarantee, the compute-only-on-calendar
reduction, and the loud guard are pinned by
`tests/engine/test_bar_loop_signal_gate.py`.
