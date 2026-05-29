# ADR 0007: FIM 2018 demoted to upper-ceiling sanity check for the M2 cost-realism gate

Status: Accepted.
Date: 2026-05-29.
Authors: Sam Doane.

## Context

ADR 0002 M2 acceptance criterion 1 originally read:

> SPY monthly rebalance at $1M notional from 2005 to 2024 produces total impact cost in `[A, B]` bps annualized where `A` is the model output at `eta=0.05` and `B` at `eta=0.30`, with `eta=0.142` central estimate falling between. Sanity-checked against Frazzini-Israel-Moskowitz 2018 (~10 bps for liquid US large-cap).

ADR 0005 step 17 (the pre-M2 design pass) queued a revision: the FIM 2018 ~10 bps figure is calibrated on institutional flows. AQR's published estimate uses their own execution book, whose typical trades are orders of magnitude larger than $1M. The Almgren 2005 formula at SPY's roughly $40B average daily volume and $1M notional yields a temporary impact of approximately 1 to 5 bps, not 10. Using FIM's 10 bps as a central-estimate target would force the engine to overstate cost relative to the formula it implements, defeating the purpose of the formula-derived band.

ADR 0006 (trailing-period SPY reconciliation) was originally planned to carry this revision as a secondary decision. The skeptical-reviewer of ADR 0006 split the decisions on the grounds that they share no code, no test files, no methodology doc, and no risk surface. ADR 0007 ships as a separate doc-only ADR per that split.

## Decision

ADR 0007 supersedes the FIM-2018 cross-check phrasing of ADR 0002 M2 acceptance criterion 1. The revised criterion is:

> **ADR 0002 M2 acceptance criterion 1 (revised by ADR 0007).** The Almgren central-estimate computed for a SPY $1M monthly rebalance over the M2 window using `eta=0.142`, `beta=0.6`, `gamma=0.314` falls inside the formula-derived band `[eta=0.05, eta=0.30]`. The central estimate is additionally below 50 bps annualized as a Frazzini-Israel-Moskowitz 2018 upper-ceiling sanity check; FIM's central ~10 bps figure is calibrated for institutional flows much greater than $1M notional and is not a central-estimate target at this scale.

The formula-derived band is the gate. FIM 2018 is the ceiling. The 5-eta sensitivity-band rendering requirement (ADR 0002 M2 acceptance criterion 2) is unchanged.

## Rationale

The reasoning is straightforward and short:

- FIM 2018's ~10 bps headline is computed across AQR's institutional execution book over 1998-2016. Their dataset has a mean trade size in the tens of millions of dollars or larger and covers thousands of liquid US large-caps in aggregate.
- SPY at $1M notional is six orders of magnitude below the AUM scale FIM calibrate on. The Almgren formula scales temporary impact as `sigma_D * |participation_rate|^beta` where `participation_rate = Q / (V_D * T)`. At SPY's daily $40B+ volume, a $1M order's `participation_rate` is roughly `2500 / 80M ~= 3e-5` (assuming a $400 share price). The temporary impact term is approximately `0.142 * 0.012 * (3e-5)^0.6 ~= 8e-7 fraction ~= 0.0008%`, or under 1 bp per trade. Annualized over a 12-trade monthly rebalance schedule, the impact cost is 1-5 bps annualized.
- Asking the engine to produce 10 bps annualized at $1M notional would require either (a) the formula's calibration to be wrong by a factor of 2-10x, or (b) the engine to silently inflate cost for the test. Neither is acceptable.
- FIM 2018 is preserved as an upper-bound sanity check at 50 bps annualized: the engine's central estimate must come in well below that ceiling, which it will for SPY at $1M but which guards against the (unlikely) class of bug where the cost model returns a fraction interpreted as a percent.

The Plan and reviewer-pass discipline that produced ADR 0005 and ADR 0006 also produced this ADR's narrowing: the cost-model gate is graded on the formula-derived band that the engine implements, not on a number from a paper that calibrates on a different population. The ceiling discipline preserves the spirit of "are we in the right ballpark at all?" without the trap of grading the engine on an inapplicable central estimate.

## Operationalization

PR A of the M2 four-PR split (per ADR 0005 step 16) ships the two unit tests:

- `test_almgren_central_inside_formula_band` asserts the central `eta=0.142` annualized cost estimate is `>= band_low` and `<= band_high` where `band_low = Almgren(eta=0.05, ...)` and `band_high = Almgren(eta=0.30, ...)` for the SPY $1M monthly rebalance fixture.
- `test_almgren_central_below_fim_ceiling` asserts the central annualized cost is `< 0.0050` (50 bps as a fraction).

Both tests live in `tests/execution/cost/test_impact.py` and ship in PR A. No code in this ADR; no test in this ADR.

## What this ADR does not do

- Does not change the formula. `tcost_fraction = (1/2) * gamma * sigma_D * (Q/V_D) * (Theta/V_D)^(1/4) + eta * sigma_D * |Q / (V_D * T)|^beta` per ADR 0005 step 1. Same Almgren et al. 2005 calibration; same `eta=0.142`, `beta=0.6`, `gamma=0.314`.
- Does not change the sensitivity-band requirement. ADR 0002 M2 acceptance criterion 2 (five SPY equity curves on one plot at `eta in [0.05, 0.10, 0.142, 0.20, 0.30]`) is unchanged.
- Does not change the M2 PR split. ADR 0005 step 16's four-PR breakdown (A: cost-model math; B: ImpactedPriceSource + matcher; C: sensitivity-band runner; D: perf-budget CI) remains binding.
- Does not modify ADR 0002 in place. ADR 0002 stays read-only history per the project convention; ADR 0007 supersedes by cross-reference, with a single new line in `docs/decisions/0002-roadmap-review.md` pointing at ADR 0007 for the revised M2 acceptance criterion 1.

## Status

Accepted. The revised M2 acceptance criterion 1 above binds PR A of the M2 split. ADR 0007 ships as a standalone doc-only PR before M2 PR A; the two M2-PR-A test names are reserved here so the PR A scaffold lands cleanly.
