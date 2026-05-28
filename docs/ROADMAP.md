# Roadmap

Status: Research phases 1 and 2 complete. Phase 3 in progress: ADR 0001 (spec critique) accepted in this PR; ADRs 0002 (roadmap) and 0003 (architecture) next. Implementation milestones (M1..Mn) will be defined in ADR 0002. See [`README.md`](../README.md) for context.

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

### Phase 3: spec critique and architecture (in progress)

Goal: stress-test the spec, draft the architecture, both reviewed by a skeptical agent persona before lock-in.

Deliverables:
- [`docs/decisions/0001-spec-critique.md`](decisions/0001-spec-critique.md) (spec critique plus skeptical review, **accepted**)
- `docs/decisions/0002-roadmap-review.md` (M1..Mn milestone breakdown plus skeptical review, pending)
- `docs/decisions/0003-architecture.md` (class/protocol hierarchy, event loop, trust boundaries, plus skeptical review, pending)

Each ADR captures the original proposal, the skeptical reviewer's critique, the author's response, and the final locked-in decisions.

Key locked-in decisions from ADR 0001 that constrain 0002 and 0003: scope is U.S. equity daily-bar only; framing is "teaching artifact" not "production-grade"; CPCV is primary validation surface with results as path distributions; the LdP chapter 14 scorecard is the default analytics output; cost model defaults to Almgren 2005 SquareRootImpact with sensitivity bands; six-layer architecture (data, signal, policy, execution, risk decomposition, analytics); Polars end-to-end with `.to_pandas()` boundary adapter; v1 data inventory is Sharadar SF1 ARQ + SEP + TICKERS with documented gaps on borrow and PIT S&P 500 reconstitution dates; performance budget is 20-year backtest on 500 names in under 60 seconds; v1 timeline is four weeks from engine implementation start or the project is killed per the kill-early rule.

### Phase 4 onwards: implementation milestones M1..Mn (pending)

To be defined at the end of phase 3. Each milestone must be independently demoable. M1 will validate against known-answer tests: a buy-and-hold SPY backtest matching the actual SPY total return, plus a deterministic hand-computable strategy.

## Deferred / out of scope (until reconsidered)

- Live trading. This is a backtester; live trading is explicitly out of scope for the v1 horizon.
- Options modeling. Equity only at M1; revisit after the equity engine is solid.
- Crypto-specific market microstructure. Assume traditional equities (regular sessions, T+1/T+2 settlement abstracted) at M1.
- Fixed income, FX, futures roll mechanics. Out of scope for v1.
- GPU acceleration. Profile first; defer until a hot path is empirically a bottleneck.
