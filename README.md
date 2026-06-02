# pit-backtest

A U.S. equity daily-bar backtester built as a teaching artifact for systematic-trading research. It exists to demonstrate four properties working together: structurally enforced point-in-time discipline, a CPCV-first validation API, Almgren-calibrated cost realism with honest uncertainty bounds, and external plus differential validation against `zipline-reloaded` and a hand-computable benchmark.

It is not a production trading system. The audience is a reviewer or recruiter who wants to see the design judgment behind those four properties.

## Status

M1 shipped (kill gate passes on 1y/3y/5y/10y windows; SI structurally skipped per Sharadar Premium SPY data starting 1997-12-31). M2 PR A (cost-model math + Commission classes + golden fixture + ADR 0006/0007), M2 PR B (ImpactedPriceSource + SquareRootImpactMatchingEngine + BarLoop wiring + ADR 0009), M2 PR C1 (SensitivityBand + Runner.run_sweep + spy_cost_sensitivity CLI demo + ADR 0010), M2 PR C2 (tolerance contract dormancy + ADR 0011 via 4-member council + verifier pass), M2 PR D Phase 0 (perf-budget CI on synthetic data + `--impact-model=bouchaud` CLI flag + `BarLoop.timing_breakdown()` opt-in + ADR 0012 via 4-member council + verifier pass), and M2 PR D Phase 1 (perf-budget workflow flipped to gate at `max(20%, 3 * sigma)` against empirical noise floor measured 2026-05-30 on `ubuntu24-20260525.161.1`; CoV=0.52%) shipped. M2 is fully shipped.

**M3 is fully shipped.** M3 PR 1 (data layer foundation: SharadarPermatickerResolver + LookaheadLeakError + TICKERS/SF1 readers), M3 PR 2 (per-row `get_price` + `get_fundamental` with Decimal at boundary), M3 PR 3 (corporate actions + cash flows + delisting dispatch over Sharadar ACTIONS), M3 PR 4 (SharadarSP500Universe + `members_at` wiring; Protocol amended for honest open-ended intervals), M3 PR 5a (five data quality contracts + runner + snapshot freshness check at startup), M3 PR 5b (SF1-vs-SEP authoritative-source resolution via `get_marketcap` / `get_shares_outstanding` + `NoDuplicateSp500EventsContract`), and M3 PR 5c (`examples/sp500_survivorship.py` IsMemberAt(t) survivorship study + M3 SHIPPED documentation flip) all merged. PR 5c implements ADR 0002 acceptance criterion 1 with the four headline numbers (PIT 2010 count, current count, survivor count, equal-weight CAGR delta in bps) consistent with published survivorship-bias studies (Hsu-Hutchinson 2006, Brown-Goetzmann-Ibbotson-Ross 1992). The two-panel equity-curve plot contemplated at ADR 0002 line 96 is deferred to M5 `scripts/figures/` per the amendment footer documented in ADR 0002.

**M4 is fully shipped.** M4 PR 1 (`analytics.sharpe`: PSR + DSR + MinTRL reproducing the Bailey-LdP 2014 worked example, DSR=0.766 within 1e-3), M4 PR 2 (`analytics.drawdown` + `concentration` + `distribution`), prep PR 3a / ADR 0015 (`BacktestPathDistribution[T]` bounded TypeVar + `BacktestResult.__lt__` + `Split.test_groups`), M4 PR 3b (the three CV splitters: PurgedKFold + WalkForward + CPCV with purge/embargo + path-stitching), M4 PR 4 (SQLite WAL `TrialRegistry` feeding DSR; concurrent-write acceptance), and M4 PR 5 (the `ConstantWeightDemoResult -> BacktestResult` adapter + `Scorecard.to_markdown()` six-section LdP ch14 render + `docs/TESTING.md`) all merged. The six ADR 0002 M4 acceptance criteria are met. `Runner.run_cpcv`'s orchestration body is deferred to M5 because a CPCV path requires the fit/predict strategy semantics the M5 momentum study introduces; the CPCV path-count acceptance is satisfied by `CPCVSplitter` + the `BacktestPathDistribution` container.

**M5 is in progress.** ADR 0016 (the `run_cpcv` redesign, the CPCV-degeneracy finding for a deterministic factor, and the stationary block-bootstrap path-uncertainty plan) and M5 PR 1 (the JT1993 12-1 total-return momentum signal) are merged. The M5 universe rework (ADR 0017) reworks `SharadarSP500Universe` from event-replay to the Sharadar quarterly membership snapshots: `members_at(t)` returns the most-recent `historical` / `current` snapshot on or before `t`, members resolve by date-agnostic ticker string, and the `added` / `removed` event log is demoted to a raising cross-check contract (`_DEFAULT_CONTRACTS` grows from six to seven). The survivorship-bias-free `sharadar_2026-05-31` S&P 500 bundle loads contract-clean and `members_at` returns about 500 names across 2005-2024. M5 PR 2 is shipped in three parts: PR 2a (the `stationary_block_bootstrap` and `TopQuintileLongPolicy`), PR 2b (the real `PitView` BarLoop wiring), and PR 2c (the `Runner.run_cpcv` body, which runs N per-group backtests and stitches the phi reconstructed paths, reporting their coincidence for the deterministic momentum factor as the CPCV degeneracy ADR 0016 anticipates, rather than presenting a single line as a distribution). M5 PR 3a adds the `BarLoop.signal_calendar` perf gate (gating `signal.compute` to the rebalance calendar, the ~21x speedup that makes the real 502-name 2005-2024 study tractable; behavior-preserving). M5 PR 3b is the worked study (`examples/sp500_momentum_study.py`): the contiguous zero-cost backtest as the level reference, PSR and the Deflated Sharpe at `naive_effective_n=1` (one pre-specified strategy, so the DSR is the honest PSR-against-zero), the CPCV degeneracy, the stationary block-bootstrap fan on the monthly return series (block length chosen from the measured autocorrelation), the year-by-year decomposition, and an honest DSR conclusion that passes whether or not the strategy clears the bar. M5 PR 3c adds the cost surfaces (appended by `compute_cost_surfaces`, skippable via `--skip-sweep` so the zero-cost core is untouched): a cost-sensitivity band over the Almgren eta grid, labelled a uniform-liquidity eta-sensitivity surface rather than a realized cost (one SPY-typical market-state row is applied to all names), and a commission-only contiguous-vs-CPCV seam decomposition. The seam measurement corrected ADR 0016 dec 2 on real data: the naive contiguous-minus-stitched level gap is dominated by the omitted inter-group gap-day market moves rather than commission, and the stitch's running-level carry normalizes away the per-group entry commissions, so the genuine N-1 re-entry commission is measured directly (and is positive) instead of being read off the level gap. Remaining M5 work: `scripts/figures/` + `docs/METHODOLOGY.md` + the M5 SHIPPED flip (PR 4).

Research phases 1 and 2 are complete; phase 3 ADRs 0001 (spec critique), 0002 (M1-M5 roadmap), 0003 (architecture), 0004 (rebalance calendar), 0005 (M2 plan), 0006 (trailing-period reconciliation), 0007 (FIM ceiling), 0008 (SSGA tolerances), 0009 (ImpactedPriceSource policy + M2 PR B structure), 0010 (sensitivity-band runner), 0011 (tolerance contract dormancy at M2), 0012 (perf-budget CI Phase 0 noise-floor measurement), 0013 (PSR/DSR/MinTRL API + Bailey-LdP 2014 pin correction), 0014 (DrawdownDurationReport contract), 0015 (CV TypeVar Protocol + Split.test_groups), 0016 (run_cpcv redesign + CPCV degeneracy + block bootstrap), 0017 (snapshot-based SP500 universe) are merged. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/decisions/`](docs/decisions/) for the current plan and locked architecture.

## Why this exists

Most open-source Python backtesters fall into one of two failure modes:

1. They allow lookahead bias by convention rather than by structure. A diligent user avoids it; an inattentive user produces spurious results.
2. They silently distort returns by under-modeling execution and data: zero slippage, no partial fills, no borrow, no point-in-time index membership, no corporate-action correctness.

This repo is an opinionated response to those failure modes within a deliberately narrow scope. The phase 1 survey at [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md) and the phase 2 methodology synthesis at [`docs/research/0002-methodology.md`](docs/research/0002-methodology.md) explain what the existing landscape gets wrong and what the canonical literature says is right.

## Design pillars

- **Event-driven core with kernel sharing**. Every signal, order, and fill is an explicit timestamped event. The same kernel runs validation backtests and any future live execution; only the clock implementation differs. The pattern is taken from `nautilus_trader` and is documented in [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md).
- **Structural lookahead protection at the API boundary, with documented trust boundaries**. The data layer's return types and the event-loop ordering prevent the common patterns. The remaining trust boundaries (arbitrary Python in callbacks, alternative-data joins computed outside the engine, feature-store wrappers) are enumerated explicitly in [`docs/decisions/0003-architecture.md`](docs/decisions/0003-architecture.md). This is the honest version of the "structurally impossible" claim.
- **CPCV-first validation**. Combinatorial Purged Cross-Validation is the primary validation surface. Walk-forward is exposed as a CPCV configuration with one path. Backtest results are distributions across paths; any single-Sharpe API on a CPCV result is a correctness bug.
- **The LdP chapter 14 scorecard**. PSR, DSR, MinTRL, HHI, drawdown stats, per-year decomposition are the default analytics. Raw Sharpe shown alone is a configuration error.
- **Cost realism with honest uncertainty bounds**. Default cost model is SquareRootImpact with Almgren 2005 calibration (eta=0.142, beta=0.6, gamma=0.314), labeled as a 1998-2000 calibration. Sensitivity bands at eta in [0.05, 0.30] are required in every backtest report. A `--impact-model=bouchaud` flag substitutes beta=0.5. At M2 the cost-tolerance enforcement contract is scaffolded but dormant by design; the Almgren-2005 cost model does not consume mid price, so the methodology doc's tolerance formula is documentation-only until distinct policy-time vs matcher-time `MarketStateLookup` snapshots land at M3 (see [ADR 0011](docs/decisions/0011-tolerance-contract-dormancy-at-m2.md)).
- **Point-in-time data with persistent identifiers**. Dual-timestamp model (`period_end_dt`, `available_dt`) on every record. Typed `Universe` API with `is_member(asset_id, date)`. Persistent asset identifiers via Sharadar TICKERS.
- **Engine self-validation**. M1 reconciles buy-and-hold SPY against SSGA's published SPY NAV TR for the trailing 1Y / 3Y / 5Y / 10Y / SI periods (anchored on SSGA's as-of date) within 5 bps annualized per period; a deterministic hand-computable strategy is verified to exact match. M5 includes differential testing against `zipline-reloaded` with a published reconciliation report.

## Explicit non-goals

- Not intraday or LOB-level market microstructure.
- Not options or other derivatives.
- Not multi-asset macro portfolios; equity only at v1.
- Not live trading. The kernel-sharing pattern is included for design discipline, not because live execution is in scope.
- Not crypto-specific market structure.
- Not a substitute for `vectorbtpro` for parameter-sweep research, `zipline-reloaded` for established factor work, or `nautilus_trader` for production-grade execution. This is a focused teaching artifact, not a feature-superset of any of them.

## Stack

- Python 3.11+
- `uv` for environment and dependency management
- Polars end-to-end (with `.to_pandas()` available on every public results object for users who prefer Pandas)
- NumPy and Numba for the inner kernel hot paths
- Pydantic for typed data models
- pytest with high coverage on the engine core
- mypy strict mode
- Runs on Linux and WSL2

## Performance budget

A 20-year backtest on 500 U.S. equity names completes in under 60 seconds on a laptop. The budget is tracked in CI via [`.github/workflows/perf-budget.yml`](.github/workflows/perf-budget.yml) using the `bench/spy_20y.py` synthetic harness (real Sharadar data is unavailable in CI per `dataset_versioning.md`). Per [ADR 0012](docs/decisions/0012-perf-budget-ci-phase-0-noise-floor.md) the workflow shipped in two phases: **Phase 0** ran the harness in warning-only mode while the empirical noise floor was collected; **Phase 1** (current) fails the build on regression at `max(20%, 3 * sigma)`. The empirical baseline was collected on runner image `ubuntu24-20260525.161.1` at commit `293a2ad` via `workflow_dispatch` (7-run median = 43.0 ms, stdev = 0.22 ms, CoV = 0.52%); the 20% floor binds. The synthetic harness is a SPY-only single-ticker monthly-rebalance probe sized for per-bar dispatch overhead, not the full 500-name production budget; the 43 ms wall-clock is two orders of magnitude under the headline 60-second target so the gate is a smoke test for catastrophic regressions, not a fine-grained scaling check (M3 PIT-data work will revisit the harness shape when 500-name universes become available). Per ADR 0001 decision 18 the workflow is Linux-only; cross-platform reproducibility is disavowed. The baseline is bound to the pinned `polars==1.41.1` and `numpy==1.26.4` in `pyproject.toml`; any patch bump requires a B1 baseline-rerun PR per ADR 0012 lock #5.

## V1 data inventory

- [Sharadar SF1 ARQ](https://data.nasdaq.com/databases/SF1): point-in-time U.S. fundamentals (as-reported quarterly).
- [Sharadar SEP](https://data.nasdaq.com/databases/SEP): point-in-time prices and delistings with cash proceeds.
- [Sharadar TICKERS](https://data.nasdaq.com/databases/SF1): identifier history.

Documented gaps for v1: borrow rates (no v1 source; short tests are flagged as estimates), PIT S&P 500 reconstitution at effective-date resolution (the Sharadar quarterly membership snapshots are the v1 source; effective-date reconciliation against the add/drop event log is v1.1, per ADR 0017), full corporate-actions feed (rights offerings and special distributions out of v1 scope).

## Repo layout

```
docs/
  ROADMAP.md            phased milestones
  METHODOLOGY.md        the quant methodology, with citations (M5)
  TESTING.md            validation strategy, known-answer tests (M4)
  methodology/          per-topic methodology contracts (Monday pre-M1)
    total_return_reconstruction.md
    dataset_versioning.md
    pydantic_polars_boundary.md
    determinism.md
  research/             topical research syntheses
    sources/            per-source detailed notes
  decisions/            numbered ADRs
src/pit_backtest/       engine source (protocols stubbed; M1 fills in)
tests/                  pytest tests (scaffold only; M1 fills in)
data/snapshots/         vendor snapshots (gitignored; manifest.toml committed)
scripts/figures/        reproducible figure generators for the README (M5)
pyproject.toml
CHANGELOG.md
LICENSE
```

## Running locally

The M1 engine path is wired. Both demos run against synthetic fixtures in CI; the real-data versions require a Sharadar snapshot under `data/snapshots/`.

Setup:

```
uv sync --extra dev --extra dataops
```

Set the Nasdaq Data Link API key (the official env var; legacy `SHARADAR_API_KEY` also accepted; never paste secrets in chat):

```
[Environment]::SetEnvironmentVariable("NASDAQ_DATA_LINK_API_KEY", "<your_key>", "User")
```

Open a new PowerShell window so the variable loads. On Windows, also point uv at a venv outside OneDrive and force copy-mode linking (the default hardlink mode fails on OneDrive reparse points):

```
[Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", "C:\Users\<you>\.venvs\pit-backtest", "User")
[Environment]::SetEnvironmentVariable("UV_LINK_MODE", "copy", "User")
```

Both variables MUST be set at User scope (not just in the current shell). If `UV_PROJECT_ENVIRONMENT` is unset in a shell, uv silently writes to an in-tree `.venv` inside OneDrive which then accumulates corruption from interrupted installs. Verify with `uv pip show pandas` after `uv sync`: the `Location` field must be your `.venvs\pit-backtest\Lib\site-packages` path, not `<project>\.venv\Lib\site-packages`. If you see the wrong path, set the variables and nuke BOTH venvs:

```
$env:UV_PROJECT_ENVIRONMENT = "C:\Users\<you>\.venvs\pit-backtest"
$env:UV_LINK_MODE = "copy"
Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\Users\<you>\.venvs\pit-backtest" -ErrorAction SilentlyContinue
uv sync --extra dev --extra dataops
```

Pull data and run the kill-gate. Pass `--end-date` to pull through SSGA's most recent `as_of_date` (per ADR 0006 the kill gate is anchored on the SSGA as-of date; the bundle must cover at least one trailing window for the gate to report PASS or FAIL rather than NEEDS_DATA).

```
uv run python scripts/pull_m1_data.py --end-date 2026-04-30
# Manually download spdr-etf-historical-distributions.xlsx and
# spdr-product-data-us-en.xlsx from
# https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy
# into data/snapshots/spy_ssga_<YYYY-MM-DD>/  (do not rename them).
uv run python -m pit_backtest.data.sources.sharadar_pull --bundle sharadar_<YYYY-MM-DD> --refresh-hashes
uv run python -m pit_backtest.data.sources.sharadar_pull --bundle spy_ssga_<YYYY-MM-DD> --refresh-hashes
uv run python -m examples.spy_buy_and_hold --compare-to-ssga
uv run python -m examples.constant_weight_three_names --diff-against-reference
```

`examples.spy_buy_and_hold --compare-to-ssga` exits 0 on PASS, 1 on FAIL, 2 on NEEDS_DATA (the bundle covers no trailing window) or any missing-bundle condition.

See [`docs/vendor/nasdaq-data-link-pull.md`](docs/vendor/nasdaq-data-link-pull.md) for the full pull procedure and troubleshooting.

Or just the test suite:

```
$env:PYTHONHASHSEED="0"; uv run pytest tests/
```

CI runs the synthetic-fixture tests on every push; the snapshot-gated real-data tests skip cleanly when no snapshot is present.

## Suggested reading order for a reviewer

1. [`docs/ROADMAP.md`](docs/ROADMAP.md): the M1 through M5 plan with the kill-early gate.
2. [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md): what the existing field gets wrong.
3. [`docs/research/0002-methodology.md`](docs/research/0002-methodology.md): what the canonical literature says.
4. [`docs/decisions/0001-spec-critique.md`](docs/decisions/0001-spec-critique.md): the critique of the original spec, the skeptical-reviewer pass, the locked decisions on scope and stack.
5. [`docs/decisions/0002-roadmap-review.md`](docs/decisions/0002-roadmap-review.md): the M1 through M5 acceptance criteria, with the ten-week timeline and kill gate.
6. [`docs/decisions/0003-architecture.md`](docs/decisions/0003-architecture.md): the protocol hierarchy, the trust boundary list, the data model.
7. [`docs/decisions/0004-rebalance-calendar-independence.md`](docs/decisions/0004-rebalance-calendar-independence.md): rebalance calendars are fund-policy-determined, independent of backtest window. Captured during M1 day 3 implementation.
8. [`docs/decisions/0005-m2-cost-realism-plan.md`](docs/decisions/0005-m2-cost-realism-plan.md): M2 cost-realism implementation plan; 18 locked decisions including the Almgren formula form, VWAP refusal, the four-PR split, and the queued ADRs 0006 and 0007.
9. [`docs/decisions/0006-trailing-period-spy-reconciliation.md`](docs/decisions/0006-trailing-period-spy-reconciliation.md): SPY reconciliation reframed from a single 2005-2024 window to SSGA's published trailing 1Y / 3Y / 5Y / 10Y / SI periods anchored on SSGA's as-of date; snap-backward anchor convention; `ExpenseRatioSchedule` for the 2003-11-01 step.
10. [`docs/decisions/0007-fim-2018-demoted-to-upper-ceiling.md`](docs/decisions/0007-fim-2018-demoted-to-upper-ceiling.md): M2 cost-realism acceptance criterion revised; formula-derived `[eta=0.05, eta=0.30]` band is the gate; FIM 2018 preserved as a 50-bp upper-ceiling sanity check.
11. [`docs/methodology/`](docs/methodology/): the four pre-M1 contracts (total-return reconstruction, dataset versioning, Pydantic/Polars/attrs boundary, determinism invariant). Read after the ADRs; these are the implementation contracts the engine code in M1 onward is held to.
