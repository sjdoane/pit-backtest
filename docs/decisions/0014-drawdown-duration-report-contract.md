# ADR 0014: DrawdownDurationReport contract and `drawdown_duration_days` return-type widening

Status: Accepted.
Date: 2026-05-30.
Authors: Sam Doane (with M4 PR 2 Plan + skeptical-reviewer pass; the reviewer surfaced the cascade through `scorecard.py:38` and `tests/test_scaffold.py:156` that the original plan's amendment-footer-only choice did not address).

## Context

The M4 PR 1 post-impl reviewer (PR #32 / `feat/m4-pr1-analytics-sharpe`) flagged the `analytics/drawdown.py:13` stub `drawdown_duration_days -> int` as mirroring the same mis-typing ADR 0013 corrected for `MinTRL`. Lopez de Prado (2018), *Advances in Financial Machine Learning*, chapter 13, treats longest-drawdown-duration as a censored survival-analysis quantity: when the equity curve ends underwater (the backtest window cuts off before the strategy recovers to its prior peak), the realized duration is an interval-censored observation, not an integer count of completed-recovery days. A bare `int` return loses the censoring flag and forces the M4 PR 5 scorecard renderer to either fabricate a recovery date or silently misrepresent the censored case.

The M4 PR 2 Plan agent proposed an `attrs.frozen` record `DrawdownDurationReport(days, is_censored_at_end, peak_dt, trough_dt)` to carry the four LdP-honest fields, with the change documented via a 100-word ADR 0003 amendment footer (the M3 PR 5c precedent at `docs/decisions/0002-roadmap-review.md:406-408`).

The Plan-reviewer pass rejected the amendment-footer-only choice on the facts: the `int` annotation propagates through THREE concrete files that all have their own contracts, not just one ADR sketch:

1. `src/pit_backtest/analytics/drawdown.py:13` (the stub being replaced).
2. `src/pit_backtest/analytics/scorecard.py:38` (a Pydantic field annotation: `drawdown_duration_days: int` on the `RunsAndDrawdowns` model).
3. `tests/test_scaffold.py:156` (a regression-test constructor call: `drawdown_duration_days=10`).

When `drawdown_duration_days()` starts returning `DrawdownDurationReport`, two cascade effects fire:

- The scorecard's `RunsAndDrawdowns.drawdown_duration_days: int` field either (i) continues to hold an `int`, forcing an adapter to convert `DrawdownDurationReport -> int` at the scorecard boundary (with the field's semantic silently changing from "the duration" to "the `.days` projection of the report"), OR (ii) gets retyped to carry the full record (in which case the test_scaffold.py:156 constructor call breaks).
- The render-path enforcement test at `tests/test_scaffold.py:122-191` depends on the scorecard model accepting the constructor call without errors; a type change there must coordinate with the test fixture.

The ADR 0013 precedent applies on the cascade-shape axis, not the numerical-pin-vs-type-annotation axis the original plan tried to draw: when a contract change propagates through multiple files, the prep PR + ADR + corrections land together so the implementation PR ships against a frozen contract with no cascade risk. The 100-word amendment footer is appropriate for deferred-scope items (ADR 0002 line 406's M5 plot deferral); it is insufficient for live-code contract changes.

This ADR locks the `DrawdownDurationReport` shape and the scorecard model retype so M4 PR 2's implementation PR has a stable target.

## Locked decisions

### `DrawdownDurationReport` record

1. **`DrawdownDurationReport` is an `attrs.frozen(slots=True)` record** declared at the top of `src/pit_backtest/analytics/drawdown.py` (above the function stubs). Four fields:

```python
@attrs.frozen(slots=True)
class DrawdownDurationReport:
    days: int
    is_censored_at_end: bool
    peak_dt: date
    trough_dt: date | None
```

   Semantics per LdP 2018 chapter 13:
   - `days`: integer count of bars in the longest underwater run (an underwater bar has `nav < running_peak`). For a flat curve with no drawdown, `days == 0`.
   - `is_censored_at_end`: `True` when the longest underwater run's last bar is the last bar of the equity curve (recovery is censored by the backtest window cutoff). `False` otherwise.
   - `peak_dt`: the date of the last bar BEFORE the longest underwater run (the high-water mark from which the drawdown started). For a flat curve, this equals the first bar's date.
   - `trough_dt`: the date at which `nav` reached its minimum within the longest underwater run. `None` when the curve never went below its first-bar peak (flat curve case).

2. **`attrs.frozen(slots=True)`** matches the existing record discipline at `analytics/sensitivity.py:28` (`SensitivityBand`). Immutability is enforced by attrs; `slots=True` keeps the memory layout tight per `docs/methodology/pydantic_polars_boundary.md`.

### `drawdown_duration_days` -> `drawdown_duration_report`

3. **The `analytics.drawdown` function is renamed from `drawdown_duration_days` to `drawdown_duration_report`** in M4 PR 2 to reflect the widened return type. The stub at `src/pit_backtest/analytics/drawdown.py:13` is replaced; the old name is NOT preserved as an alias (no deprecation cycle; the function has never had a real body and no downstream production code imports it; only the test_scaffold.py constructor call and the scorecard field need updates, both of which land in this ADR's prep PR).

### Scorecard model retype

4. **`RunsAndDrawdowns.drawdown_duration_days: int` at `src/pit_backtest/analytics/scorecard.py:38` is replaced by `drawdown_duration: DrawdownDurationReport`.** The field name is changed alongside the type so a reader scanning the scorecard model sees the new shape immediately. The M4 PR 5 scorecard `to_markdown()` renderer reads `.days` for the integer count and renders the censored flag and trough date alongside, per the LdP chapter 14 scorecard convention.

5. **`tests/test_scaffold.py:156` constructor call updated** from `drawdown_duration_days=10,` to `drawdown_duration=DrawdownDurationReport(days=10, is_censored_at_end=False, peak_dt=date(2024, 3, 1), trough_dt=date(2024, 3, 11)),`. This is the smallest synthetic value that exercises the record without affecting the render-enforcement-test contract (the test asserts that raw SR alone without PSR/DSR raises `RenderEnforcementError` under the CPCV tier; the drawdown report's specific values are immaterial to that assertion).

### Pydantic model serialization

6. **Pydantic v2 serializes `attrs` classes natively as long as they expose their fields**, which `attrs.frozen` does via `__attrs_attrs__`. The `RunsAndDrawdowns` model's `model_config` already accepts arbitrary types via the `_SCORECARD_CONFIG` definition at `scorecard.py:23` (which uses `arbitrary_types_allowed=True` per the pattern at the Pydantic v2 boundary). The field declaration `drawdown_duration: DrawdownDurationReport` is consistent with the existing Pydantic-at-boundary convention from ADR 0003 decision 1.

### ADR 0003 amendment footer

7. **A short amendment footer is added to ADR 0003** at the end of the analytics-module package-layout block (after `docs/decisions/0003-architecture.md:64`), documenting that the M4 PR 2 prep PR (this ADR) widened `drawdown.duration` from `int` to `DrawdownDurationReport`. The footer is informational; ADR 0014 carries the canonical decisions.

## What this ADR does NOT do

- Does NOT modify `analytics/drawdown.py`'s function bodies. The three function stubs (`max_drawdown`, `drawdown_duration_report`, `calmar_ratio`) stay as `NotImplementedError("M4 deliverable")` after the type widening; M4 PR 2 implements them against the now-locked contract.
- Does NOT introduce a `time_under_water` function per LdP 2018 chapter 13's Triple Penance Rule. The methodology research note at `docs/research/sources/methodology-afml-backtesting.md:427-440` flagged the original PDF as inaccessible; the closed-form TuW vs realized TuW comparison is M5 worked-study scope, not this PR's.
- Does NOT touch `analytics/concentration.py`, `analytics/distribution.py`, `analytics/scorecard.py:to_markdown()`, `validation/cv.py`, or `validation/trial_registry.py`. M4 PR 2 implements the first two; M4 PR 3-5 carry the rest.
- Does NOT add scipy or numpy to `pyproject.toml`. ADR 0013 decision 11's stdlib-only constraint for analytics stands.
- Does NOT amend the M3 ADRs (0009, 0010, 0011, 0012), the M2 ADRs (0006, 0007, 0008), or the ADR 0013 contract. Only ADR 0003's package-layout block at line 61 gets the amendment footer.
- Does NOT introduce a deprecation alias for `drawdown_duration_days`. The function has never had a real body; no production code imports it; M4 PR 2 ships against the new name and signature directly.

## Cross-references

- ADR 0001 decision 4 (PSR/DSR/MinTRL non-optional; the analytics layer is the LdP chapter 14 scorecard surface). UNCHANGED.
- ADR 0002 acceptance criterion 1 (Bailey-LdP 2014 DSR=0.766 per ADR 0013). UNCHANGED.
- ADR 0003 decision 14 (analytics module split + `drawdown.py` shape): amendment footer added by this ADR; the underlying module decomposition unchanged.
- ADR 0013 (PSR/DSR/MinTRL public API + Bailey-LdP 2014 pin correction): precedent for the prep-PR-before-implementation pattern this ADR follows.
- `docs/research/sources/methodology-afml-backtesting.md:427-440` (LdP 2018 chapter 13 Triple Penance Rule + censored-duration framing).
- `src/pit_backtest/analytics/drawdown.py:13` (the stub being replaced; M4 PR 2 lands the body).
- `src/pit_backtest/analytics/scorecard.py:38` (the field retype landing in this prep PR).
- `tests/test_scaffold.py:156` (the constructor-call update landing in this prep PR).
- `src/pit_backtest/analytics/sensitivity.py:28` (the `attrs.frozen(slots=True)` record precedent).

## Status

Accepted. M4 PR 2 implements `max_drawdown`, `drawdown_duration_report` (returning `DrawdownDurationReport`), `calmar_ratio`, `hhi`, and `BacktestPathDistribution.percentiles`/`median`/`p10`/`p90` against the contract locked above. Revisiting any decision requires a superseding ADR.
