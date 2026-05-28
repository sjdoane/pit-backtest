# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository scaffold: `.gitignore`, `README.md`, `CHANGELOG.md`, `LICENSE`, `docs/ROADMAP.md` stub.
- Research phase 1: comparative survey of existing open-source backtesters under [`docs/research/0001-existing-backtesters.md`](docs/research/0001-existing-backtesters.md), with per-library detailed analyses in [`docs/research/sources/`](docs/research/sources/).
- Research phase 2: methodology canon synthesis under [`docs/research/0002-methodology.md`](docs/research/0002-methodology.md), with per-topic source analyses in [`docs/research/sources/methodology-*.md`](docs/research/sources/) covering AFML chs 11-15 on backtesting, the Bailey and Lopez de Prado backtest-overfitting papers (PSR, DSR, PBO, MinTRL), the Almgren-Chriss optimal-execution model and the square-root market-impact literature, the point-in-time data treatment across five PIT axes, and seven substantive practitioner postmortems.
- ADR 0001 ([`docs/decisions/0001-spec-critique.md`](docs/decisions/0001-spec-critique.md)): critique of the original project spec with a skeptical-reviewer pass and final decisions. Reframes the project as a teaching artifact with explicit non-goals; commits CPCV as the primary validation surface; commits the LdP chapter 14 scorecard as the default analytics; commits Polars end-to-end with `.to_pandas()` adapter; commits a four-week v1 timeline subject to the kill-early rule; commits Sharadar SF1 ARQ + SEP + TICKERS as the v1 data inventory with documented gaps.

### Changed
- README reframed from "production-grade event-driven backtester" to "U.S. equity daily-bar backtester built as a teaching artifact" per ADR 0001 final decisions. Added explicit non-goals section, design-pillar updates, performance budget, and v1 data inventory.
- ROADMAP updated to reflect ADR 0001 accepted and ADR 0002 (roadmap) and ADR 0003 (architecture) as the next deliverables, with the locked-in decisions carried forward.
