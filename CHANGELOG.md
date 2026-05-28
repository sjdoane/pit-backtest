# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository scaffold: `.gitignore`, `README.md`, `CHANGELOG.md`, `LICENSE`, `docs/ROADMAP.md` stub.
- Research phase 1: comparative survey of existing open-source backtesters under [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md), with per-library detailed analyses in [`docs/research/sources/`](docs/research/sources/).
- Research phase 2: methodology canon synthesis under [`docs/research/0002-methodology.md`](docs/research/0002-methodology.md), with per-topic source analyses in [`docs/research/sources/methodology-*.md`](docs/research/sources/) covering AFML chs 11-15 on backtesting, the Bailey and Lopez de Prado backtest-overfitting papers (PSR, DSR, PBO, MinTRL), the Almgren-Chriss optimal-execution model and the square-root market-impact literature, the point-in-time data treatment across five PIT axes, and seven substantive practitioner postmortems.
- ADR 0001 ([`docs/decisions/0001-spec-critique.md`](docs/decisions/0001-spec-critique.md)): critique of the original project spec with a skeptical-reviewer pass and final decisions. Reframes the project as a teaching artifact with explicit non-goals; commits CPCV as the primary validation surface; commits the LdP chapter 14 scorecard as the default analytics; commits Polars end-to-end with `.to_pandas()` adapter; commits Sharadar SF1 ARQ + SEP + TICKERS as the v1 data inventory with documented gaps.
- ADR 0002 ([`docs/decisions/0002-roadmap-review.md`](docs/decisions/0002-roadmap-review.md)): M1 through M5 implementation milestone breakdown with a skeptical-reviewer pass and final decisions. Extends the v1 timeline from four weeks to ten weeks after the reviewer's honest-hours analysis (15-25 effective hours per week across DripWatch, Kalshi, undergraduate obligations). Cuts `zipline-reloaded` differential testing from v1 (moved to v1.1 backlog) and moves the kill-early gate from end of week 1 to end of week 2 on the SPY reconciliation. Locks acceptance criteria for each milestone including the Bailey-LdP 2014 numerical replication (DSR=0.971 within 1e-3) for M4 and the honest DSR reporting requirement for M5 (the strategy passes the milestone whether it clears DSR>=0.95 or fails it).

### Changed
- README reframed from "production-grade event-driven backtester" to "U.S. equity daily-bar backtester built as a teaching artifact" per ADR 0001 final decisions. Added explicit non-goals section, design-pillar updates, performance budget, and v1 data inventory.
- ROADMAP updated through both ADR 0001 (carrying the locked decisions forward) and ADR 0002 (adding the full M1 through M5 phase with the ten-week timeline, acceptance criteria, and the v1.1 backlog).
