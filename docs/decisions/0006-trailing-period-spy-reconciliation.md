# ADR 0006: Trailing-period SPY reconciliation

Status: Accepted.
Date: 2026-05-29.
Authors: Sam Doane (with Plan + skeptical-reviewer pass per the project rule on substantial code).

## Context

ADR 0002 acceptance criterion 1 (the M1 kill-early gate) committed to reconciling a buy-and-hold SPY backtest "from 2005-01-01 through 2024-12-31" against "SPDR-published SPY total return" within 5 basis points annualized. The methodology doc [`docs/methodology/total_return_reconstruction.md`](../methodology/total_return_reconstruction.md) named the SSGA-published SPY NAV TR as the authoritative reference. M1 day 2's `engine/spy_reconciliation.py` operationalized this as a single-window comparison between the engine's 2005-2024 TR and SSGA's published period (default `10y`).

When Sam first ran the gate against real data on 2026-05-29, the windowing was wrong. SSGA does not publish a fixed 2005-2024 figure; SSGA publishes trailing periods (`1y`, `3y`, `5y`, `10y`, `si`) ending at the "Total Returns as of Date" cell on the SPY product page. The current cell reads 2026-04-30. Comparing the engine's 2005-2024 to SSGA's 10y (which is 2016-04-30 to 2026-04-30 trailing) is apples-to-oranges and the kill gate cannot be exercised under the existing API.

ADR 0006 supersedes the window phrasing of ADR 0002 acceptance criterion 1. The 5 bps tolerance is unchanged; the comparison surface changes from a single fixed window to SSGA's published trailing periods. The Plan agent's 1700-word implementation plan and the senior multi-strat-fund quant reviewer's critique (~1600-word) are captured in condensed form below. The reviewer surfaced two Critical and four High findings before any code; all are addressed in the final locked decisions.

This ADR ships the trailing-period reconciliation only. The Frazzini-Israel-Moskowitz 2018 cross-check revision that ADR 0005 step 17 queued for "ADR 0006" is broken out into its own ADR 0007 per the reviewer's split recommendation; the two decisions share no code, no test files, and no risk surface.

## The plan (summarized)

### Goal

Realign `reconcile_spy` from a single 2005-2024 window comparison to a multi-window trailing-period comparison against SSGA's published 1Y / 3Y / 5Y / 10Y / SI annualizations, all anchored on SSGA's `as_of_date` (currently 2026-04-30). The 5 bps annualized tolerance per window is the kill-gate criterion; the gate passes when every reconcilable window passes.

### Files

| File | Status | Kind |
|---|---|---|
| `docs/decisions/0006-trailing-period-spy-reconciliation.md` | new | doc (this ADR) |
| `src/pit_backtest/engine/spy_reconciliation.py` | rewrite | code |
| `src/pit_backtest/data/adjustments.py` | extend | code |
| `examples/spy_buy_and_hold.py` | rewire CLI | code |
| `scripts/pull_m1_data.py` | `--end-date` flag + post-pull date-range assertion | code |
| `tests/integration/test_spy_reconciliation.py` | reframe | test |
| `tests/data/test_expense_ratio_schedule.py` | new | test |
| `tests/data/test_tr_reconstruction.py` | back-compat assertion | test |
| `docs/methodology/total_return_reconstruction.md` | window commitment + boundary conventions | doc |
| `docs/decisions/0002-roadmap-review.md` | cross-reference note | doc |
| `README.md` | evidence-line format + window phrasing | doc |
| `CHANGELOG.md` | Unreleased entry | doc |

Net new positive code is approximately 285 LOC (`engine/spy_reconciliation.py` +120 net, `data/adjustments.py` +35, `examples/spy_buy_and_hold.py` +25, `scripts/pull_m1_data.py` +20, tests +85). The reconciler rewrite is one module crossing 200 LOC; ADR 0006 is the required design record.

### API surface

```python
# src/pit_backtest/engine/spy_reconciliation.py

SPY_PERIOD_TAGS: Final[tuple[str, ...]] = ("1y", "3y", "5y", "10y", "si")
SPY_INCEPTION_DATE: Final[date] = date(1993, 1, 22)

SPY_EXPENSE_RATIO_SCHEDULE: Final = ExpenseRatioSchedule(
    rows=(
        ExpenseRatioStep(effective_from=date(1993, 1, 22), rate=Decimal("0.0012")),
        ExpenseRatioStep(effective_from=date(2003, 11, 1),  rate=Decimal("0.000945")),
    )
)

@attrs.frozen(slots=True)
class PerWindowResult:
    period_tag: str                            # "1y" | "3y" | "5y" | "10y" | "si"
    window_start_dt: date | None
    window_end_dt: date | None
    engine_annualized_return: float | None
    ssga_annualized_return: float | None
    delta_bps: float | None
    n_trading_days: int | None
    verdict: Literal["PASS", "FAIL", "SKIPPED"]
    skip_reason: str | None

@attrs.frozen(slots=True)
class MultiWindowReconciliationReport:
    as_of_date: date
    sharadar_bundle: str
    ssga_bundle: str
    per_window: tuple[PerWindowResult, ...]
    tolerance_bps: float = 5.0

    @property
    def overall_verdict(self) -> Literal["PASS", "FAIL", "NEEDS_DATA"]:
        return _compute_overall_verdict(self.per_window)

    def passes_kill_gate(self) -> bool: ...
    def render_evidence_line(self) -> str: ...

def reconcile_spy_trailing(
    sharadar: SharadarDataSource,
    ssga: SSGASpyReference,
    *,
    expense_ratio_schedule: ExpenseRatioSchedule = SPY_EXPENSE_RATIO_SCHEDULE,
    tolerance_bps: float = 5.0,
    spy_ticker: str = "SPY",
    inception_dt: date = SPY_INCEPTION_DATE,
) -> MultiWindowReconciliationReport: ...
```

### Control flow

1. `as_of = ssga.as_of_date`. If `None` (legacy CSV path), raise `ValueError` with the migration message.
2. Build the NYSE trading-day calendar (module-level frozen `tuple[date, ...]` computed at import time over `[SPY_INCEPTION_DATE, today() + 365 days]`).
3. For each period tag, compute `raw_start = as_of - relativedelta(years=N)` (SI uses `inception_dt`). Snap `raw_start` BACKWARD to the last NYSE trading day `<= raw_start`. This becomes the `anchor_dt`. Snap `as_of` backward to the most recent NYSE trading day `<= as_of` (defensive; SSGA cells are always real trading days in practice).
4. Coverage check: read the SEP frame's `min(dt)` and `max(dt)` once. If the SEP frame is empty for SPY, every window is SKIPPED with `"bundle has no SPY rows"`. If `sharadar_min_dt > anchor_dt` or `sharadar_max_dt < snapped_as_of`, the window is SKIPPED with the coverage reason.
5. For each reconcilable window: read prices and dividends over `[anchor_dt, snapped_as_of]`, call `reconstruct_total_return(prices, dividends, anchor_dt, snapped_as_of, expense_ratio_schedule)`. The returned frame has `TR[anchor_dt] = 1.0`; returns accumulate on every subsequent trading day.
6. Compute `engine_ann = annualized_return(tr_series)`. Compute `ssga_ann = ssga.annualized_nav_tr_for_period(period_tag)`. Assert both are in `[-1.0, 1.0]` (a sanity check on the units; >100% annualized SPY would be a scale-confusion bug). Compute `delta_bps = (engine_ann - ssga_ann) * 10_000`. Verdict is PASS if `abs(delta_bps) <= tolerance_bps`, else FAIL.
7. Aggregate verdict via `_compute_overall_verdict`: any FAIL collapses to FAIL; else at least one PASS collapses to PASS; else (zero passes, all skipped) to NEEDS_DATA.

### Expense-ratio schedule

`ExpenseRatioSchedule` lives in `src/pit_backtest/data/adjustments.py` alongside `reconstruct_total_return`. `reconstruct_total_return`'s `expense_ratio_annual` parameter widens to `Decimal | ExpenseRatioSchedule`. The scalar path is preserved byte-for-byte. The schedule path joins a precomputed `{effective_from: pl.Date, daily_drag: pl.Float64}` frame against the price frame via `join_asof(strategy="backward")`, matching each price row's `dt` to the most recent step with `effective_from <= dt`. The boundary day (`dt == effective_from`) picks up the new rate. Daily drag is applied multiplicatively to the per-row return multiplier as `(close + div) / prev_close * (1.0 - daily_drag)`, identical algebra to the scalar path.

### Test plan

- `tests/integration/test_spy_reconciliation.py`:
  - `test_synthetic_reconciliation_matches_known_annualized_tr` rewires to the multi-window API with a 1y fixture (SSGA `as_of_date` populated, performance frame carrying `1y` matching the expected ann return).
  - `test_reconciliation_report_evidence_line_format` rewires to construct a 5-window report and asserts the byte-for-byte rendering. Three new sibling tests assert the FAIL and NEEDS_DATA rendering.
  - `test_kill_gate_fails_above_tolerance` rewires to a 2-window report with one PASS and one FAIL and asserts the FAIL aggregation.
  - **New** `test_overall_verdict_one_pass_four_skipped_is_pass`: 1 PASS + 4 SKIPPED collapses to PASS, not NEEDS_DATA.
  - **New** `test_overall_verdict_all_skipped_is_needs_data`.
  - **New** `test_overall_verdict_any_fail_is_fail`.
  - **New** `test_coverage_check_empty_frame_returns_skipped` against an empty Sharadar fixture.
  - `test_spy_reconciliation_full_window_2005_2024` renamed to `test_spy_reconciliation_trailing_periods_snapshot_gated`. On Sam's current 2005-2024 bundle every window SKIPS and `overall_verdict == "NEEDS_DATA"`; the test passes. On a post-re-pull bundle the test asserts no window is FAIL.
  - `test_spy_reconciliation_one_quarter_preflight` reanchored to the most recent published quarter ending `as_of_date`, gated on whether `as_of - 90d >= sharadar_min_dt`.
  - **New** `test_trailing_window_anchor_snap_backward`: deterministic unit test on the snap helper. Given a fake calendar and `raw_start = date(2024, 5, 4)` (Saturday), assert the anchor is `date(2024, 5, 3)` (Friday).
- `tests/data/test_expense_ratio_schedule.py` (new):
  - `test_schedule_rate_for_returns_last_step_at_or_before_date`: the standard step-table test. `rate_for(date(2003, 11, 1)) == Decimal("0.000945")`, `rate_for(date(2003, 10, 31)) == Decimal("0.0012")`.
  - `test_schedule_rate_for_at_boundary_returns_new_rate`: the explicit boundary test (`dt == effective_from` returns the new rate). Polars asof drift in a future version would fail this first.
  - `test_reconstruct_total_return_with_schedule_applies_step_at_boundary`: 4-bar fixture spanning 2003-10-29 through 2003-11-04, synthetic 0.03% daily return, no dividends. Asserts the per-day multiplier matches the worked-example values from the Author's response below.
  - `test_reconstruct_total_return_scalar_decimal_unchanged`: the back-compat invariant; the existing toy 3-day fixture reproduced byte-for-byte under the scalar API.
  - `test_rate_for_agrees_with_join_at_every_boundary_day`: parametrized over `(2003-10-31, 2003-11-01, 2003-11-03)`; asserts the scalar lookup and the join's row both return the same rate.
- `tests/data/test_tr_reconstruction.py`: one back-compat assertion that `Decimal("0.000945")` and `ExpenseRatioSchedule(rows=((effective_from=date(1900, 1, 1), rate=Decimal("0.000945")),))` produce identical TR series within 1e-12.

### Edge cases

1. Trailing-window raw start lands on Saturday: snap backward to Friday's NYSE trading day. The Friday close becomes the engine's TR anchor (`TR[Friday] = 1.0`); returns accumulate from Monday.
2. `as_of_date is None` (legacy CSV path): raise `ValueError` with the migration path message.
3. `reconstruct_total_return` anchor row at `anchor_dt`: `TR[anchor_dt] = 1.0` by construction; per-window annualization is well-defined because `n_trading_days >= 2` is guaranteed by the coverage check.
4. SI window expense-ratio step at 2003-11-01: handled by `ExpenseRatioSchedule` via `join_asof(strategy="backward")`. The boundary day takes the new rate.
5. Bundle covers window partially: SKIPPED with reason. Partial coverage is treated as no coverage; a partial 10y window cannot be compared to SSGA's full 10y NAV TR.
6. Bundle has zero SPY rows: SKIPPED with reason; no `TypeError` on a null `min(dt)` comparison.
7. SPY inception 1993-01-22: a Friday NYSE trading day; the SI anchor is the day itself.
8. `as_of` on a non-trading day (defensive): snap backward to the most recent NYSE trading day `<= as_of`.
9. Dividends out of window: `read_actions_dividends(start_dt=anchor_dt, end_dt=snapped_as_of)` filters by date range.
10. Leap-year asymmetry: `relativedelta(years=N)` on Feb 29 maps to Feb 28 the previous year. Snap-backward absorbs the asymmetry consistently across the 1y / 3y / 5y / 10y windows.
11. Decimal-to-float boundary: schedule rates are `Decimal`; the per-day drag column is built once as `float(rate) / 252.0` at frame construction inside the schedule-path branch of `reconstruct_total_return`. Documented in that branch's docstring.

### Design choices for reviewer focus

The Plan agent flagged six choices for the reviewer; the reviewer's verdicts are folded into the Final locked decisions below. Captured for the audit trail:

1. `ExpenseRatioSchedule` shape (attrs-frozen with binary-search `rate_for`, located in `data/adjustments.py`).
2. Skipped-window representation (inline `PerWindowResult.verdict = "SKIPPED"` rather than separate aggregate list).
3. Trailing-window start snap convention (snap-backward, the reviewer changed this from the Plan's snap-forward).
4. Aggregate verdict requires every reconcilable window to pass (not N-of-M).
5. Delete the old single-window API (no deprecation shim).
6. Three-way overall verdict `PASS | FAIL | NEEDS_DATA` (not boolean).

## Skeptical reviewer's response

The senior multi-strat-fund quant reviewer (same persona that critiqued ADRs 0001-0005) returned a structured verdict. The verbatim findings:

### Summary verdict: ship-with-modifications

"The plan is structurally sound and shows real internalization of prior feedback (the schedule abstraction, the three-way verdict, the explicit decision to delete rather than deprecate, the FIM revision phrased as a ceiling). The trailing-period reframing is the right answer to the SSGA windowing problem, and the expense-ratio schedule is the correct abstraction for the 2003-11 step. But there are two genuine correctness landmines the author has not seen: (1) the expense-ratio drag application is underspecified across the step boundary; (2) the forward-snap leakage claim is not derived and is almost certainly wrong on the 1y window. The ADR also overpacks two unrelated decisions and should split."

### Top 5 RIGHT

1. Deletion of `reconcile_spy` and `ReconciliationReport` with no shim; mid-M1 with one caller is precisely when you delete and rewire in the same commit.
2. `ExpenseRatioSchedule` as a first-class type widening `reconstruct_total_return`'s parameter; the back-compat scalar path is the right insurance.
3. All-reconcilable-pass aggregate verdict; the disciplined choice. N-of-M would mask exactly the step-function bug the schedule was introduced to handle.
4. Inline `SKIPPED` verdict with fixed 5-slot ordering; clean evidence-line render and stable JSON shape.
5. FIM revision phrased as upper-ceiling (50 bps annualized) rather than central-estimate target; correctly addresses the SPY-at-$1M-is-sub-scale concern from ADR 0005.

### Top findings WRONG or MISSING

- [Critical] §4.2 step 7 / §4.3 do not specify the expense-ratio boundary convention. The schedule path uses `join_asof` against a precomputed frame, but the plan does not state: (a) whether daily drag is applied to the cumulative TR or to the daily return; (b) what happens on the boundary day `dt == effective_from`; (c) whether the join strategy is "backward" (correct: 2003-11-01 picks up the new 0.000945) or "forward" (silently flips direction). Two correct-looking implementations can produce ~3-4 bps annualized divergence on the SI window, inside the kill-gate budget and undetectable by the synthetic CI tests (single-rate fixture). **Fix:** write the three lines down explicitly, add a boundary-day unit test, and hand-compute a 5-day worked example across the 2003-11 step.
- [Critical] §4.4 forward-snap-only convention will leak more than 1 bp on the 1y window and the math is not shown. On a 1y SPY return of 10%, dropping the first 2 trading days due to forward-snap while SSGA's series includes them can shift the annualized return by 1.5-150 bps depending on what happened on those days. The "<1 bp" claim is plausible on average, not worst-case, and the kill gate is not graded on averages. **Fix:** either work out the worst-case for each window (and accept the tolerance hit), or switch the convention. The reviewer recommended switching to snap-backward with an anchor row aligning the engine to SSGA's "NAV at trading day on or before period anchor" semantics.
- [High] §4.2 step 5 coverage check via `min/max` on `dt` misses the zero-row case. An empty SEP frame returns null min/max and the comparison against `None` raises `TypeError`. **Fix:** branch on `frame.height == 0` before the comparison.
- [High] `scripts/pull_m1_data.py --end-date` does not address as_of synchronization. Same-day pulls can produce a bundle whose actual max(dt) is earlier than the requested end_date without warning. **Fix:** assert `pulled_max_dt >= end_date - 5 trading days` at the end of the pull, raise with a clear message if not.
- [High] §6 test plan rewires the existing synthetic tests but does not exercise the multi-window FAIL or NEEDS_DATA aggregation. The verdict-aggregation logic ships green even if the aggregation is wrong. **Fix:** add explicit synthetic tests for each of the three overall-verdict transitions.
- [High] §4.7 evidence-line format is defined only for the all-PASS case. README and STATUS.md will drift the moment the first FAIL or NEEDS_DATA renders. **Fix:** define all three formats in §4.7 and unit-test each byte-for-byte.
- [Medium] `rate_for` and the join_asof path are two execution paths through the schedule. Two paths means two places to put a boundary-convention bug. **Fix:** test that they agree at every boundary day.
- [Medium] The NYSE calendar cache scope is not specified. **Fix:** module-level frozen tuple computed at import.
- [Medium] Polars asof drift across versions is partially mitigated by the scalar-vs-schedule equivalence test, but the boundary-day semantic is not. **Fix:** add the explicit boundary-day test.
- [Low] §3 LOC budget says "~200 LOC" but the table sums to ~285. Restate the number.

### Gotchas before first line of code

1. PMC calendar range should start at `inception_dt` itself, not earlier; avoids the inception-day off-by-one edge case.
2. `relativedelta(years=N)` on Feb 29: `date(2024, 2, 29) - relativedelta(years=1) = date(2023, 2, 28)`. Asymmetric across the 1y / 3y / 5y windows when `as_of` is Feb 29. Test for it.
3. Scale-unit assertion: SSGA returns decimals (`0.151` for 15.1%) and engine `annualized_return` returns decimals; if either side ever returns percent (`15.1`), every window FAILs spectacularly. Add `assert -1.0 <= ann <= 1.0` defensive checks.
4. `Decimal` vs `float` at the schedule boundary: state explicitly which path converts where.
5. `overall_verdict` as `@property` is non-picklable across multiprocessing pools. Implement as a pure module-level helper that the property delegates to.
6. SI window from 1993-01-22 with Sharadar Premium: data quality on 1993-era bars can be inconsistent across vendors. Decide now whether SI is in the kill-gate set or informational. The reviewer's read: kill-gate set; if SI fails, Sam debugs.
7. `pull_m1_data.py --end-date` does not address ticker-set freezing. Out of scope, but worth a one-line guard in the pull script that records the ticker set.
8. Methodology doc must carry the boundary convention writeup in the same commit. Per project rule "doc drift is a bug."

### ADR-naming recommendation: SPLIT

"Decision A (trailing-period reconciliation) and Decision B (FIM revision) share no code, no test files, no methodology doc, no risk surface, and no PR. The only thing they share is sequencing ('both ship before M2 PR A') and that is a calendar coincidence, not a decision-domain coincidence." The reviewer recommended ADR 0006 cover Decision A only and ADR 0007 be a separate ~80-line doc-only ADR covering Decision B (FIM revision).

### Splitting recommendation: ONE PR for Decision A, one separate doc-only PR for Decision B

"Do not pack the FIM-revision doc into the reconciliation refactor PR; a reviewer should not have to wade through schedule-abstraction code to evaluate whether the FIM ceiling logic is right."

### Closing

"Before writing one line of `ExpenseRatioSchedule`, hand-compute the SI TR for SPY from 1993-01-22 to 2026-04-30 under three boundary conventions, write the three answers down, pick one, justify the pick in one paragraph, and ship the chosen convention with the boundary-day unit test. Then write the rest of the code. Everything else in this review is fixable in PR review; this one is not."

## Author's response

The reviewer is right on every Critical and High finding. The schedule boundary convention (C1) is exactly the silent-correctness class of bug the schedule abstraction was introduced to eliminate; leaving it unspecified would defeat the purpose. The forward-snap leakage (C2) is plausibly fine on the long windows but materially wrong on the 1y window in a worst-case price path, and the kill gate is not graded on best cases. The split into ADR 0006 and ADR 0007 is the right call: a future reader searching "FIM" should not have to dig through a reconciliation refactor.

### Accepted

1. **Snap-backward with anchor-row convention.** The plan's forward-snap is replaced with: `anchor_dt = max(t in NYSE trading days such that t <= raw_start)`. The engine TR window is `[anchor_dt, snapped_as_of]` with `TR[anchor_dt] = 1.0` as the anchor row (no return accumulates on the anchor row). Returns accumulate from the next NYSE trading day. This matches SSGA's published convention of anchoring the period return at the NAV value on or before the calendar period boundary. The worst-case engine-vs-SSGA boundary asymmetry drops to roughly zero modulo the 252-vs-365 day annualization-convention difference; both sides use 252 trading days so even that residual is sub-bp.
2. **Expense-ratio drag boundary convention.** Daily drag is applied to the per-row multiplier as `(close + div) / prev_close * (1.0 - daily_drag)`. This is identical algebra to the scalar-path multiplier; the only change is that `daily_drag` is now a per-row column instead of a scalar literal. The schedule path joins a precomputed `{effective_from: pl.Date, daily_drag: pl.Float64}` frame against the prices frame via `join_asof(strategy="backward")`, with `by_left=None` and the implicit left key `dt`, matching each price row's `dt` to the most recent step satisfying `effective_from <= dt`. The boundary day `dt == effective_from` picks up the new rate. `Decimal` rates are converted to `float` exactly once at frame construction (inside the schedule-path branch of `reconstruct_total_return`); the schedule itself stays `Decimal`-typed so test failures show exact rates.
3. **Worked example for the SI window across the 2003-11 step.** A 5-trading-day fixture with synthetic constant 0.03% daily return (multiplier `1.0003`) and no dividends, spanning 2003-10-29 through 2003-11-04:

   | Day | Date | NYSE? | Pre-step rate (0.0012) daily_drag | Post-step rate (0.000945) daily_drag | Multiplier | TR |
   |---|---|---|---|---|---|---|
   | 0 | 2003-10-29 (Wed) | yes | 4.76190e-6 | n/a | 1.0 (anchor) | 1.000000000 |
   | 1 | 2003-10-30 (Thu) | yes | 4.76190e-6 | n/a | 1.0003 * (1 - 4.76190e-6) = 1.00029524... | 1.00029524... |
   | 2 | 2003-10-31 (Fri) | yes | 4.76190e-6 | n/a | same as day 1 | 1.00059056... |
   | 3 | 2003-11-03 (Mon) | yes | n/a | 3.75000e-6 | 1.0003 * (1 - 3.75000e-6) = 1.00029625... | 1.00088690... |
   | 4 | 2003-11-04 (Tue) | yes | n/a | 3.75000e-6 | same as day 3 | 1.00118334... |

   The boundary day is 2003-11-03, the first NYSE trading day on or after 2003-11-01. The join_asof("backward") matches 2003-11-03's `dt` to the `effective_from = 2003-11-01` step, picking up the new rate. 2003-10-31 matches the `effective_from = 1993-01-22` step. The synthetic test asserts TR values to within 1e-12. Polars version drift in `join_asof` boundary semantics would change the day-3 multiplier from the post-step rate to the pre-step rate (or vice versa) and the test would fail with a clear delta.

4. **Empty-frame coverage branch.** `_check_coverage(sharadar_sep_frame, anchor_dt, snapped_as_of)` returns a `("SKIPPED", reason)` tuple if `frame.height == 0` or if `min/max(dt)` does not bracket the window. `reconcile_spy_trailing` consumes the tuple before any `read_sep_prices` / `read_actions_dividends` call for that period.
5. **`pull_m1_data.py` post-pull range assertion.** After writing the SEP parquet, the script reads it back, computes `actual_max_dt = sep.select(pl.col("dt").max()).item()`, and asserts `actual_max_dt >= end_date - timedelta(days=10)` (10 calendar days is approximately 5 trading days plus weekends; the constant is named in the script for clarity). On failure, the script raises `ValueError` pointing at the as_of-synchronization rule and prints the requested vs actual range so Sam can retry the pull on a later date or re-check Sharadar's coverage.
6. **Synthetic verdict-aggregation tests.** Three new tests added per the reviewer's H3:
   - `test_overall_verdict_one_pass_four_skipped_is_pass`
   - `test_overall_verdict_all_skipped_is_needs_data`
   - `test_overall_verdict_any_fail_is_fail`
   Each constructs a `MultiWindowReconciliationReport` from in-line `PerWindowResult` values and asserts the expected overall verdict and the byte-for-byte evidence line.
7. **Evidence-line formats for all three overall verdicts.** Defined verbatim:

   PASS (all five windows reconcilable):
   ```
   M1 SPY reconciliation: PASS (as_of=2026-04-30, sharadar_bundle=sharadar_2026-05-29, ssga_bundle=spy_ssga_2026-05-29; 1y=+2.10bps PASS, 3y=+1.85bps PASS, 5y=-0.40bps PASS, 10y=+0.95bps PASS, si=+3.10bps PASS)
   ```

   FAIL (one or more reconcilable windows fail):
   ```
   M1 SPY reconciliation: FAIL (as_of=2026-04-30, sharadar_bundle=sharadar_2026-05-29, ssga_bundle=spy_ssga_2026-05-29; 1y=+2.10bps PASS, 3y=+1.85bps PASS, 5y=-0.40bps PASS, 10y=+7.20bps FAIL [tolerance 5.00bps], si=+3.10bps PASS)
   ```

   NEEDS_DATA (no window reconcilable):
   ```
   M1 SPY reconciliation: NEEDS_DATA (as_of=2026-04-30, sharadar_bundle=sharadar_2026-05-29 [coverage 2005-01-03..2024-12-31], ssga_bundle=spy_ssga_2026-05-29; 1y SKIPPED [bundle does not cover 2025-04-30..2026-04-30], 3y SKIPPED [bundle does not cover 2023-04-28..2026-04-30], 5y SKIPPED [bundle does not cover 2021-04-30..2026-04-30], 10y SKIPPED [bundle does not cover 2016-04-29..2026-04-30], si SKIPPED [bundle does not cover 1993-01-22..2026-04-30])
   ```

   Three byte-for-byte unit tests assert each format. `dataset_versioning.md` points at the tests as the source of truth.
8. **`rate_for` and join path share boundary semantics.** `rate_for(d)` uses `bisect_right` over the sorted `effective_from` tuple and returns the step at index `bisect_right - 1`. The schedule-path frame in `reconstruct_total_return` uses `join_asof(strategy="backward")`. A parametrized unit test hits every boundary day (`effective_from` and the day before) and asserts `rate_for(d) == schedule_path_frame.filter(pl.col("dt") == d)["daily_drag"][0] * 252.0` for all boundary days.
9. **Module-level NYSE calendar cache.** A frozen `tuple[date, ...]` computed at import time over `[SPY_INCEPTION_DATE, today() + 365 days]` via `pandas_market_calendars.get_calendar("NYSE").valid_days(...)`. The snap helpers accept the tuple as an argument so they remain pure functions (unit-testable with synthetic calendars) and consume the module-level tuple at the call site. Determinism rule preserved: import-time computation is deterministic given a fixed PMC version, and PMC's NYSE holiday list is data-only (no network calls).
10. **Scale-unit defensive assertion.** Inside `reconcile_spy_trailing`, after computing `engine_ann` and reading `ssga_ann`, assert `-1.0 <= engine_ann <= 1.0` and `-1.0 <= ssga_ann <= 1.0`. On violation, raise `ValueError("scale-unit confusion: engine_ann={engine_ann}, ssga_ann={ssga_ann}; check that both are decimals (0.10) not percent (10.0)")`. Catches the SSGA-CSV-vs-XLSX-percent-vs-decimal mismatch class of bug.
11. **`overall_verdict` as a pure module-level helper.** `_compute_overall_verdict(per_window: Iterable[PerWindowResult]) -> Literal["PASS", "FAIL", "NEEDS_DATA"]`. The `@property` on the report delegates. The helper is unit-tested independently.
12. **SI window in the kill-gate set.** SI is graded under the same 5-bps tolerance as the other windows. The reviewer's read is the correct one: if Sharadar's 1993-era SEP coverage produces a >5-bp delta on the SI window, that surfaces as a real data-quality finding rather than a graded-on-a-curve PASS. ADR 0008 can demote SI to informational if (and only if) the SI failure can be attributed to a documented vendor data issue with no available fix.
13. **ADR split.** ADR 0006 covers Decision A (trailing-period reconciliation) only. ADR 0007 covers Decision B (FIM 2018 demoted to upper-ceiling sanity check for M2 cost-realism gate) as a separate ~80-line doc-only ADR. ADR 0007 ships in its own doc-only PR before M2 PR A; it does not block this PR.
14. **PR split.** ADR 0006 ships as one PR: schedule extension to `data/adjustments.py`, reconciler rewrite, CLI rewire, `pull_m1_data.py --end-date` extension with post-pull assertion, methodology doc update, tests, README and CHANGELOG updates. ADR 0007 ships separately as a doc-only PR before M2 PR A.
15. **LOC budget restated.** Net new positive code is approximately 285 LOC across `engine/spy_reconciliation.py`, `data/adjustments.py`, `examples/spy_buy_and_hold.py`, `scripts/pull_m1_data.py`, and the test files. The Plan's "~200 LOC" was understated; the corrected number stands.

### Contested

None on substance. One emphasis push-back:

The reviewer suggested in Gotcha 7 a one-line guard in `pull_m1_data.py` recording the ticker set in a sidecar manifest. The existing `data/snapshots/manifest.toml` written by `sharadar_pull --refresh-hashes` already records per-file SHA256, size, and row_count, and the bundle name itself records the pull date. The ticker-set-changed-between-pulls scenario is real but the M3 PIT data infrastructure (which introduces `Universe.is_member(asset_id, date)`) is the right place to formalize it. Deferring the ticker-set freeze to M3 is the cleaner path; adding it as a one-liner in M1 would introduce a sidecar file format that M3 would have to migrate or replace. The cost of the gotcha (a Sam pulls SPY/AGG/GLD today and SPY only next month with no warning, then runs reconciliation expecting AGG/GLD evidence and finds nothing) is mitigated by the fact that `pull_m1_data.py` hardcodes `M1_TICKERS = ["SPY", "AGG", "GLD"]`; changing it requires a code edit that a reviewer would flag.

### Final locked decisions

These decisions are binding on the ADR 0006 PR. ADR 0007 covers the FIM revision separately. Revising any decision below requires a superseding ADR.

1. **Trailing-period reconciliation against SSGA's published `1y`, `3y`, `5y`, `10y`, `si`** anchored on `SSGASpyReference.as_of_date`. The 5 bps annualized tolerance per ADR 0002 acceptance criterion 1 applies per window.
2. **Snap-backward anchor convention.** For each period tag, `raw_start = as_of - relativedelta(years=N)` (SI uses `SPY_INCEPTION_DATE = date(1993, 1, 22)`). `anchor_dt = max(t in NYSE trading days, t <= raw_start)`. Engine TR window is `[anchor_dt, snapped_as_of]` with `TR[anchor_dt] = 1.0`.
3. **`ExpenseRatioSchedule` widens `reconstruct_total_return`.** New parameter type `Decimal | ExpenseRatioSchedule`. Scalar path byte-for-byte preserved. Schedule path uses `join_asof(strategy="backward")` matching `dt` to `effective_from`. Boundary day picks up the new rate. Daily drag applied to the per-row multiplier identically to the scalar path. `Decimal` -> `float` conversion happens once at frame construction inside the schedule-path branch.
4. **`SPY_EXPENSE_RATIO_SCHEDULE` constant.** Two rows: `(date(1993, 1, 22), Decimal("0.0012"))` and `(date(2003, 11, 1), Decimal("0.000945"))`. Exported from `engine/spy_reconciliation.py` for the kill-gate default.
5. **Three-way overall verdict.** `Literal["PASS", "FAIL", "NEEDS_DATA"]`. Implemented as `_compute_overall_verdict(per_window)` pure helper; the `@property` delegates. `passes_kill_gate()` returns `True` only on PASS.
6. **All-reconcilable-pass aggregate.** Any FAIL -> overall FAIL. Else any PASS -> overall PASS. Else (all SKIPPED) -> NEEDS_DATA.
7. **`PerWindowResult` skipped-window representation.** Inline `verdict = "SKIPPED"` with `skip_reason`. Optional fields `window_start_dt`, `window_end_dt`, `engine_annualized_return`, `ssga_annualized_return`, `delta_bps`, `n_trading_days` are `None` on SKIPPED rows.
8. **Coverage check covers the empty-frame case.** `frame.height == 0` -> SKIPPED with `"bundle has no SPY rows"`. Non-empty but non-bracketing -> SKIPPED with `"bundle [min..max] does not cover window [anchor..as_of]"`.
9. **Evidence-line formats defined for PASS / FAIL / NEEDS_DATA.** Byte-for-byte tested. `dataset_versioning.md` cross-references the tests.
10. **Scale-unit assertion.** `assert -1.0 <= engine_ann <= 1.0` and `assert -1.0 <= ssga_ann <= 1.0` inside `reconcile_spy_trailing` after computing both. Violation raises `ValueError` with the percent-vs-decimal diagnostic.
11. **Module-level NYSE calendar cache.** Frozen `tuple[date, ...]` over `[SPY_INCEPTION_DATE, today() + 365 days]`. Snap helpers accept the tuple as an argument for unit-testability and consume the module-level constant at call sites.
12. **Old single-window `reconcile_spy` and `ReconciliationReport` deleted.** No deprecation shim. `examples/spy_buy_and_hold.py` rewired in the same commit.
13. **`examples/spy_buy_and_hold.py` `--compare-to-ssga` path** calls `reconcile_spy_trailing` and prints the evidence line. Exit codes: 0 on PASS, 1 on FAIL, 2 on NEEDS_DATA. The non-`--compare-to-ssga` branch keeps `--start-dt` / `--end-dt` for engine-only inspection.
14. **`scripts/pull_m1_data.py --end-date` flag** with default `date.today()`. After pulling, the script asserts `actual_max_dt >= end_date - timedelta(days=10)` and raises with a clear message on failure.
15. **`docs/methodology/total_return_reconstruction.md`** updates the window commitment to SSGA's trailing periods, documents the snap-backward + anchor-row convention, documents the `ExpenseRatioSchedule` and its boundary semantics, and carries the worked example from Author response item 3 verbatim. ADR 0006 is cross-referenced.
16. **`docs/decisions/0002-roadmap-review.md`** receives a single cross-reference line at the M1 acceptance criterion 1 entry pointing at ADR 0006 for the superseding window definition. The original 2005-2024 text stays intact (ADRs are append-only history per the project convention).
17. **SI window in the kill-gate set under the same 5-bp tolerance.** If Sharadar 1993-era data quality causes a >5-bp SI delta, that surfaces as a real finding for ADR 0008 to address (the demotion to informational is a separate decision, not graded into ADR 0006).
18. **ADR 0007 (queued)** covers the FIM 2018 revision as a separate doc-only ADR. The two test names operationalizing the revised M2 criterion 1 (`test_almgren_central_inside_formula_band`, `test_almgren_central_below_fim_ceiling`) ship with M2 PR A.

## Status

Accepted. The PR implementing ADR 0006 begins next. ADR 0007 (FIM revision) ships as a separate doc-only PR before M2 PR A. The 18 locked decisions above bind the implementation; deviations require a superseding ADR.
