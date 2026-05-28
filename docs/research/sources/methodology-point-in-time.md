# Point-in-Time Data Treatment

Covers: survivorship bias, PIT semantics across five data axes, vendor landscape (CRSP, Compustat, Norgate, Sharadar, Refinitiv), common backtester errors, and hooks into pit-backtest design.

Research conducted: 2026-05-28.

---

## Executive Summary

- **PIT is not one problem, it is five.** Price adjustments, fundamentals reporting lags, index membership history, corporate-action date semantics, and analyst estimate revisions each have distinct failure modes. Conflating them produces a compound bias that is impossible to decompose after the fact.
- **Survivorship bias magnitude is strategy-dependent.** For a broad equal-weight S&P 500 strategy, the CAGR overstatement is approximately 1.4 to 1.5 percentage points per year. For concentrated small-cap or momentum strategies, the distortion can exceed 25 percentage points CAGR and eliminate an apparent Sharpe of 0.76 entirely. Academic estimates on mutual fund data (Brown et al., 1992; Carhart, 1997) put the bias at 80 to 150 bps per year in that domain.
- **Vendor support is uneven.** CRSP and Compustat Snapshot are the academic gold standards but are expensive and institution-gated. Norgate Data and Sharadar (Nasdaq Data Link) are the most cost-accessible PIT-correct sources for independent practitioners; each has documented coverage limits.
- **McLean-Pontiff (2016) quantifies the cost of ignoring PIT.** Of 97 published anomalies, post-publication returns decay by 58%; part of that decay is consistent with data-mining bias enabled by non-PIT historical data.
- **Design implication.** The pit-backtest data layer must enforce the distinction between `simulation_dt` (what the strategy sees) and `knowledge_dt` (when data became available), reject any data feed that conflates the two, and require explicit delisting outcomes for every asset in the universe.

---

## Sources Accessed

Primary sources fetched directly: CRSP PERMNO page, CRSP dsp500list schema (SMU library), Norgate Data content tables, Concretum Group Python walkthrough, I/B/E/S PIT Thomson Reuters press release, Analytical Platform survivorship article, WRDS GitHub issue for Compustat date fields, Oreilly/Wiley appendix for Compustat Snapshot description.

Partially available (marketing text only; schema-level docs login-gated): Refinitiv/LSEG PIT Fundamentals page, Sharadar/Nasdaq Data Link SF1 documentation (schema reconstructed from integration docs and secondary sources).

Academic papers accessed via abstract + Semantic Scholar / IDEAS/REPec summaries (PDFs were binary-encoded at fetch time): Brown et al. (1992), Shumway (1997), Shumway-Warther (1999), Carhart (1997), McLean-Pontiff (2016), Harvey-Liu (2020).

---

## What "Point in Time" Means

Point-in-time (PIT) data is data constrained to what was knowable on a given simulation date, excluding any information that, in reality, would have been unavailable. The concept sounds simple but fractures into at least five distinct axes.

### Axis 1: Price-Level Adjustments

Stock prices are backward-adjusted for splits and dividends so the series is continuous. The failure mode: a strategy computes book-to-price using a price denominator back-adjusted to today's cumulative factor, but a fundamentals numerator taken from the original filing. A 10-for-1 split in 2015 divides all pre-2015 prices by 10; the ratio becomes 10x larger in the pre-split period, producing a phantom value signal.

The correct semantics require two time coordinates: `simulation_dt` (the bar being processed) and `perspective_dt` (the date from whose vantage point adjustments are computed). Zipline-reloaded applies adjustments as of the current bar, not as of today. See also [[research-existing-backtesters]] Zipline section for the Pipeline API implementation.

### Axis 2: Fundamentals, Announcement vs Reported vs Restated

Compustat records two key dates for each quarterly observation:

- **datadate**: period end (e.g., September 30). This is NOT the availability date.
- **rdq**: Report Date of Quarterly earnings, i.e., the actual public release date.

A naive backtest sorting on `datadate + 1 day` uses data that was not public for another 45 to 60 days. More than 50,000 quarterly WRDS observations have `rdq` falling beyond even a 90-day lag, so a fixed-lag assumption is incorrect for a material fraction of the universe.

The second layer is restatements. Compustat's default delivery file overwrites original values with the most recently restated figure. Revenue reported at $1.2B in November 2018, later restated to $1.1B in 2019, appears as $1.1B in any backtest run today. Lyle (2024, Yale SOM working paper) documents that Compustat re-standardizes the same firm-quarter an average of 6.3 times, with a mean interval of 685 days between updates.

**Compustat Snapshot** (from March 1987) is the PIT-correct product: it appends rather than overwrites, giving each update a new row with an activation date. The standard FUNDA/FUNDQ files are NOT PIT-correct for restatements.

### Axis 3: Universe Membership, Index Constituency Over Time

Using today's S&P 500 members to backtest 20 years introduces two errors simultaneously: survivorship bias (all current members survived) and preinclusion look-ahead bias (some members were not yet in the index during the historical period).

Quantified (Analytical Platform, 2025): broad equal-weight S&P 500 strategy: 1.45 pp CAGR overstatement. Small-cap tilt (20 smallest members): 26.84 pp CAGR overstatement.

The correct structure is a membership table of `(permno, start_date, end_date)` triples. CRSP's `crsp_a_indexes.dsp500list` provides this with fields `PERMNO`, `mbrstartdt`, `mbrenddt` (coverage from 1958). Caveat: the table is updated annually; active members at pull time receive `mbrenddt` equal to the last trading day of the most recent full year.

### Axis 4: Corporate Actions, Effective Date vs Declaration Date vs Ex-Date

A dividend's timeline runs: declaration date (board announces), record date (shareholders of record are determined), and ex-date (stock opens at reduced price, typically one business day before record date). A backtest that adjusts returns using the declaration date charges the dividend haircut one or two days too early.

All adjustment factors must be applied as of the ex-date. CRSP records ex-dates for dividends and stock splits, which is the academically correct convention. Vendors that record the declaration date will introduce a timing error in any daily or higher-frequency backtest.

### Axis 5: Analyst Estimates, Originally Published vs Current Consensus

The standard I/B/E/S history file stores only the most recently revised estimate for each analyst-period pair. If an analyst revised five times in a quarter, only the final revision is retained. An earnings-surprise strategy using this file sees a consensus that already incorporates all pre-announcement revisions, including those made after any simulation date.

The I/B/E/S Point in Time product (Thomson Reuters / LSEG, launched December 2017) addresses this with daily snapshots from January 2000 (activation dates to January 1980). Key field: **Point Date** identifies the file-date on which an estimate appeared. No data is overwritten. Coverage: 80,000 companies, 100+ countries.

---

## Survivorship Bias

### Definition

Survivorship bias in financial research occurs when the sample is restricted to assets that survived through the end of the observation period, excluding those that were delisted, merged, went bankrupt, or were otherwise removed from the investable universe. The surviving sample systematically excludes the worst outcomes, inflating measured returns.

### Empirical Magnitude

**Mutual fund studies.** Brown, Goetzmann, Ibbotson, and Ross (1992), Review of Financial Studies 5(4), pp. 553-580: apparent persistence in mutual fund performance is partly an artifact of survivorship. High-risk funds that underperform are more likely to close; the surviving sample retains only the high-risk winners. Probit analysis confirms poor performance increases fund disappearance probability. Estimated survivorship bias: approximately 80 to 150 bps per year in measured fund performance.

Carhart (1997), Journal of Finance 52(1): using a survivor-bias-free sample, common factor exposures and expenses nearly fully explain persistence. The "hot hands" result (Hendricks et al., 1993) largely disappears once survivorship bias is removed and the one-year momentum factor is controlled.

**Equity universe studies.** Shumway (1997), Journal of Finance 52(1): CRSP NYSE/AMEX data was missing delisting returns for a large fraction of stocks delisted for poor performance; the missing returns are large and negative. Shumway and Warther (1999), Journal of Finance 54(6), pp. 2361-2379: the problem is 4.7x larger on Nasdaq. Substituting -55% for missing performance-related Nasdaq delisting returns corrects the bias. After correction, there is no evidence that a size effect ever existed on Nasdaq.

**Interaction with other biases.** Survivorship bias compounds selection bias (vendor universes implicitly exclude very small firms that fail early) and look-ahead bias (a non-PIT index file contains only members that survived long enough to be in the index at download time).

---

## Vendor Landscape

### CRSP (Center for Research in Security Prices)

The academic standard for US equity price data, used in the majority of peer-reviewed asset pricing research.

**PERMNO.** Each security receives a permanent numeric identifier that does not change across name changes, ticker changes, or CUSIP reassignments. CRSP's own description: "Tickers change. PERMNO doesn't." PERMNO is the canonical key for linking price history, corporate actions, and fundamentals across decades.

**Delisting returns.** CRSP records `DLRET` (Delisting Return), computed from the delisting amount vs. the last trading price. For missing performance-related delisting returns CRSP codes -1 (i.e., -100%), but Shumway and Warther (1999) recommend using -55% for Nasdaq delistings based on empirical calibration.

**Index membership.** Table `crsp_a_indexes.dsp500list` provides daily S&P 500 membership with fields `PERMNO`, `mbrstartdt`, `mbrenddt`. Coverage from 1958. Updated annually: active members receive `mbrenddt` equal to the last trading day of the most recent full year, so a membership query for the current year requires a fresh pull.

**Access.** Institution-licensed via WRDS. Coverage: NYSE, AMEX, Nasdaq from 1925.

### Compustat

Primary commercial database for US company financial statements (S&P Global).

**Standard vs Snapshot.** The default FUNDA/FUNDQ files are NOT PIT-correct: they contain the most recently restated value for each firm-period, overwriting the original. **Compustat Snapshot** (from March 1987; extended history to 1968) appends rather than overwrites, assigning each update an activation date. It separates "as first reported" (original SEC filing values) from subsequent restatements.

**Key date fields in FUNDQ:**
- `datadate`: period end. NOT the availability date.
- `rdq`: earnings announcement date. This is the correct gate for PIT filtering; use `rdq` not `datadate + 90 days`.

**Limitations.** Snapshot coverage starts 1987. The "as first reported" supplemental file is separately licensed.

### Norgate Data

Australian commercial vendor explicitly positioned for systematic backtesting.

**Historical index constituents.** S&P 500 constituency tracked from March 1957 using the `$SPX` symbol. Membership changes recorded on the exact date of change; delistings trigger immediate replacement rather than waiting for scheduled reconstitution. The exit date stored is the last trading day the symbol was in the index (not the first day of removal), preventing off-by-one errors.

**Python API.** `norgatedata.index_constituent_timeseries(symbol, "$SPX", ...)` returns a binary daily series. To avoid survivorship bias both the active and delisted symbol sets must be scanned:

```python
active   = norgatedata.database_symbols('US Equities')
delisted = norgatedata.database_symbols('US Equities Delisted')
```

Scanning approximately 40,000 symbols takes 4 to 5 minutes on standard hardware.

**Delisted coverage.** Over 25,000 delisted securities since 1950; tickers appended with year-month suffix (e.g., `ALOG-201806`).

**Access.** Historical index constituent data requires a US Platinum or Diamond subscription.

### Sharadar (Nasdaq Data Link, formerly Quandl)

The most cost-accessible PIT-correct source for US equity fundamentals and index membership; the recommended v1 data source for independent practitioners.

**SF1 dimensions.** The `SHARADAR/SF1` table's `dimension` field controls PIT semantics:

| Dimension | Meaning | PIT-correct? |
|---|---|---|
| ARQ | As Reported, Quarterly (original SEC filing) | Yes |
| ART | As Reported, Trailing Twelve Months | Yes |
| MRQ | Most Recent Quarterly (restated) | No |
| MRY | Most Recent Annual (restated) | No |
| ARY | As Reported, Annual | Yes |

**Key date columns:**
- `datekey`: filing/announcement date. Gate all PIT queries on `datekey <= simulation_date`.
- `calendardate`: quarter-end date (analogous to Compustat `datadate`).
- `reportdate`: SEC submission date.
- `lastupdated`: Sharadar's internal update timestamp.

**S&P 500 membership.** `SHARADAR/SP500` (formerly SPCONSTITUENTS) is an event log of `(ticker, date, action)` records where action is "added" or "removed." Replaying this log reconstructs the constituent set on any date. Coverage from 1957.

**Limitations.** Fundamentals history from approximately 1990. US-listed equities only. Less granular than Compustat for detailed financial statement items. Delisted securities are included in the universe.

### Refinitiv (LSEG) and Bloomberg

**Refinitiv Point-in-Time Fundamentals** preserves original and restated values separately without overwriting. Coverage from 1989 (US) and 1997 (non-US). Schema-level documentation requires LSEG login; the product is enterprise-tier.

**I/B/E/S Point in Time** (launched December 2017) is the vendor standard for analyst estimates. Daily snapshots from January 2000; activation dates from January 1980. No data overwritten. Covered in Axis 5 above.

**Bloomberg BDH** provides `ANNOUNCE_DT` and `ENTRY_DT` fields for PIT fundamental queries, but schema details are proprietary and terminal-gated. Bloomberg is primarily used for desk-level and intraday work; CRSP/Compustat remain the academic standard for cross-sectional research.

---

## Common Backtester Errors

### The "Current S&P 500" Problem

Pulling today's S&P 500 and backtesting it 20 years introduces two compounded errors: (1) all current members survived to today (survivorship bias), and (2) some were not in the index during the historical period (preinclusion look-ahead bias).

Empirically demonstrated by the Analytical Platform (2025): a broad equal-weight S&P 500 strategy sees 1.45 pp CAGR overstatement (15.80% biased vs 14.35% unbiased). A small-cap tilt to the 20 smallest members shows 26.84 pp CAGR overstatement and 365 pp total-return overstatement over five years. For Russell 3000: of 3,000 original 1986 constituents, only 565 were still active as of the study date.

### Restated Fundamentals Creating False Alpha

A value strategy sorts on book-to-market using the standard Compustat delivery file. A company that reported inflated book value in 2010, later restated downward in 2012, appears in a 2024 backtest with the corrected (lower) 2010 figure: a number that was not available to any investor in 2010. This biases the sort and produces apparent alpha that could not have been realized.

Lyle (2024) documents 6.3 average re-standardizations per firm-quarter with a mean gap of 685 days, confirming this is systematic across the dataset, not an edge case.

### Identifier Non-Persistence

Ticker symbols are reused after delistings, sometimes within months. A backtest using tickers as primary keys silently merges the return history of two unrelated companies. CUSIPs also change on spin-offs and redomiciling transactions.

PERMNO-style persistent identifiers are required. A backtester with tickers as keys produces non-reproducible results across data vintages: the same ticker maps to different companies depending on when the symbol table was downloaded.

### Adjustment-vs-Corporate-Action Mismatch

A 10-for-1 split in 2015 divides all pre-2015 prices by 10. The book-to-price ratio computed on the adjusted price becomes 10x larger in the pre-split period, producing a phantom value signal. Most backtesting frameworks default to fully back-adjusted prices for all uses, which silently breaks ratio signals around large corporate actions.

Correct approach: use unadjusted prices for ratio computation; apply the split/dividend adjustment only to return calculation. Adjustment factors should be exposed as a separate column so the user can apply them selectively.

---

## Academic Standards for PIT Rigor

### McLean and Pontiff (2016)

R. David McLean and Jeffrey Pontiff, "Does Academic Research Destroy Stock Return Predictability?", Journal of Finance, Vol. 71, No. 1, February 2016, pp. 5-32.

Examines out-of-sample and post-publication returns for 97 published cross-sectional predictors. Portfolio returns are 26% lower out-of-sample and 58% lower post-publication than in-sample. The 32% additional post-publication decay (58% minus 26%) is consistent with investor learning. In-sample predictability is higher for factors with greater data-mining potential.

For PIT design: the 26% in-sample-to-out-of-sample decay provides an empirical ceiling on what a well-tested anomaly should yield. Backtests exceeding that ceiling on in-sample data are likely contaminated by restated fundamentals, survivorship bias, or both.

### Harvey and Liu (2020)

Campbell Harvey and Yan Liu, "False (and Missed) Discoveries in Financial Economics", Journal of Finance, Vol. 75, No. 5, October 2020.

Addresses the multiple-testing problem across the factor zoo. Their double-bootstrap method establishes a t-statistic threshold of approximately 3.0 (vs. the conventional 1.96) for a factor to pass a credible false-discovery-rate test, given the hundreds of prior tests on the same historical data. Non-PIT data amplifies the factor zoo problem: data-mining bias and look-ahead bias compound, inflating t-statistics for factors that would not survive a clean out-of-sample test.

---

## Hooks into pit-backtest Design

The research findings translate directly into concrete data-layer requirements.

**Typed Universe API.** Expose `is_member(asset_id, date) -> bool` backed by a historical membership table with O(1) indexed lookup. Fail at backtest-construction time if the membership source does not cover the requested date range.

**Dual-timestamp data model.** Every fundamental record must carry both `period_end_dt` (quarter end, analogous to `datadate`) and `available_dt` (announcement date, analogous to `rdq` / Sharadar `datekey`). The simulation engine must filter on `available_dt <= simulation_dt`. This is the `perspective_dt` / `dt` pattern from Zipline's Pipeline API. See [[research-existing-backtesters]] Zipline section.

**Delisting requirement.** Every asset with a membership record must have a known terminal state: either a valid delisting record (with a final return) or confirmed active status at backtest end. An open membership spell with no price data past a given date must raise a validation error, not silently drop a position.

**Price adjustment consistency.** The data layer must expose adjusted prices (for return calculation) and unadjusted prices (for ratio computation) as separate labeled columns, plus adjustment factors as a third column.

**Recommended v1 data source.** Sharadar SF1 (ARQ dimension) + `SHARADAR/SP500` is the cheapest credible PIT-correct source: PIT fundamentals from ~1990, PIT S&P 500 membership from 1957, delisted securities included. For price data, Norgate provides PIT-correct prices with a 25,000+ delisted universe. For academic-grade research, CRSP + Compustat Snapshot via WRDS is the gold standard.

**Vendor adapter interface.** Define a `PitDataSource` protocol with methods: `get_price(asset_id, dt, field)`, `get_fundamental(asset_id, available_dt, field)`, `get_members(universe_id, dt)`, `get_delisting(asset_id)`. Ship Sharadar and Norgate adapters in v1; CRSP and Compustat adapters in v2.

---

## Open Questions

1. **Should v1 support fundamentals or defer?** Fundamentals require dual timestamps, restatement tracking, and lag validation (the most complex PIT machinery). A price-only v1 with PIT index membership is still a meaningful differentiation; fundamentals can be v2.

2. **Identifier mappings.** The data layer needs a symbol-resolution service: `(identifier, type, start_date, end_date, asset_id)` populated from vendor cross-reference files. Must handle ticker reuse after delisting.

3. **Norgate vs CRSP adjustment methodology.** Both apply standard split and dividend adjustments but may differ on special dividends, spin-offs, and rights offerings. The chosen default must be documented; mixing adjustment sources across vendors is not safe.

4. **Validating user-supplied PIT membership.** If users supply their own membership CSV, the engine should verify every `(asset, start, end)` spell has continuous price coverage. Gaps should surface as warnings with the option to treat them as delistings.

---

## Sources

1. Brown, S.J., Goetzmann, W., Ibbotson, R.G., and Ross, S.A. (1992). "Survivorship Bias in Performance Studies." Review of Financial Studies, 5(4), 553-580. https://academic.oup.com/rfs/article-abstract/5/4/553/1590264. Foundational paper establishing survivorship bias in mutual fund performance studies.

2. Shumway, T. (1997). "The Delisting Bias in CRSP Data." Journal of Finance, 52(1), 327-340. https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf. Documents missing delisting returns in CRSP NYSE/AMEX data and their impact on measured anomalies.

3. Shumway, T. and Warther, V.A. (1999). "The Delisting Bias in CRSP's Nasdaq Data and Its Implications for the Size Effect." Journal of Finance, 54(6), 2361-2379. https://onlinelibrary.wiley.com/doi/abs/10.1111/0022-1082.00192. Extends delisting bias documentation to Nasdaq; finds the bias is 4.7x larger; recommends -55% correction for missing performance-related returns.

4. Carhart, M.M. (1997). "On Persistence in Mutual Fund Performance." Journal of Finance, 52(1), 57-82. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1997.tb03808.x. Survivor-bias-free study showing performance persistence is largely explained by factor exposures and costs.

5. McLean, R.D. and Pontiff, J. (2016). "Does Academic Research Destroy Stock Return Predictability?" Journal of Finance, 71(1), 5-32. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623. 97 anomalies; post-publication returns are 58% lower than in-sample, consistent with data-mining and investor learning.

6. Harvey, C.R. and Liu, Y. (2020). "False (and Missed) Discoveries in Financial Economics." Journal of Finance, 75(5). https://people.duke.edu/~charvey/Research/Published_Papers/P143_False_and_missed.pdf. Multiple-testing framework showing most published factors require a t-statistic above 3.0 to be credible.

7. Thomson Reuters. (2017). "Thomson Reuters Drives Greater Backtesting and Historical Analysis with Launch of I/B/E/S Point in Time." https://www.thomsonreuters.com/en/press-releases/2017/december/thomson-reuters-drives-greater-backtesting-and-historical-analysis-with-launch-of-ibes-point-in-time. I/B/E/S PIT product announcement; describes daily snapshot structure, Point Date field, and no-overwrite policy.

8. CRSP. "PERMNO: Permanent Security Identifier." https://www.crsp.org/research/permno/ (accessed 2026-05-28). CRSP's description of the PERMNO persistent identifier system.

9. SMU Libraries. "Notes and Thoughts on Retrieving Historical Members of S&P 500 from WRDS." https://library.smu.edu.sg/topics-insights/notes-and-thoughts-retrieving-historical-members-sp-500-wrds (accessed 2026-05-28). Technical walkthrough of CRSP dsp500list schema and the annual-update caveat for current members.

10. Concretum Group. "Historical Constituents of an Equity Index in Python (Norgate Data)." https://concretumgroup.com/historical-constituents-of-an-equity-index-in-python-norgate-data/ (accessed 2026-05-28). Full Python API walkthrough for Norgate index_constituent_timeseries; documents exit-date semantics and delisted-symbols requirement.

11. Norgate Data. "Data Content Tables." https://norgatedata.com/data-content-tables.php (accessed 2026-05-28). Norgate vendor documentation: S&P 500 coverage from March 1957, delisted universe of 25,000+ securities, pricing methodology.

12. Nasdaq Data Link / Sharadar. "SF1 Fundamentals Documentation." https://data.nasdaq.com/databases/SF1 (accessed 2026-05-28, login-gated). Summary of SF1 dimensions (ARQ, ART, MRQ, MRY, ARY) and SP500 membership table coverage from 1957.

13. LSEG / Refinitiv. "Point-in-Time Fundamentals." https://www.lseg.com/en/data-analytics/financial-data/company-data/fundamentals-data/point-in-time-fundamentals (accessed 2026-05-28). Coverage from 1989 (US) and 1997 (non-US); no-overwrite policy; original vs restated distinction.

14. Analytical Platform. "The Hidden Impact of Survivorship Bias on Backtesting Results." https://www.analyticalplatform.com/the-hidden-impact-of-survivorship-bias-on-backtesting-results-of-investment-strategies/ (accessed 2026-05-28). Empirical demonstration with S&P 500 data: 1.45 pp CAGR bias for broad strategy; 26.84 pp for small-cap tilt.
