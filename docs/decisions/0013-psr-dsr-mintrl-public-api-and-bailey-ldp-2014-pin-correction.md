# ADR 0013: PSR/DSR/MinTRL public API and Bailey-LdP 2014 numerical pin correction

Status: Accepted.
Date: 2026-05-30.
Authors: Sam Doane (with M4 PR 1 Plan + skeptical-reviewer pass; the reviewer surfaced three Critical findings before any code was written, including an arithmetic error in the M3-era methodology doc that propagated through ADR 0002, ADR 0003, and ROADMAP).

## Context

ADR 0001 decision 4 made PSR / DSR / MinTRL non-optional. ADR 0002 acceptance criterion 1 (lines 114 + 370 of the original ADR) committed a numerical pin: `SR_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4 -> DSR=0.971 (within 1e-3)`. The same number appears in ADR 0003 line 513, `docs/ROADMAP.md` line 104, and `docs/methodology/dataset_versioning.md` line 139. The `analytics/sharpe.py` stub docstring at line 33 also carries the pin.

M4 PR 1 (`feat/m4-pr1-analytics-sharpe`) opened with a Plan agent that transcribed the methodology doc's formulas and worked example verbatim. The Plan-reviewer caught three Critical issues:

1. **The DSR=0.971 acceptance pin is wrong.** The methodology doc at `docs/research/sources/methodology-backtest-overfitting.md:181-185` hand-walks the example with incorrect inverse-normal quantile values: it states `Phi_inv(0.96667) = 1.869` and `Phi_inv(0.98774) = 1.624`. The correct values (verified against `scipy.stats.norm.ppf` v1.17.1 on the project venv) are 1.834 and 2.249 respectively. The error on the second quantile is large (~0.625 absolute); these cannot be a roundoff artifact, they are arithmetic mistakes that the original ADR-time review missed.

2. **The methodology doc's DSR formula at line 163 has a typo.** The doc writes:

   `DSR = Phi( (SR_hat - SR_0) * sqrt(T - 1) / sqrt(1 - gamma_3 * SR_0 + (gamma_4 - 1)/4 * SR_0^2) )`

   using `SR_0` inside the `sigma_sq` term. But the PSR formula at lines 197-200 uses `SR_hat` inside the same `sigma_sq`. A single canonical Bailey-LdP convention cannot make both equations right; either the PSR formula has `SR_hat` and DSR (which IS PSR evaluated at `SR* = SR_0`) also has `SR_hat`, or both have `SR_0`. Bailey-LdP 2014 uses the Wald form (asymptotic variance evaluated at the unrestricted MLE `SR_hat`), so line 163's use of `SR_0` is a transcription error.

3. **The Plan agent's no-scipy rationale cited a fabricated determinism-doc clause.** Specifically, the plan said `docs/methodology/determinism.md` lines 24-28 declare scipy "known-broken across platforms"; a grep verifies zero scipy mentions in that file. The rationale needs to be re-defended on first principles.

The Plan-reviewer additionally surfaced:

- **MinTRL return type**: ADR 0003 dec 14 stubs `min_trl(...) -> int` with `math.ceil` in the plan. Bailey-LdP 2012 publishes a real-valued lower bound; the methodology doc itself at lines 261-263 reports the values to one decimal place ("5.1 months", "9.8 months"). Returning an integer loses information about which side of the threshold a given observation count falls.
- **DSR with `n_effective == 1` raising**: the methodology doc at line 214 explicitly defines the N=1 case as DSR degenerating to PSR with `SR* = 0`. Raising contradicts the doc and is API-hostile.

This ADR locks the canonical conventions, corrects the numerical pin everywhere it propagated, and pre-empts the API design choices the M4 PR 1 plan otherwise re-litigates.

## The empirical settlement

Computed with `scipy.stats.norm` v1.17.1 on the project venv (Python 3.12.x; numpy 1.26.4 pinned; scipy not pinned but installed transitively for the verification only; the production code does NOT depend on scipy per Choice E below).

Inputs (locked by ADR 0002 acceptance criterion 1): `sr_hat = 1.5`, `T = 60`, `gamma_3 = -0.5`, `gamma_4 = 5.0`, `v_sr = 0.4`, `n_effective = 30`.

Intermediate values:

| Quantity | Methodology doc claim | scipy ground truth |
|---|---|---|
| `Phi_inv(1 - 1/30) = Phi_inv(0.96667)` | 1.869 | **1.833915** |
| `Phi_inv(1 - 1/(30*e)) = Phi_inv(0.98774)` | 1.624 | **2.248799** |
| `sr_0 = sqrt(0.4) * ((1 - gamma_E) * q1 + gamma_E * q2)` | 1.092 | **1.311328** |

Final DSR under two competing conventions:

| Convention | `sigma_sq` form | Result |
|---|---|---|
| **A (Wald, canonical)** | `1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2 = 4.0` | **DSR = 0.765653** |
| B (Score / Rao; methodology doc line 163 reading) | `1 - gamma_3 * SR_0 + (gamma_4 - 1)/4 * SR_0^2 = 3.3752` | DSR = 0.784892 |

The methodology doc's final printed answer `Phi(1.894) = 0.971` IS internally arithmetically consistent (scipy reports `Phi(1.894) = 0.970887`), but the upstream walk that produces `z = 1.894` is wrong on three counts: the two quantiles and the sigma_sq formula. The probability that all three errors collide into the headline value 0.971 by chance is essentially zero; the doc was written backwards from a target answer that does not derive from the stated inputs under any single coherent convention.

Both Convention A and Convention B are asymptotically equivalent (same null distribution); Convention A is the Bailey-LdP 2014 published form (Wald test statistic). The canonical pin per the published paper is **Convention A: DSR = 0.7657**.

## Locked decisions

### Numerical pin (corrected)

1. **The Bailey-LdP 2014 acceptance pin is `DSR = 0.766 within 1e-3`** (Convention A). This supersedes the prior 0.971 pin at:
   - `docs/decisions/0002-roadmap-review.md` lines 114 and 370
   - `docs/decisions/0003-architecture.md` lines 513 and 789
   - `docs/ROADMAP.md` line 104
   - `docs/methodology/dataset_versioning.md` line 139
   - `src/pit_backtest/analytics/sharpe.py` docstring line 33
   This ADR's adoption updates all five files in the same PR; subsequent M4 PR 1 implements against the corrected pin.

2. **The methodology doc's worked example at `docs/research/sources/methodology-backtest-overfitting.md:181-185` is rewritten** to use the correct quantile values, the Convention A `sigma_sq` form, and the corrected final answer `DSR = 0.766`. A footnote documents the pre-correction error so future readers see why the 0.971 number propagated this far.

3. **The methodology doc's DSR formula at line 163 is corrected to use `SR_hat` in `sigma_sq`**, matching the PSR formula at lines 197-200 and the canonical Wald form. The corrected equation reads:

   `DSR = Phi( (SR_hat - SR_0) * sqrt(T - 1) / sqrt(1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2) )`

### PSR / DSR / MinTRL API contracts

4. **`psr(sr_hat: float, sr_star: float, T: int, gamma_3: float, gamma_4: float) -> float`**. Signature unchanged from the ADR 0003 stub. Raises `ValueError` on `T < 2`; raises `ValueError` on `sigma_sq <= 0` (the algebra-degenerate corner where SR_hat is so large that the variance term goes non-positive). `Phi` via `math.erf` (Python stdlib).

5. **`dsr(sr_hat: float, T: int, gamma_3: float, gamma_4: float, v_sr: float, n_effective: int) -> float`**. Signature unchanged from the ADR 0003 stub except `n_effective` annotated as `int` (clarification, not a change). Raises `ValueError` on `v_sr < 0`. Raises `ValueError` on `n_effective < 1`. **For `n_effective == 1`, returns `psr(sr_hat, 0.0, T, gamma_3, gamma_4)`** per the methodology doc's degeneracy clause at line 214; does NOT raise. Internal: derives `sr_0` from the False Strategy Theorem benchmark and calls `psr(sr_hat, sr_0, T, gamma_3, gamma_4)`. `_phi_inv` via the Acklam (1998) public-domain polynomial approximation, with absolute error under 1.15e-9 over `[1e-15, 1 - 1e-15]`.

6. **`min_trl(sr_hat: float, sr_star: float, alpha: float, gamma_3: float, gamma_4: float) -> float`**. ADR 0003 dec 14 stubbed `-> int`; **this ADR amends the return type to `-> float`** matching the Bailey-LdP 2012 published form (real-valued lower bound). The doc reports values to one decimal place ("5.1 months"); the float return preserves that precision and lets a caller `math.ceil` when an integer period count is needed. Raises `ValueError` on `alpha` outside `(0, 1)`; raises `ValueError` on `sr_hat <= sr_star` (the formula has no positive lower bound when the strategy never exceeds the threshold).

### Domain-violation behavior (Choice B in the M4 PR 1 plan)

7. **Raise `ValueError` loudly** on every domain violation (`T < 2`, `sigma_sq <= 0`, `v_sr < 0`, `n_effective < 1`, `alpha` outside `(0, 1)`, `sr_hat <= sr_star` in MinTRL). Matches the codebase discipline at `data/contracts.py` (`LookaheadLeakError`), `data/sources/sharadar.py` (`PriceNotFoundError`, `TickerNotFoundError`, `DelistingDataQualityError`), and `analytics/sensitivity.py`. Returning NaN would silently propagate through the M4 PR 5 scorecard renderer; raising surfaces operator-fixable input mistakes at the call site.

### Euler-Mascheroni constant (Choice C in the M4 PR 1 plan)

8. **`_EULER_MASCHERONI = 0.5772156649015329`** declared as a module-level `Final[float]` at the top of `analytics/sharpe.py`. Hardcoded at 16 significant digits (the maximum precision IEEE-754 binary64 can represent for this irrational constant). The docstring cites the methodology doc and the Wikipedia "Euler-Mascheroni constant" page. No dependency import for one float constant.

### MinTRL return type (Choice D in the M4 PR 1 plan, amended here)

9. **`min_trl(...) -> float`** per locked decision 6 above. Bailey-LdP 2012 publishes the real-valued lower bound; the M4 PR 1 plan's `-> int` with `math.ceil` rounding was a misreading of the "minimum" qualifier. Callers that need an integer apply `math.ceil` at the call site.

### Test pin tolerance (Choice E in the M4 PR 1 plan)

10. **The acceptance pin is `pytest.approx(0.766, abs=1e-3)`** verbatim per ADR 0002 (as corrected by this ADR). M4 PR 1 ships at least one regression test pinning this assertion against the corrected inputs, plus a tighter informational test pinning `pytest.approx(0.7657, abs=1e-4)` for visibility on which side of the boundary the implementation lands.

### scipy dependency (Choice A in the M4 PR 1 plan, re-defended here)

11. **scipy is NOT pinned in `pyproject.toml`.** The first-principles rationale (replacing the M4 PR 1 plan's fabricated determinism-doc citation):
    - `pyproject.toml` carries five scientific pins (polars, numpy, pydantic, attrs, pandas-market-calendars); adding scipy grows the dependency lock surface for two function calls (`norm.cdf` and `norm.ppf`).
    - `math.erf` (Python stdlib since 3.2) provides `Phi` to ~1e-15 absolute precision; the Acklam 1998 polynomial provides `Phi_inv` to ~1e-9 absolute precision. The 1e-3 acceptance pin tolerates the 1e-9 inverse-CDF accuracy with three orders of magnitude of headroom.
    - The Acklam algorithm is the canonical PSR/DSR/MinTRL implementation reference in the empirical-quant literature (quantstrat, portfoliooptimizer.io, marti.ai). It is public-domain; the coefficients are stable since 1998.
    - The project venv on the author's machine ships scipy 1.17.1 transitively (likely through `pandas-market-calendars` or `nasdaq-data-link`); production code does NOT import it. The verification for this ADR was performed using scipy on the venv as ground truth, but the M4 PR 1 implementation will not import it.
    - If a future M4 PR 4 (trial registry) needs `scipy.stats.kendalltau` for the effective-N clustering (PCA fallback only), the dependency decision is re-litigated at that ADR; this ADR does not bind PR 4.

12. **`_phi(z: float) -> float`** uses `0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))`. **`_phi_inv(p: float) -> float`** uses the Acklam (1998) polynomial approximation; coefficients hardcoded at the top of `analytics/sharpe.py` with the Acklam citation in the docstring and a `Final[tuple[float, ...]]` annotation per mypy strict.

## What this ADR does NOT do

- Does NOT introduce or re-litigate any architectural decision beyond the corrections above. PSR/DSR/MinTRL are pure scalar arithmetic; no protocol surface changes.
- Does NOT pre-commit to the MinTRL float return propagating through the M4 PR 5 scorecard. The scorecard renderer can `math.ceil(min_trl(...))` at its rendering boundary; that is M4 PR 5's choice.
- Does NOT bind M4 PR 4 (trial registry) on the effective-N data type. PR 4's `n_effective` plumbing can be `int` (rounded at the registry boundary) or `float`; the `dsr()` signature in this ADR is `n_effective: int` per decision 5, so PR 4 rounds.
- Does NOT add scipy to `pyproject.toml`. See decision 11.
- Does NOT amend the M3 ADRs (0009, 0010, 0011, 0012) or the M2 ADRs (0006, 0007, 0008). Only ADR 0002 (acceptance criterion 1), ADR 0003 (dec 14 docstring + the reviewer-list line at 789), the methodology doc, the ROADMAP, and the sharpe.py stub docstring are amended.
- Does NOT introduce a `confidence_tier` raise on the corrected DSR; that wiring is M4 PR 5's job. The M4 PR 1 acceptance test consumes `dsr(...)` directly and asserts against `0.766 within 1e-3` per decision 10.

## Cross-references

- ADR 0001 decision 4 (PSR/DSR/MinTRL non-optional). UNCHANGED.
- ADR 0002 acceptance criterion 1 (lines 114 + 370): SUPERSEDED IN PLACE in this PR (the wrong 0.971 pin replaced with 0.766).
- ADR 0003 decision 14 (analytics module split): docstring at line 513 superseded in place; reviewer commentary at line 789 also corrected.
- `docs/research/sources/methodology-backtest-overfitting.md` lines 163 and 181-185: corrected in place with a pre-correction-error footnote.
- `docs/ROADMAP.md` line 104: corrected.
- `docs/methodology/dataset_versioning.md` line 139: corrected.
- `src/pit_backtest/analytics/sharpe.py` line 33: corrected docstring (function body still raises `NotImplementedError("M4 deliverable")`; the body lands in M4 PR 1).

## Status

Accepted. M4 PR 1 implements PSR + DSR + MinTRL against the contracts locked above. Revisiting any decision requires a superseding ADR.
