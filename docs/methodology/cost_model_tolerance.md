# Cost-model tolerance contract

Status: locked for M2 PR A.
ADR cross-references: ADR 0005 step 9 (pre-trade vs fill-cost tolerance formula); ADR 0007 (FIM-2018 upper-ceiling sanity check + formula-derived band gate).
Audience: implementers of the matcher (M2 PR B), of the SquareRootImpactMatchingEngine wiring, and reviewers reading test failures from `tests/integration/test_cost_golden_fixture.py`.

## Goal

Define the pre-trade-vs-fill-cost tolerance contract that the matcher in M2 PR B will enforce. The cost model produces two outputs for any given (asset, dt, shares) tuple:

1. `estimate(...)` returned at the start of the bar (when the policy decides whether to commit to the trade).
2. `compute(fill_state)` evaluated at fill time once the bar's `(open, high, low, close, volume)` are realized.

The two outputs **may legitimately differ** when the market state used at estimate time differs from the realized fill state. The tolerance budget below specifies how large the gap is allowed to be before the matcher raises `CostEstimateVsFillMismatchError` (PR B's typed error class).

## Locked formula

```
tolerance_bps = 0.5 + 0.1 * |delta_mid_bps|
```

where `delta_mid_bps` is the bar-mid drift between estimate and fill, in basis points:

```
delta_mid_bps = ((mid_at_fill - mid_at_estimate) / mid_at_estimate) * 10_000
mid_at_estimate = mid at the start of the bar (typically prior close)
mid_at_fill     = (open + close) / 2 of the bar (the daily-bar mid proxy)
```

The `0.1 * |delta_mid_bps|` coefficient is dimensionless. The formula is the one locked in ADR 0005 step 9 after the M2 reviewer pass replaced the original `5 bps + 1 day sigma_D` tolerance (which evaluated to roughly 125 bps for SPY, i.e. not a tolerance at all).

## Worked example

For a SPY-shaped asset at $500.00 prior close, fill bar with open=$500.00 and close=$501.00:

- `mid_at_estimate = 500.00`
- `mid_at_fill = (500.00 + 501.00) / 2 = 500.50`
- `delta_mid_bps = (500.50 - 500.00) / 500.00 * 10_000 = 10 bps`
- `tolerance_bps = 0.5 + 0.1 * 10 = 1.5 bps`

The matcher in PR B verifies `abs(estimate_bps - compute_breakdown.total_bps) <= 1.5` for that fill and raises on violation.

## Tolerance budget components

| Component | Magnitude (bps) | Why |
|---|---|---|
| Commission rounding (cents/share quantization) | <0.1 | The fixed `0.5 bps` base absorbs commission-rounding plus the float-accumulation noise in `Decimal(repr(float))` boundary conversions. |
| Float64 accumulation noise in the Almgren formula | <0.01 | Pinned NumPy 1.26.4 + Polars 1.41.1 give bit-stable arithmetic per the determinism invariant. |
| Estimate-vs-fill mid drift | `0.1 * |delta_mid_bps|` | A 10 bp drift between estimate and fill produces 1 bp tolerance because the cost model's marginal sensitivity to mid is approximately 0.1 (empirically; depends on participation rate). |

## What this doc does NOT constrain

- **VWAP fills.** ADR 0005 step 4 / PR B's `SquareRootImpactMatchingEngine` raises `UnsupportedFillPriceModelError` on `FillPriceModel.VWAP`. The tolerance contract is undefined for VWAP because real VWAP requires intraday tick data not present in v1.
- **Intraday execution slicing.** ADR 0005 step 7 fixes one-fill-per-`(asset, dt)` for daily bars. `T = 1.0` in the Almgren formula; intraday slicing is v1.1.
- **Pre-trade vs realized slippage.** ADR 0005 step 3 fixes `epsilon_bps = 0` at v1; there is no spread proxy that would create a separate slippage-tolerance budget.
- **Permanent-impact register feedback.** PR B's `ImpactedPriceSource` decorator applies permanent impact to subsequent bars' visible prices. The tolerance contract here is per-fill; the cumulative permanent-impact feedback loop is documented in ADR 0007 (queued as ADR 0009 in the corrected numbering) and tested separately.

## Cross-references

- [ADR 0005](../decisions/0005-m2-cost-realism-plan.md) step 9: the formula source and reviewer's rationale for rejecting the original 5-bp-plus-sigma tolerance.
- [ADR 0007](../decisions/0007-fim-2018-demoted-to-upper-ceiling.md): the formula-derived `[eta=0.05, eta=0.30]` band is the cost-model acceptance gate; FIM 2018's 50 bp ceiling is the upper-bound sanity check.
- `tests/execution/cost/test_impact.py::test_estimate_and_compute_are_consistent`: locks the identity `estimate(...) == compute(...).temporary + .permanent + .slippage` to within 1e-10 on the SPY $1M fixture. This identity is the implementation-side guarantee that the formula at estimate-time and fill-time produce the same number when no mid-drift is introduced.
- `tests/integration/test_cost_golden_fixture.py::test_golden_fixture_matches_expected_bands`: exercises the same identity on the 3-bar golden fixture.
- PR B's `tests/integration/test_cost_estimate_vs_fill_tolerance.py` (not yet written): will exercise the `tolerance_bps` formula above on synthetic mid-drift scenarios.

## Notes for future readers

The base `0.5 bps` is the tolerance floor; below this, signal is noise. The `0.1 * |delta_mid_bps|` slope is calibrated for the SPY $1M monthly rebalance regime (Q/V ~ 1e-5, sigma_D ~ 1.2%/day). At significantly higher participation rates (Q/V > 1e-3 for smaller-cap names in M3 worked studies), the marginal sensitivity may exceed 0.1 and the tolerance formula may need to be revisited per a future ADR.
