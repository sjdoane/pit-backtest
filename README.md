# pit-backtest

A U.S. equity daily-bar backtester built as a teaching artifact for systematic-trading research. It exists to demonstrate four properties working together: structurally enforced point-in-time discipline, a CPCV-first validation API, Almgren-calibrated cost realism with honest uncertainty bounds, and external plus differential validation against `zipline-reloaded` and a hand-computable benchmark.

It is not a production trading system. The audience is a reviewer or recruiter who wants to see the design judgment behind those four properties.

## Status

Pre-M1 contracts landed. Research phases 1 and 2 are complete; all three phase 3 ADRs are accepted and merged (0001 spec critique, 0002 M1 through M5 roadmap, 0003 architecture). The Monday-of-week-1 pre-M1 deliverables are landed: four methodology documents under [`docs/methodology/`](docs/methodology/) (total-return reconstruction, dataset versioning, Pydantic/Polars/attrs boundary, determinism invariant) plus the `src/pit_backtest/` package scaffold with protocols stubbed and dependencies pinned. M1 engine code begins Tuesday of week 1. The implementation is sliced into M1 through M5 over a ten-week timeline with a kill-early gate at end of week 2 on SPY reconciliation. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/decisions/`](docs/decisions/) for the current plan and locked architecture.

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
- **Cost realism with honest uncertainty bounds**. Default cost model is SquareRootImpact with Almgren 2005 calibration (eta=0.142, beta=0.6, gamma=0.314), labeled as a 1998-2000 calibration. Sensitivity bands at eta in [0.05, 0.30] are required in every backtest report. A `--impact-model=bouchaud` flag substitutes beta=0.5.
- **Point-in-time data with persistent identifiers**. Dual-timestamp model (`period_end_dt`, `available_dt`) on every record. Typed `Universe` API with `is_member(asset_id, date)`. Persistent asset identifiers via Sharadar TICKERS.
- **Engine self-validation**. M1 reconciles buy-and-hold SPY against the actual SPY total return within 5 bps annualized; a deterministic hand-computable strategy is verified to exact match. M5 includes differential testing against `zipline-reloaded` with a published reconciliation report.

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

A 20-year backtest on 500 U.S. equity names completes in under 60 seconds on a laptop. The budget is tracked in CI; any regression over 10% fails the build.

## V1 data inventory

- [Sharadar SF1 ARQ](https://data.nasdaq.com/databases/SF1): point-in-time U.S. fundamentals (as-reported quarterly).
- [Sharadar SEP](https://data.nasdaq.com/databases/SEP): point-in-time prices and delistings with cash proceeds.
- [Sharadar TICKERS](https://data.nasdaq.com/databases/SF1): identifier history.

Documented gaps for v1: borrow rates (no v1 source; short tests are flagged as estimates), PIT S&P 500 reconstitution effective dates (Sharadar SP500 event log is the source), full corporate-actions feed (rights offerings and special distributions out of v1 scope).

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

Not yet runnable. The package scaffold imports and the contract tests pass, but every engine path raises `NotImplementedError` until M1 lands the SEP adapter, the total-return reconstruction, the buy-and-hold demo, and the constant-weight monthly rebalance demo (target: end of week 2). See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the schedule and [`docs/methodology/`](docs/methodology/) for the contracts M1 builds against.

## Suggested reading order for a reviewer

1. [`docs/ROADMAP.md`](docs/ROADMAP.md): the M1 through M5 plan with the kill-early gate.
2. [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md): what the existing field gets wrong.
3. [`docs/research/0002-methodology.md`](docs/research/0002-methodology.md): what the canonical literature says.
4. [`docs/decisions/0001-spec-critique.md`](docs/decisions/0001-spec-critique.md): the critique of the original spec, the skeptical-reviewer pass, the locked decisions on scope and stack.
5. [`docs/decisions/0002-roadmap-review.md`](docs/decisions/0002-roadmap-review.md): the M1 through M5 acceptance criteria, with the ten-week timeline and kill gate.
6. [`docs/decisions/0003-architecture.md`](docs/decisions/0003-architecture.md): the protocol hierarchy, the trust boundary list, the data model.
7. [`docs/methodology/`](docs/methodology/): the four pre-M1 contracts (total-return reconstruction, dataset versioning, Pydantic/Polars/attrs boundary, determinism invariant). Read after the ADRs; these are the implementation contracts the engine code in M1 onward is held to.
