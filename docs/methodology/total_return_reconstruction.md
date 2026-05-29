# Total return reconstruction

Status: locked for M1; window reframed by ADR 0006.
ADR cross-references: ADR 0002 decisions 6 and 9 (SPY reconciliation tolerance, dividend reinvestment convention); ADR 0001 decision 14 (engine self-validation); ADR 0006 (window reframed to SSGA-published trailing periods anchored on `SSGASpyReference.as_of_date`; snap-backward anchor row; `ExpenseRatioSchedule` for the 2003-11-01 step).
Audience: implementers of the SEP adapter and the M1 reconciliation harness.

## Goal

Reconstruct a SPY total-return series from a price series plus a dividend series, and match SSGA's published SPY NAV TR for the trailing 1Y / 3Y / 5Y / 10Y / SI periods (anchored on SSGA's "Total Returns as of Date" cell) to within **5 basis points annualized per period**. The kill-early gate (end of week 2 per ADR 0002) passes when every reconcilable window passes within tolerance. Any single window FAIL collapses the overall verdict to FAIL and ends the project per [`feedback_kill_early`](../../../.claude/projects/C--Users-SamJD-OneDrive-Desktop-AI-Projects/memory/feedback_kill_early.md); a `POSTMORTEM.md` is written.

The previous window commitment (a single 2005-2024 comparison) was retired by ADR 0006 because SSGA does not publish a 2005-2024 figure: SSGA publishes trailing periods ending at its as-of date. The 5-bp tolerance is unchanged; only the windows changed.

## Authoritative reference

The reconciliation reference is the SPY fund net asset value total return as published by State Street Global Advisors (SSGA, the SPDR brand owner) on the SPY fund page:

- URL: https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy (URL of record; SSGA's older slug `etfs/spy` no longer redirects)
- Source tab: Performance (for the cumulative and annualized NAV TR) and Distributions (for every ex-date and per-share cash amount in the fund's history).
- Snapshot policy: a CSV export of the SSGA Performance and Distributions tabs is committed to `data/snapshots/spy_ssga_<YYYY-MM-DD>/` on each pull. The directory name records the pull date. The SHA256 of each file is recorded in [`dataset_versioning.md`](dataset_versioning.md).
- Initial pull date: 2026-05-28 (the date this document was authored). The first snapshot commit will replace this placeholder with the actual pull date.

Why the SSGA SPY fund NAV TR and not an alternative:

| Candidate | Why not |
|---|---|
| S&P 500 Total Return Index (SPTR) | This is the index SPY tracks, not SPY itself. Gross of all fund-level frictions; reconciling against SPTR validates only the dividend reinvestment math and does not exercise the engine's ability to model real-world fund drag. |
| Yahoo Finance SPY adjusted close | Yahoo computes its adjusted-close series with proprietary methodology that has changed at least twice (2018 dividend handling change, 2022 split adjustment change) without public notice. Non-citable, non-reproducible. |
| Bloomberg SPY TR field | Institution-gated. Not reproducible from the public. |
| CRSP value-weighted index | This is an academic index, not SPY. Differs from SPY by index methodology (CRSP uses delisting returns; SPY does not own delisted constituents) and constituent weights. |

The SSGA-published NAV TR is net of the SPY expense ratio (currently 0.0945% per year per the fund prospectus; verify on every pull, as the ratio has been reduced twice in the fund's history). The engine's reconstruction is gross of fund expenses by default; the reconciliation harness explicitly subtracts a daily expense-ratio accrual before comparing to the SSGA reference. See [Modeling the expense-ratio drag](#modeling-the-expense-ratio-drag) below.

## Reinvestment convention

**Same-day-at-close on ex-date.** On every ex-dividend date `t` the holder receives `D_t` per share in cash, which is reinvested immediately at the ex-date close price `P_t`. The holder's share count grows by `D_t / P_t` per share held.

This convention has two equivalent algebraic formulations:

1. Share-count formulation: if the holder owned `N_{t-1}` shares at the prior close `P_{t-1}`, then on the ex-date `t` the new share count is `N_t = N_{t-1} * (1 + D_t / P_t)`. The portfolio value at `t` is `V_t = N_t * P_t = N_{t-1} * (P_t + D_t)`.

2. Return-index formulation: the standard total-return index update is `TR_t = TR_{t-1} * (P_t + D_t) / P_{t-1}`. On non-ex-dates `D_t = 0` and the formula collapses to a simple price return.

The two are identical because:

```
V_t / V_{t-1} = (N_{t-1} * (P_t + D_t)) / (N_{t-1} * P_{t-1}) = (P_t + D_t) / P_{t-1}
```

The engine implements the return-index formulation because it does not require tracking fractional share counts across the universe. Fractional shares re-enter at M3 for the constant-weight monthly rebalance demo, which is a strategy concern (target weight matching), not a TR reconstruction concern.

Why same-day-at-close and not an alternative:

| Alternative convention | Why not |
|---|---|
| Reinvest at the prior close `P_{t-1}` | Algebraically equivalent to same-day-at-close in the formula above. Reported here for completeness, not as a separate convention. |
| Reinvest at the next day's open `P^{open}_{t+1}` | Introduces a one-day timing gap; the holder is briefly in cash. Standard in practitioner spreadsheets but adds a small (and inconsistent across vendors) bias. SSGA's NAV TR does not use this convention. |
| Reinvest on the payment date (typically 30 days post ex-date) | Realistic for a passive holder; not what SSGA's NAV TR uses. The fund itself receives the cash on payment date and reinvests programmatically. |
| Continuous compounding via implied dividend yield | Used by some academic risk-model implementations. Not what SSGA reports. |

The same-day-at-close convention matches SSGA's published NAV TR methodology and is the standard for U.S. equity total-return index providers (S&P Dow Jones, MSCI, FTSE). It is also the convention used by CRSP for the dispf series used in WRDS-based academic backtests.

## Math

Inputs to the reconstruction:

- `prices: pl.DataFrame` with columns `(dt, close)`, one row per trading day in America/New_York.
- `dividends: pl.DataFrame` with columns `(ex_date, amount_per_share)`, one row per dividend distribution.
- `start_dt`, `end_dt`: the reconciliation window.
- `expense_ratio_annual: Decimal`: the SPY expense ratio for the window. For windows crossing the fund's two ratio reductions (2003 and 2024), the ratio is a step function; the engine handles this by joining a `(effective_dt, expense_ratio)` table from the SSGA prospectus history.

Algorithm:

```
tr[0] = 1.0  (normalized; the final compared quantity is the annualized return, not the level)
for t in 1..T:
    div_t = dividends.amount_per_share where ex_date == dt[t], else 0
    daily_expense_drag = expense_ratio_annual / 252  (deduct one trading-day's expense)
    tr[t] = tr[t-1] * (prices.close[t] + div_t) / prices.close[t-1] * (1 - daily_expense_drag)

annualized_return = (tr[T] / tr[0])^(252 / (T - 1)) - 1
```

The 252-trading-day annualization convention is used because SSGA reports annualized fund performance on a trading-day basis (verified against SSGA's published 1-year, 3-year, 5-year, 10-year NAV TR for SPY).

The engine implements this in `src/pit_backtest/data/adjustments.py` as a pure Polars expression. No Pandas, no NumPy loops; the only loop is at the daily granularity for the cumulative product, which Polars expresses as `cumprod` over the daily multiplier `(P_t + D_t) / P_{t-1} * (1 - expense_drag_t)`.

## Worked example A: toy three-day calculation

A minimal example, with one dividend on day 2, illustrates the reinvestment mechanic without external data dependencies.

| Day | Close `P_t` | Dividend `D_t` | Multiplier `(P_t + D_t) / P_{t-1}` | `TR_t` |
|---|---|---|---|---|
| 0 | 100.00 | 0.00 | (initial) | 1.000000 |
| 1 | 101.00 | 0.00 | 101.00 / 100.00 = 1.010000 | 1.010000 |
| 2 | 100.50 | 1.50 | (100.50 + 1.50) / 101.00 = 1.009901 | 1.020000 |
| 3 | 102.00 | 0.00 | 102.00 / 100.50 = 1.014925 | 1.035224 |

Cumulative TR over the three days: 3.5224%. Plain price return: (102.00 / 100.00) - 1 = 2.0000%. Difference is the dividend contribution, in line with `(P_2 + D_2 - P_2_no_div) / P_1` mechanics.

This example is a unit-test fixture: `tests/data/test_tr_reconstruction.py::test_toy_three_day_with_one_dividend`.

## Worked example B: SPY 2024-03-15 ex-dividend

A real-data example using the SPY Q1 2024 distribution. All values below are illustrative and must be verified against the SSGA snapshot at pull time before they are used in any reconciliation test. The snapshot SHA256 will pin them once the pull lands.

| Day | Close `P_t` (illustrative) | Dividend `D_t` (illustrative) | Multiplier | Notes |
|---|---|---|---|---|
| 2024-03-14 | 517.51 | 0.0000 | (prior) | Trading day before SPY Q1 ex-date. |
| 2024-03-15 | 512.85 | 1.7715 | (512.85 + 1.7715) / 517.51 = 0.99442 | SPY ex-dividend day. Mechanical price drop approximately matches `D_t`; the residual is the day's market move. |

The Q1 2024 distribution amount of $1.7715 per share is taken from the SSGA Distributions tab; the prices are illustrative pending the SSGA pull. The 0.99442 multiplier is exact given those inputs. With no dividend (price-only) the day's return is `512.85 / 517.51 - 1 = -0.900%`; with the dividend reinvested the TR is `0.99442 - 1 = -0.558%`, recovering 34 basis points of return that the price series alone would have understated.

This example becomes a unit-test fixture once the SSGA pull lands: `tests/data/test_tr_reconstruction.py::test_spy_q1_2024_ex_dividend`. The pull-date snapshot pins the exact prices and dividend amount; subsequent restated values do not invalidate the test because the test pulls from the pinned snapshot, not the live SSGA feed.

## Modeling the expense-ratio drag

The SSGA SPY NAV TR is net of the fund's expense ratio.

**Correction per ADR 0008 Decision C:** The original methodology doc claimed "reconstruction from prices and dividends alone is gross of expenses." That claim applies to INDEX reconstruction (using index price + index dividends). It does NOT apply to SPY market-price reconstruction. SPY's closeunadj is the fund's market closing price, which tracks NAV. NAV is computed net of expenses by construction (the prospectus expense ratio is deducted daily from fund assets). Reconstructing TR from SPY market price + SPY dividends therefore approximates SPY NAV TR directly, **already net of expenses**.

The M1 SPY reconciliation harness consequently does NOT apply the `SPY_EXPENSE_RATIO_SCHEDULE` to SPY's TR reconstruction; doing so would double-count the prospectus expense and bias the engine below SSGA NAV TR by ~9 bps annualized. The schedule constant is retained in `engine/spy_reconciliation.py` as documentation of the prospectus history and for potential M3+ callers that reconstruct from an actual S&P 500 INDEX TR rather than from SPY market prices.

SPY expense-ratio history:

| Effective period | Expense ratio (annual) | Source |
|---|---|---|
| 1993-01-22 to 2003-10-31 | 0.1200% (12 bps) | SPY original prospectus. |
| 2003-11-01 to present | 0.0945% (9.45 bps) | SPY prospectus reduction announced 2003-11. |

The reconciliation harness applies the expense ratio as a daily multiplicative drag: `daily_factor = 1 - (annual_ratio / 252)`. Per ADR 0006 the engine uses an `ExpenseRatioSchedule` (an attrs-frozen tuple of `(effective_from, rate)` steps) to model the 2003-11-01 step; the SI window straddles the step and a constant-rate assumption would fail by approximately 2-3 bps annualized for SI. The post-2003-11 windows (10y / 5y / 3y / 1y as of any modern as-of date) are entirely under the 0.0945% rate and behave identically to the scalar-rate case.

`reconstruct_total_return` accepts either a `Decimal` (constant-rate path, byte-for-byte unchanged from the pre-ADR-0006 implementation) or an `ExpenseRatioSchedule` (step-function path). The schedule path joins a precomputed `{effective_from, daily_drag}` frame against the price frame via Polars `join_asof(strategy="backward")`. The boundary convention is `dt == effective_from` picks up the new rate (2003-11-01 itself takes the 0.0945% rate; 2003-10-31 takes 0.12%). The first NYSE trading day at or after 2003-11-01 is 2003-11-03 (Monday); the schedule's `join_asof(strategy="backward")` maps 2003-11-03's `dt` to `effective_from=2003-11-01`, so 2003-11-03 is the first row using the new rate. `Decimal` rates are converted to `float` once at frame construction inside the schedule-path branch; the scalar path uses an inline literal as before.

Worked example for the SI step (synthetic constant 0.03% daily return, no dividends, schedule from ADR 0006):

| Day | Date | NYSE? | Rate (annualized) | Multiplier | TR |
|---|---|---|---|---|---|
| 0 | 2003-10-29 (Wed) | yes | 0.12% (pre-step) | 1.0 (anchor) | 1.0000000000 |
| 1 | 2003-10-30 (Thu) | yes | 0.12% | 1.0003 * (1 - 0.0012/252) = 1.0002952... | 1.0002952... |
| 2 | 2003-10-31 (Fri) | yes | 0.12% | same as day 1 | 1.0005905... |
| 3 | 2003-11-03 (Mon) | yes | 0.0945% (post-step) | 1.0003 * (1 - 0.000945/252) = 1.0002963... | 1.0008869... |
| 4 | 2003-11-04 (Tue) | yes | 0.0945% | same as day 3 | 1.0011833... |

The boundary day (2003-11-03) takes the post-step rate. This worked example ships verbatim as `test_reconstruct_total_return_with_schedule_applies_step_at_boundary` in `tests/data/test_expense_ratio_schedule.py`.

Tracking error and securities-lending revenue are second-order effects (typically 1-3 bps annualized each) and are absorbed by the 5-bps tolerance budget. They are not modeled explicitly.

## Trailing-period window alignment (ADR 0006)

Per ADR 0006 each trailing window is anchored as follows:

- `raw_start = as_of - relativedelta(years=N)` for the 1y / 3y / 5y / 10y windows. `raw_start = SPY_INCEPTION_DATE = 1993-01-22` for the SI window.
- `anchor_dt = max(t in NYSE trading days, t <= raw_start)`. When `raw_start` is itself a trading day, `anchor_dt = raw_start`. When `raw_start` is a weekend or holiday (e.g., `as_of = 2026-04-30` produces a 3y `raw_start = 2023-04-30 Sun`), `anchor_dt` is the most recent trading day before (Friday 2023-04-28 in this case).
- Engine TR window is `[anchor_dt, snapped_as_of]` with `TR[anchor_dt] = 1.0` as the anchor row. Returns accumulate from the next NYSE trading day.

The snap-backward convention matches SSGA's published convention of anchoring period returns at the NAV value on the trading day on or before the calendar period boundary. The Plan's original snap-forward proposal was rejected during the ADR 0006 reviewer pass; snap-forward would have shifted the engine's effective return window by 1-3 trading days relative to SSGA's and could leak 50-150 bps annualized on the 1y window in worst-case price paths (the kill gate is not graded on best cases).

## Tolerance budget

The 5-bps annualized tolerance is the M1 acceptance gate. Components of the expected drift between the engine's reconstruction (gross of fees, with explicit expense-drag applied) and SSGA's published NAV TR:

| Component | Expected magnitude (annualized) | Handled by |
|---|---|---|
| Expense-ratio drag | 9.45 bps (post-2003-11) | Explicit subtraction in the engine. |
| Tracking error vs S&P 500 | 1-3 bps | Absorbed by tolerance. |
| Securities-lending revenue (a credit, not a drag) | 1-2 bps | Absorbed by tolerance. |
| Dividend timing (ex-date vs payment-date) | <1 bp annualized over 20y | Both conventions converge; the convention is same-day-at-close. |
| Floating-point accumulation error in cumprod | <0.1 bp annualized | Polars uses float64; accumulation error is negligible at 5000 bars. |
| Sharadar SEP adjusted-close methodology drift | Unknown | This is the residual the tolerance is sized to detect. Pre-flight: a one-month sanity check on 2024 Q1 vs SSGA before running the full 20-year reconciliation. |

The residual after subtracting the modeled components should be on the order of 3-5 bps annualized; the 5-bps tolerance leaves no safety margin if Sharadar's adjusted-close methodology drifts materially from the SSGA-implied reconstruction. The pre-flight one-month check is the early-warning gate: if the 2024 Q1 reconciliation drifts more than 5 bps for the quarter (equivalent to 20 bps annualized), debug before running the full 20-year window.

## Known sources of drift and their mitigations

1. **Sharadar SEP adjusted close uses a vendor-specific cumulative factor.** Sharadar applies splits and dividends to historical prices using a cumulative adjustment factor on every bar. The engine must use Sharadar's `closeunadj` (raw close) and Sharadar's `dividends` table separately, not the back-adjusted `close`. Conflating these will double-apply dividends and produce TR drift on the order of 100+ bps annualized.

2. **Ex-date convention drift.** Sharadar records dividend ex-dates; SSGA records them as well. Both should match for SPY. A unit test verifies that every dividend in the SSGA snapshot has a matching ex-date in the Sharadar SEP dividend table for SPY within a 1-trading-day tolerance.

3. **Calendar mismatches.** The engine uses pandas-market-calendars' NYSE calendar (US trading days). Sharadar SEP bars exist on every NYSE trading day; SSGA reports NAV on the same calendar. Any bar missing in SEP that is present in SSGA's TR series (or vice versa) raises `CalendarMismatchError` at reconciliation time with the offending dates surfaced.

4. **2024-03 SPDR S&P 500 ETF Trust restructuring.** SPY operated as a unit investment trust (UIT) until a 2024 restructuring to a fund of the same name. Distribution mechanics did not change for unit holders; the legal entity did. The engine treats all SPY history as one continuous series. SSGA's published TR also treats the series as continuous.

5. **Pre-2005 history.** The 5-bps tolerance applies to every reconcilable trailing window. The SI window starts at SPY inception 1993-01-22; Sharadar Premium provides full-history coverage, but the 1993-era SEP bars have historically shown methodology drift across vendors. ADR 0006 keeps SI in the kill-gate set under the same 5-bp tolerance: if 1993-era data quality causes a >5-bp SI delta, that surfaces as a real finding for a follow-up ADR rather than as a graded-on-a-curve PASS.

## Test artifacts

- `tests/data/test_tr_reconstruction.py`: unit tests for the toy three-day fixture and the SPY Q1 2024 fixture. Scalar-rate path unchanged from the pre-ADR-0006 implementation.
- `tests/data/test_expense_ratio_schedule.py`: per-ADR-0006 unit tests for the `ExpenseRatioSchedule` step-function path. Includes the boundary-day agreement test between `rate_for` and the Polars `join_asof("backward")` path, the SPY 2003-10-29..2003-11-04 worked example, and the scalar-vs-schedule back-compat equivalence assertion.
- `tests/integration/test_spy_reconciliation.py`: the M1 end-to-end test (per ADR 0006). Two layers in CI: (1) verdict-aggregation and evidence-line-format unit tests against in-memory `PerWindowResult` values; (2) synthetic-bundle round-trip tests for the all-skipped, partial-coverage, and legacy-CSV cases. The snapshot-gated kill-gate test `test_spy_reconciliation_trailing_periods_snapshot_gated` (marked `@pytest.mark.snapshot` + `@pytest.mark.kill_gate`) runs against the real bundles under `data/snapshots/` and asserts `overall_verdict != "FAIL"`. NEEDS_DATA (bundle does not cover any window) is acceptable as a "needs a fresher pull" signal but is not a PASS.
- The one-quarter preflight (`test_spy_reconciliation_one_quarter_preflight`) is reanchored to the quarter ending SSGA's `as_of_date` and gates on a credible-magnitude bound on the SPY quarterly return; it skips when the bundle does not cover the quarter.
- `data/snapshots/spy_ssga_<YYYY-MM-DD>/`: the pinned SSGA snapshot. SHA256 in [`dataset_versioning.md`](dataset_versioning.md). Tests load from the snapshot, never from the live SSGA feed.

## Cross-references

- ADR 0001 decision 14: engine self-validation against SPY total return is required by M1.
- ADR 0002 decision 6: SPY reconciliation tolerance and reinvestment convention named.
- ADR 0002 decision 9: dividend reinvestment named at the data-layer level.
- [`docs/methodology/dataset_versioning.md`](dataset_versioning.md): Sharadar pull SHA256 commitment for the SEP price and dividend tables.
- [`docs/methodology/determinism.md`](determinism.md): the float64 accumulation tolerance referenced in the tolerance budget.
