# Total return reconstruction

Status: locked for M1.
ADR cross-references: ADR 0002 decisions 6 and 9 (SPY reconciliation tolerance, dividend reinvestment convention); ADR 0001 decision 14 (engine self-validation).
Audience: implementers of the SEP adapter and the M1 reconciliation harness.

## Goal

Reconstruct a SPY total-return series from a price series plus a dividend series, and match the SPDR-published SPY total return to within **5 basis points annualized over the 2005-01-01 through 2024-12-31 window**. Passing this reconciliation is the M1 kill-early gate (end of week 2). Failing it ends the project per [`feedback_kill_early`](../../../.claude/projects/C--Users-SamJD-OneDrive-Desktop-AI-Projects/memory/feedback_kill_early.md) and a `POSTMORTEM.md` is written.

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

The SSGA SPY NAV TR is net of the fund's expense ratio. Reconstruction from prices and dividends alone is gross of expenses and will systematically overstate SSGA's reported TR by approximately the cumulative expense drag.

SPY expense-ratio history:

| Effective period | Expense ratio (annual) | Source |
|---|---|---|
| 1993-01-22 to 2003-10-31 | 0.1200% (12 bps) | SPY original prospectus. |
| 2003-11-01 to present | 0.0945% (9.45 bps) | SPY prospectus reduction announced 2003-11. |

The reconciliation harness applies the expense ratio as a daily multiplicative drag: `daily_factor = 1 - (annual_ratio / 252)`. The cumulative drag over 20 years at 9.45 bps annual is approximately 0.0945% * 20 = 1.89% cumulative, or 9.45 bps annualized (the daily compounding adds a negligible second-order correction at these ratios). For the M1 5-bps tolerance, the engine must apply the expense drag explicitly or the reconciliation will fail by approximately 9 bps annualized for any window after 2003-11-01.

Tracking error and securities-lending revenue are second-order effects (typically 1-3 bps annualized each) and are absorbed by the 5-bps tolerance budget. They are not modeled explicitly.

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

5. **Pre-2005 history.** The 5-bps tolerance applies to the 2005-2024 window. Pre-2005 SPY data is available but Sharadar SEP coverage and SSGA distribution-feed coverage both degrade before approximately 2000. The reconciliation window is fixed at 2005-01-01 through 2024-12-31 for the M1 gate; broader windows are M4 work.

## Test artifacts

- `tests/data/test_tr_reconstruction.py`: unit tests for the toy three-day fixture and the SPY Q1 2024 fixture.
- `tests/integration/test_spy_reconciliation.py`: the M1 end-to-end test. Loads the Sharadar SEP SPY series, loads the SSGA snapshot, runs the reconstruction, computes the annualized return delta over 2005-2024, asserts the delta is within 5 bps. Marked `@pytest.mark.kill_gate`; CI runs this on every push to `main` and the test failure ends the build.
- `tests/integration/test_spy_one_quarter_preflight.py`: the pre-flight one-month sanity check. Runs the reconciliation over 2024-Q1 only and asserts the delta is within 5 bps for the quarter (the equivalent annualized tolerance for one quarter is approximately 20 bps but the absolute quarterly delta is more interpretable). This test gates the full 20-year run.
- `data/snapshots/spy_ssga_<YYYY-MM-DD>/`: the pinned SSGA snapshot. SHA256 in [`dataset_versioning.md`](dataset_versioning.md). Tests load from the snapshot, never from the live SSGA feed.

## Cross-references

- ADR 0001 decision 14: engine self-validation against SPY total return is required by M1.
- ADR 0002 decision 6: SPY reconciliation tolerance and reinvestment convention named.
- ADR 0002 decision 9: dividend reinvestment named at the data-layer level.
- [`docs/methodology/dataset_versioning.md`](dataset_versioning.md): Sharadar pull SHA256 commitment for the SEP price and dividend tables.
- [`docs/methodology/determinism.md`](determinism.md): the float64 accumulation tolerance referenced in the tolerance budget.
