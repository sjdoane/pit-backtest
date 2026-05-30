# ADR 0012: Perf-budget CI Phase 0 (noise-floor measurement)

Status: Accepted.
Date: 2026-05-30.
Authors: Sam Doane (via 4-member parallel council + verifier pass per session_rules.md rule 1).

## Context

ADR 0005 step 16 commits M2 PR D to ship `src/pit_backtest/bench/spy_20y.py` (synthetic data per lock #11), `src/pit_backtest/bench/compare.py`, `.github/workflows/perf-budget.yml`, `.bench-baseline.json`, the `--impact-model=bouchaud` CLI flag, and `BarLoop.timing_breakdown()` opt-in instrumentation per lock #12. README.md:52 already publicly commits: "tracked in CI; any regression over 10% fails the build."

Two sub-decisions inside that scope are architectural and not predetermined by ADR 0005:

**Decision A (CI workflow trigger + matrix + failure mode)**:
- A1: every PR, ubuntu-latest, comment delta, fail >10%
- A2: only push-to-main, ubuntu-latest, issue on >10%
- A3: matrix Linux + Windows on every PR, fail any platform regression
- A4 (kill): no CI; local-run gate per ADR 0005 step 11

**Decision B (baseline-bump procedure)**:
- B1 (manual PR with justification)
- B2 (auto-faster, manual-slower; bot writes)
- B3 (time-locked recalibration; 30-day lock + 7-run median)
- B4 (no baseline)

Per session_rules.md rule 1 ("for decisions, spawn a 3-4 member council + verifier") and Sam's 2026-05-29 EOD+3 reinforcement, the decision was directed to a 4-member council (Realist / Quant / Builder / Growth) + Verifier. Council voted **A1=3, A2=1** (Quant; with A4 caveat) and **B1=2, B2=1, B3=1**. The Verifier synthesized to **HYBRID A1 + B1 with Phase 0 / Phase 1 split**.

## Council vote (condensed)

### Realist: A1 + B1

> "Ship the boring version. A1 is the only option that catches regressions where humans can actually fix them: in the PR diff. A2 is 'let it land then bisect' which I've been paged into at 3am twice. A3 is bait: Windows runners are 2x slower and 3x flakier; the project's `determinism.md` already flags cross-platform as a caveat. B1: a bot rewriting (B2) is how baselines silently drift 30% slower over six months because nobody noticed each 4% 'improvement' was actually noise pushing the floor up after a real regression. B3 has the same drift pathology dressed up as policy."

Required additions: justify 10% threshold against measured variance; warmup + median-of-N (not single-shot); pinned runner image SHA; baseline-bump PR template; escape-hatch label; quarterly review.

### Quant: A2 + B3 (with A4 honest-answer caveat)

> "Ubuntu-latest runners have empirical CoV in the 5-15% range; a >10% threshold against a single-sample baseline sits at or below the noise floor. Single-run power to detect a true 10% mean shift is well under 50%. A2 reduces trial count ~10x vs A1, attenuating the multiple-testing problem the Bailey-Lopez de Prado framework is built to expose: at 200 PRs/year and 10% Type-I per run, A1's expected false-positive count is ~20/year. B3 borrows from SPC/CUSUM: a 30-day window of 7-run medians produces a robust location estimator whose standard error shrinks as 1/sqrt(7). B2 has the asymmetry-bias pathology BLP flag in Section 4."

Counter-arg: "A2+B3 measures something the determinism invariant explicitly disavows: Requirement 5 commits to bit-identical OUTPUTS, not timing. A4 (no CI; local-run gate per ADR 0005 step 11) is the honest answer; defending A2 over A4 requires conceding the perf budget is a coarse smoke test for catastrophic regressions, not a scientifically calibrated gate."

### Builder: A1 + B1

> "Ship the boring version. A1 buys real protection at ~90s per push, cheap on a public repo. A3's Windows matrix doubles wall-clock for marginal coverage: polars==1.41.1 + numpy==1.26.4 on 3.11 behave identically across glibc and MSVC for the timing critical path. A2 is too lenient. A4 abdicates the gate. B1 is the only option whose blast radius is exactly the blast radius of a normal PR review. B2 needs write-scoped credentials. B3 silently rebases real regressions into the new 'normal' without anyone signing the slip."

Required additions: `bench/compare.py` exit 0 if baseline missing (first-PR bootstrap); workflow `paths:` filter excludes `docs/**` and `*.md`; `actions/cache@v4` keyed on `hashFiles('uv.lock', 'pyproject.toml')`; `BarLoop.timing_breakdown()` returns `list[tuple[str, float]]` sorted by step name; baseline-bump PR template; kill switch via repo settings.

### Growth: A1 + B2 (with noise-floor precondition)

> "The README already promises CI enforcement of the 10% perf budget in the public Status block. A4 retroactively breaks that promise; A2 hides it from PR review; A3 doubles cost to demonstrate a Windows posture the recruiter cannot interpret. A1 is the lowest-cost path that makes the README sentence true and visible. A green 'perf-budget' check sitting next to a merged PR titled 'ImpactedPriceSource wiring' is the single richest 3-second signal Sam can give."

Counter-arg (binding precondition): "If the synthetic harness has any noise floor above the 10% gate, A1 produces flaky red PRs that signal the OPPOSITE of discipline. The honest version requires Sam to publish the noise floor (7 runs, stdev) and choose a fail threshold strictly outside it; the kill-early case for A4 is real if the synthetic harness cannot hold a tight noise floor. Document the gate and skip the workflow rather than ship a flaky one."

Required additions: noise floor first (publish 7-run stdev in `.bench-baseline.json`; require fail threshold > 2 sigma; no gate ships until floor is measured); Bouchaud flag in `examples/spy_cost_sensitivity.py` not sibling demo; README "CI and perf budget" subsection; scorecard prints active `--impact-model` and timing breakdown; B2 bot may only widen tolerances faster.

## Verifier's synthesis (HYBRID Phase 0 / Phase 1)

The Verifier checked premises empirically:

- **README:52 IS load-bearing**. A4 silently breaks the promise; rewriting it in the same PR to retract is a worse recruiter signal than the flaky-badge risk Growth flagged.
- **Quant's CoV claim (5-15%) is directionally credible but is at the LOW end for this workload**. A 60-90s CPU-bound NumPy/Polars loop is on the low-variance tail; the dominant source on shared runners is cold-start scheduler jitter in the first 10-15s, which a warmup run eliminates almost entirely.
- **Builder's `actions/cache@v4` strategy is correct**. `pyproject.toml:14-27` pins exact patches (polars==1.41.1, numpy==1.26.4); a content hash of `uv.lock + pyproject.toml` is a perfect cache key.
- **BarLoop sorted-iteration discipline supports `timing_breakdown` as sorted list**. `src/pit_backtest/engine/bar_loop.py` already wraps every dict traversal in `sorted(...)`; a `list[tuple[str, float]]` sorted by step name is consistent. The Quant is right that perf is OUT of Requirement 5 (timing values are NOT in the bit-identical output set), which means `timing_breakdown` is FREE to be non-deterministic without violating the invariant, but the perf gate is legitimate as a smoke-test gate.

### What 3 of 4 council members missed

1. The Realist's "median-of-N" and Growth's "publish 7-run stdev" are the SAME recommendation in two registers: one is the test statistic, the other is the calibration data. They unify cleanly.
2. The Builder's `paths:` filter (skip docs-only PRs) drew zero objection across four reviewers. Lock it.
3. The Bouchaud flag belongs in a SEPARATE binding (ADR 0005 lock #2 already specifies the behavior; PR D's mechanical implementation of the CLI flag is not architecturally new; do NOT muddle ADR 0012 with it).
4. **Convergent answer all four missed: A1 + B1 with threshold = max(20%, 3 * sigma) AFTER the noise floor is measured.** This honors every camp's strongest point: Realist's PR-level enforcement, Builder's boring-ship discipline, Quant's statistical rigor, Growth's README-promise-keeping. Neutralizes each camp's counter-argument.

### Phase 0 / Phase 1 split

- **Phase 0 (this PR, M2 PR D)**: ship the infrastructure (`bench/spy_20y.py`, `bench/compare.py`, `.github/workflows/perf-budget.yml`, `.bench-baseline.json` bootstrap placeholder, `BarLoop.timing_breakdown()` opt-in) PLUS measure the empirical noise floor by running the bench in CI multiple times. The workflow runs in WARNING-only mode (exit 0 always; emit `::warning::` annotation on regression). README is updated to honestly describe Phase 0 status.
- **Phase 1 (follow-up PR after PR D merges)**: empirical median + stdev populate `.bench-baseline.json`. The workflow's `bench/compare.py` flips from warning to gate at `threshold = max(20%, 3 * stdev / median)`. README:52 updates to reflect the calibrated threshold.

## Author's response

The Verifier's synthesis is the correct read. The Phase 0 / Phase 1 split honors the four council positions cleanly: Phase 0 ships the infrastructure and measures the noise (Quant + Growth's precondition satisfied); Phase 1 enables the gate at a calibrated threshold (Realist + Builder + Growth get every-PR enforcement; Quant gets statistical defensibility via 3-sigma calibration). The Bouchaud flag is mechanical implementation of ADR 0005 lock #2 and ships in this PR as part of the M2 PR D scope per ADR 0005 step 16, but it does not need its own ADR.

### Accepted

All eight Verifier binding requirements are accepted in full.

### Contested

None.

### Final locked decisions

These 8 decisions bind the M2 PR D implementation. Revisiting any requires a superseding ADR.

1. **Phase 0 (this PR)**: ship infrastructure + measurement-only workflow. `bench/spy_20y.py --runs N --warmup K --output path.json` writes `{schema_version, median_seconds, stdev_seconds, n_runs, warmup, runner_image_sha, commit_sha, measured_at}`. `bench/compare.py --current path.json --baseline path.json --threshold-pct N --threshold-sigma M` computes delta and exits 0 with `::warning::` annotation on regression beyond threshold. Workflow runs `bench/spy_20y.py --runs 7 --warmup 1` then `bench/compare.py`; comments PR with delta + threshold + verdict; ALWAYS exits 0 at Phase 0.
2. **Phase 1 (follow-up PR)**: after PR D merges, run the workflow on main once via `workflow_dispatch`, commit empirical median + stdev to `.bench-baseline.json` in a tiny follow-up PR, AND flip the `bench/compare.py` exit code from "always 0" to "exit 1 if delta > max(20%, 3 * stdev/median)". README:52 updates in the same Phase 1 PR to reflect the calibrated threshold.
3. **Workflow trigger: every PR**, ubuntu-latest only (no Windows matrix; cross-platform is explicitly disavowed per `docs/methodology/determinism.md`), pinned runner image SHA, `actions/cache@v4` keyed on `hashFiles('uv.lock', 'pyproject.toml')`, `paths:` filter excluding `docs/**`, `**/*.md`, `.github/ISSUE_TEMPLATE/**`.
4. **Test statistic: median of 7 in-CI runs after 1 discarded warmup run.** Single-sample comparison is forbidden in `bench/compare.py`; the comparison requires `n_runs >= 5` and `warmup >= 1` on both sides.
5. **Baseline-bump procedure: B1 (manual PR with justification)**. PR template requires fields: `expected_delta_pct`, `measured_delta_pct`, `cause`, `kill_gate_rerun: yes/no`, `noise_floor_rerun: yes/no`. Bot-automated bumps (B2) and time-locked recalibration (B3) are explicitly rejected; review at Phase 2 only if Phase 1 produces > 2 false positives in the first 50 PRs.
6. **Bootstrap + escape hatch**: `bench/compare.py` exits 0 with `::warning::` if `.bench-baseline.json` is missing or has `n_runs: 0` (Phase 0 bootstrap state). Label `perf-budget-skip` on a PR bypasses the gate via `if: ${{ !contains(github.event.pull_request.labels.*.name, 'perf-budget-skip') }}`.
7. **Determinism scope clarification**: ADR 0012 states explicitly that timing values are OUT of the `docs/methodology/determinism.md` Requirement 5 bit-identical-output invariant. `BarLoop.timing_breakdown()` returns `list[tuple[str, float]]` sorted by step name, opt-in via the `enable_timing: bool = False` ctor flag per ADR 0005 lock #12. Default-off path is unchanged (no perf cost on production backtests).
8. **Bouchaud flag**: ADR 0012 does NOT address the Bouchaud CLI flag. The flag is mechanical implementation of ADR 0005 lock #2 (`beta=0.5` under `--impact-model=bouchaud` or alias `--impact-model=square-root-law`); both produce `beta=0.5`. PR D ships the flag in `examples/spy_cost_sensitivity.py` per Growth's recommendation (one CLI, two lines, beta=0.6 vs 0.5 side by side); no separate ADR.

## What this ADR does NOT do

- **Does NOT enable the perf-budget gate in Phase 0**. The workflow runs in warning-only mode until Phase 1.
- **Does NOT commit an empirical baseline in Phase 0**. The committed `.bench-baseline.json` has `n_runs: 0` until Phase 1.
- **Does NOT modify the determinism invariant**. Timing values are explicitly OUT of Requirement 5; the perf gate is a smoke test, not a scientific measurement.
- **Does NOT ship a Windows matrix**. Cross-platform reproducibility is disavowed per ADR 0001 decision 18.
- **Does NOT use a bot for baseline bumps**. B1 (manual PR with justification) is the only sanctioned procedure.

## Status

Accepted. M2 PR D implements the 8 locked decisions above (Phase 0). Phase 1 ships as a follow-up PR after empirical noise-floor data is collected on the post-merge main branch.

## Phase 1 follow-up (provenance)

Phase 1 shipped with the following empirical noise floor collected via `workflow_dispatch` on `main` HEAD `293a2ad3990ee4599426ba4d040168d900f6fec3`:

- runner image: `ubuntu24-20260525.161.1`
- 7-run median: 0.04299634899999205 s; stdev: 0.00022488560852116962 s; min: 0.042705421999997384 s; max: 0.04332529200000579 s
- CoV (stdev / median): 0.523%
- `max(20%, 3 * 0.523%) = max(20%, 1.57%) = 20%` (the 20% floor binds; the 3-sigma term sits below)
- Python: 3.11.15; polars: 1.41.1; numpy: 1.26.4; platform: Linux-x86_64
- measured_at: 2026-05-30T03:05:41.141607+00:00

The CoV came in at the low end of the Verifier's range, tighter than the council's 5-15% worst case. One caveat: the Verifier's CoV-bound estimate framed the workload as a "60-90s CPU-bound NumPy/Polars loop", but the actual harness clocks 43 ms. The 43 ms wall-clock is two orders of magnitude under the public 60-second 500-name budget so the Phase 1 gate is a smoke test for catastrophic per-bar dispatch regressions, NOT a fine-grained calibration of the production budget. M3 PIT-data work, which makes 500-name universes available, will revisit the harness shape.

The baseline is bound to the pinned `polars==1.41.1` and `numpy==1.26.4` in `pyproject.toml`; any patch bump requires a B1 baseline-rerun PR per lock #5.

Locks #1 through #8 above are unchanged. This footer records provenance only; the ADR is not reopened.
