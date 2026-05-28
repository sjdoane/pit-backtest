# Roadmap

Status: Research phases 1 and 2 complete. Phase 3 (spec critique and architecture ADRs) is next. Implementation milestones (M1..Mn) will be defined after phase 3 lands. See [`README.md`](../README.md) for context.

## Phases

### Phase 1: existing-landscape survey (complete)

Goal: understand the design choices, strengths, and failure modes of existing open-source backtesters before designing our own.

Deliverable: [`docs/research/0001-existing-backtesters.md`](research/0001-existing-backtesters.md) plus per-source detail under [`docs/research/sources/`](research/sources/).

Coverage: zipline (including zipline-reloaded), backtrader, vectorbt, bt, qstrader, nautilus_trader.

Key findings carried into phase 2 and the architecture ADR: the field collectively gets corporate actions and point-in-time index membership wrong; lookahead protection should be structural (Pipeline + min-period + clock injection) not by convention; execution realism must be required, not optional; sweep mode and event-driven mode should be separate, explicitly labeled paths.

### Phase 2: methodology canon (complete)

Goal: synthesize the literature on backtest validity and execution realism. Includes Lopez de Prado AFML chapters 11 through 15, Bailey and Lopez de Prado on the deflated Sharpe and the probability of backtest overfitting, Almgren and Chriss on optimal execution and market impact, the standard treatment of point-in-time data, and seven practitioner postmortems.

Deliverable: [`docs/research/0002-methodology.md`](research/0002-methodology.md) plus per-topic source analyses under [`docs/research/sources/methodology-*.md`](research/sources/).

Key findings carried into phase 3 and the architecture ADR: the analytics layer must compute PSR, DSR, MinTRL, and a confidence-tier label by default (raw SR alone is a configuration error); the cost model defaults to SquareRootImpact with Almgren 2005 calibration (eta = 0.142, beta = 0.6, gamma = 0.314) and permanent impact must feed the price series; the data layer requires a dual-timestamp model (`period_end_dt` and `available_dt`), a typed Universe API, and persistent asset identifiers; CPCV returns a Sharpe distribution, not a scalar.

### Phase 3: spec critique and architecture (pending)

Goal: stress-test the spec, draft the architecture, both reviewed by a skeptical agent persona before lock-in.

Deliverables:
- `docs/decisions/0001-spec-critique.md` (spec critique plus skeptical review)
- `docs/decisions/0002-roadmap-review.md` (skeptical review of the M1..Mn roadmap)
- `docs/decisions/0003-architecture.md` (class/protocol hierarchy and event loop, plus skeptical review)

Each ADR captures the original proposal, the reviewer critique, and the response that drove the final decision.

### Phase 4 onwards: implementation milestones M1..Mn (pending)

To be defined at the end of phase 3. Each milestone must be independently demoable. M1 will validate against known-answer tests: a buy-and-hold SPY backtest matching the actual SPY total return, plus a deterministic hand-computable strategy.

## Deferred / out of scope (until reconsidered)

- Live trading. This is a backtester; live trading is explicitly out of scope for the v1 horizon.
- Options modeling. Equity only at M1; revisit after the equity engine is solid.
- Crypto-specific market microstructure. Assume traditional equities (regular sessions, T+1/T+2 settlement abstracted) at M1.
- Fixed income, FX, futures roll mechanics. Out of scope for v1.
- GPU acceleration. Profile first; defer until a hot path is empirically a bottleneck.
