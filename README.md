# pit-backtest

A production-grade event-driven backtester for systematic trading research.

## Status

Pre-implementation. Currently in research phase 1 of 3 before writing any engine code. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the planned milestones and [`docs/research/`](docs/research/) for the survey work that informs the design.

## Why this exists

Most open-source Python backtesters fall into one of two failure modes:

1. They allow lookahead bias by convention rather than by structure. A diligent user avoids it; an inattentive user produces spurious results.
2. They silently distort returns by under-modeling execution: zero slippage, no partial fills, no borrow, no point-in-time index membership, no corporate-action correctness.

Goal of this repo: a backtester where lookahead bias is structurally impossible (enforced by the data API and the type system, not by author discipline) and where realistic execution costs are first-class. Walk-forward and purged k-fold cross-validation with embargo (Lopez de Prado) are first-class features.

## Design pillars

- **Event-driven core.** Every market tick, signal, order, and fill is an explicit timestamped event flowing through a queue, not a vectorized aggregation over time.
- **Structural lookahead protection.** Data access at time `t` cannot return data from time `> t`. Enforced by the data API's return types and the event-loop ordering, not by author discipline.
- **Realistic execution.** Configurable slippage (fixed bps, volume participation, square-root impact), commissions, bid-ask costs, partial fills, MOO and MOC vs intraday semantics.
- **Survivorship-bias-free universes.** Point-in-time index membership is a built-in concept.
- **Correct corporate actions.** Splits, dividends, delistings handled with explicit semantics.
- **Long/short cost realism.** Borrow costs and short-sale constraints for long-short strategies.
- **Validation as a first-class feature.** Walk-forward, purged k-fold with embargo, bootstrap confidence intervals on Sharpe.

## Stack

- Python 3.11+
- uv for environment and dependency management
- Polars as the primary tabular backbone (under evaluation; see ADR 0003 when written)
- Pydantic for typed data models
- pytest with high coverage on the engine core
- mypy strict mode
- Runs on Linux and WSL2

## Repo layout

```
docs/
  ROADMAP.md            phased milestones
  METHODOLOGY.md        the quant methodology, with citations (not yet written)
  TESTING.md            validation strategy, known-answer tests (not yet written)
  research/             topical research syntheses
    sources/            per-source detailed notes
  decisions/            numbered ADRs
src/                    engine source (not yet written)
tests/                  pytest tests (not yet written)
CHANGELOG.md
LICENSE
```

## Running locally

Not yet runnable. Engine implementation begins after the three research phases complete and the architecture ADR lands. See the roadmap.

## Suggested reading order for a reviewer

1. [`docs/ROADMAP.md`](docs/ROADMAP.md)
2. [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md)
3. `docs/research/0002-methodology.md` (pending)
4. `docs/decisions/0001-spec-critique.md` (pending)
5. `docs/decisions/0003-architecture.md` (pending)
