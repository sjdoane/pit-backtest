# 0002: Methodology canon for pit-backtest

Status: draft, research phase 2.

## 1. Purpose and method

Phase 1 established what the existing open-source backtester landscape gets wrong; phase 2 establishes what is right, drawing on the canonical academic and practitioner literature on backtest validity, performance metrics, execution modeling, point-in-time data, and the empirical failure modes that show up only after capital is deployed.

The audience is a quant researcher or recruiter who wants to confirm that the math in this repo's analytics layer was not derived ad hoc, that the cost model has a defensible empirical grounding, that the validation procedures are the ones the field treats as canonical, and that the design knows where its remaining uncertainty lives.

Five topics are covered, one per file under [`sources/`](sources/):

- [`methodology-afml-backtesting.md`](sources/methodology-afml-backtesting.md): Lopez de Prado, *Advances in Financial Machine Learning* (Wiley 2018), chapters 11 through 15.
- [`methodology-backtest-overfitting.md`](sources/methodology-backtest-overfitting.md): the Bailey and Lopez de Prado papers on the Probabilistic Sharpe Ratio, the Deflated Sharpe Ratio, the Probability of Backtest Overfitting, and the Harvey and Liu critique.
- [`methodology-almgren-chriss.md`](sources/methodology-almgren-chriss.md): the Almgren and Chriss optimal-execution model, the Almgren et al. 2005 empirical calibration, the Bouchaud-Lillo-Farmer square-root law, the Obizhaeva-Wang transient-impact model, the Gatheral no-arbitrage conditions, and the Kyle 1985 conceptual ancestor.
- [`methodology-point-in-time.md`](sources/methodology-point-in-time.md): the five PIT axes (price adjustments, fundamentals lag, index membership, corporate-action dates, analyst revisions), the survivorship-bias literature, and the vendor landscape (CRSP, Compustat, Norgate, Sharadar, Refinitiv).
- [`methodology-practitioner-postmortems.md`](sources/methodology-practitioner-postmortems.md): seven substantive industry-credible postmortems by Carver, Chan, Lopez de Prado, and Aiello et al., documenting specific failure mechanisms with quantitative evidence.

This synthesis pulls patterns across the five and translates them into design requirements for pit-backtest's architecture, statistics layer, cost model, data layer, and validation infrastructure.

## 2. The methodology canon at a glance

| Area | Primary reference | Headline result | Required by pit-backtest |
|---|---|---|---|
| Backtest validity | LdP 2018 ch. 11; Bailey, Borwein, LdP, Zhu 2014 | A backtest is a hypothesis test, not a search procedure. The False Strategy Theorem proves any Sharpe is achievable from noise given enough trials. | Trial registry; hypothesis-first workflow; result confidence tier. |
| Cross-validation | LdP 2018 ch. 12 | Standard k-fold is structural lookahead via label-horizon overlap. Purged k-fold plus embargo is the fix. CPCV produces a distribution of OOS Sharpe across multiple paths from the same data. | Label horizon metadata on every observation; CV splitter with purge + embargo; CPCV path generator. |
| Performance metrics | Bailey and LdP 2012, 2014; Lo 2002 | Raw Sharpe overstates significance under non-normality and selection bias. PSR corrects for skewness and kurtosis. DSR corrects PSR by the expected maximum Sharpe across N trials. MinTRL gives the required track-record length. | Analytics layer computes PSR, DSR, MinTRL by default; raw SR shown alone is a configuration error. |
| Multiple testing | Bailey, Borwein, LdP, Zhu 2017; Harvey and Liu 2015 | Effective N from a parameter sweep is much smaller than raw N due to correlation between trials. PBO via combinatorially symmetric cross-validation (CSCV) estimates the fraction of sweeps that produce overfit selections. | Parameter sweeps emit N_raw, N_effective (PCA or ONC clustered), SR_0, and PBO. |
| Execution modeling | Almgren and Chriss 2000; Almgren et al. 2005; Bouchaud et al. 2009; Obizhaeva-Wang 2013; Gatheral 2010 | Linear impact is empirically wrong. Temporary impact scales as approximately the 0.5 to 0.6 power of order size. Permanent impact must update the price series, not only the fill price. | Cost model hierarchy: NoImpact, FixedBps, LinearImpact, SquareRootImpact (default). Pre-trade cost estimate exposed to the portfolio optimizer. Permanent impact register that feeds the price series. |
| Point-in-time data | Compustat Snapshot; CRSP; Norgate; Sharadar; McLean and Pontiff 2016 | PIT is five problems (prices, fundamentals, membership, corporate actions, analyst data). Survivorship bias overstates returns by 1.4 pp CAGR for broad universes and >25 pp CAGR for small-cap tilts. | Dual-timestamp data model (`period_end_dt`, `available_dt`); typed Universe API with `is_member(asset, date)`; persistent asset identifiers; vendor adapter protocol. |
| Practitioner failure modes | Aiello et al. 2026; Carver 2015; Chan 2012-2017; LdP 2018 | The same patterns recur: multiple testing, fill at structurally unavailable prices, regime change invisible in backtest, library-level bugs (the backtrader /100.0 commission silent rescale). | Result `confidence_tier` field; explicit fill-price model; commission unit validation tests; backtest-live shared execution kernel. |

The unifying claim across the literature: backtesting is the most error-prone activity in quantitative investing because every method that produces convincing numbers fails when its premises are quietly violated. The mitigation strategy is uniform across sources: make the premises structural, type-enforced, and impossible to violate without an explicit declaration.

## 3. Backtesting is hypothesis testing, not search

LdP's most-cited line, repeated by Carver, Chan, and others, reframes the entire activity: a backtest is not an experiment in which you discover a strategy, it is a sanity check on a hypothesis you already had. Iterating strategy design against the same historical dataset is statistically equivalent to training on the test set; the resulting "out-of-sample" performance is in-sample once it has informed any selection.

This reframing has three concrete consequences.

**The trial registry.** Every backtest must be recorded against a (dataset fingerprint, strategy family) key, with the SR estimate, T, gamma_3, gamma_4, and timestamp. When a researcher requests a strategy report, the analytics layer reads the registry, computes N_effective and the cross-sectional Sharpe variance V[{SR_n}], and reports DSR alongside raw SR. Without this registry, DSR collapses to PSR with N = 1, which understates overfitting risk by orders of magnitude.

**The result confidence tier.** Each backtest result carries metadata describing what was selected and how. Tiers progress roughly as: single-run on pre-specified spec; walk-forward validated; sweep-with-DSR-correction; sweep-selected-without-correction. Deployment-bound code paths refuse to consume sweep-selected results without an explicit acknowledgment step, which is the structural equivalent of LdP's "do not research under the influence of a backtest." This is the design pattern that the vectorbt failure mode (presenting sweep results as production results) makes most obvious in retrospect (see [`research/0001-existing-backtesters.md`](0001-existing-backtesters.md) section 9).

**The hypothesis-first workflow.** The engine should distinguish a "research mode" (iteration allowed, but results are stamped as exploratory) from a "validation mode" (strategy spec is frozen, single backtest runs, statistics computed). Validation-mode results are the only ones that should feed any subsequent decision. The transition between modes should be explicit and recorded so an auditor can later verify the order of operations.

Carver's three-mode taxonomy of overfitting (explicit, implicit, tacit) sharpens this. Explicit fitting (automated parameter search) is the form everyone agrees is dangerous; implicit and tacit fitting are the modes the literature most often misses. Implicit fitting happens whenever a researcher modifies the strategy after seeing any out-of-sample result. Tacit fitting happens whenever a researcher uses post-hoc knowledge to constrain the search space (the "we know momentum works, so restrict the parameter space to momentum configurations" pattern). The engine cannot fully prevent these, but it can make them visible: the trial registry shows the sequence of edits, and the confidence tier downgrades on any modification after OOS exposure.

## 4. The multiple-testing problem and its formal solutions

The Bailey, Borwein, Lopez de Prado, Zhu (2014) paper in the *Notices of the AMS* is the most-cited formal statement of why parameter sweeps without correction produce false discoveries. The key result is that with N independent trials of a strategy with zero true Sharpe, the expected maximum sample Sharpe ratio grows as approximately sqrt(2 log N): testing 100 configurations and selecting the best one is expected to produce a Sharpe of around 3 even when nothing is there to find.

Two families of correction exist in the literature.

**The Lopez de Prado family** computes the expected maximum sample Sharpe from order statistics of N independent Gaussian draws and deflates the reported Sharpe accordingly. The False Strategy Theorem benchmark is:

```
SR_0 = sqrt(V[{SR_n}]) * ((1 - gamma) * Phi_inv(1 - 1/N)
                           + gamma * Phi_inv(1 - 1/(N * e)))
```

where gamma is the Euler-Mascheroni constant approximately 0.5772, e is Euler's number, Phi_inv is the inverse standard normal CDF, and V[{SR_n}] is the cross-sectional variance of Sharpe estimates across the N trials. The Deflated Sharpe Ratio (DSR) is the PSR evaluated at SR_0 rather than at zero or some other arbitrary benchmark. The PSR formula itself, from Bailey and Lopez de Prado 2012, accounts for the skewness and excess kurtosis of returns:

```
PSR(SR_star) = Phi(  (SR_hat - SR_star) * sqrt(T - 1)
                     / sqrt(1 - gamma_3 * SR_hat
                            + (gamma_4 - 1) / 4 * SR_hat^2)  )
```

with Phi the standard normal CDF, T the number of return observations, and gamma_3, gamma_4 the third and fourth standardized moments of returns. Negative skewness and excess kurtosis both inflate the denominator, which is why crash-prone strategies receive a larger PSR penalty.

The Probability of Backtest Overfitting (PBO) is computed via Combinatorially Symmetric Cross-Validation (CSCV): partition the T-period backtest into S equal contiguous blocks (S even, typically 8 to 16), enumerate all C(S, S/2) combinations of in-sample versus out-of-sample blocks, identify the best in-sample strategy in each combination, and record its out-of-sample rank. PBO is the fraction of combinations in which the in-sample winner ranked below the OOS median. PBO above 0.5 means the selection process is worse than random.

**The Harvey and Liu family** applies frequentist FWER or FDR corrections (Bonferroni, Holm, Benjamini-Hochberg-Yekutieli) to test statistics and back-converts to haircut Sharpes. Their 2016 *Review of Financial Studies* paper documents 316 published factors by 2012, implying a hurdle of approximately t = 3.0 for credible alpha versus the conventional t = 1.96. The non-uniform haircut is more severe for marginal strategies and less severe for high-Sharpe outliers, which is a different distributional shape from the LdP family's uniform deflation.

For pit-backtest, both families inform the implementation but DSR is the default because it integrates cleanly with PSR and MinTRL, requires only quantities already needed for the analytics layer, and is the most commonly cited in academic ML-finance work. The Harvey-Liu Bonferroni-style haircut is supported as an alternative metric for users who prefer the frequentist framing. The disagreement between the two on the right hurdle at moderate N is a real disagreement in the field and is surfaced in the report rather than hidden.

**Effective number of trials.** N in the DSR formula is the effective number of independent trials, not the raw count of backtest runs. Highly correlated configurations (a parameter sweep over a 20-by-20 grid of correlated parameters) have effective N much smaller than 400. LdP proposes the ONC (Optimal Number of Clusters) algorithm applied to the cross-trial correlation of OOS returns. PCA on the SR matrix across trials, taking the number of components explaining 95% of variance, is a tractable approximation. The architecture ADR will need to choose between these; the default in v1 will likely be PCA-based for simplicity, with ONC as a configurable upgrade.

## 5. Cross-validation as structural lookahead

LdP chapter 12 is the cleanest statement of why naive k-fold cross-validation, applied to financial time series, is a category error. The mechanism: financial labels (e.g., triple-barrier outcomes) depend on future price paths. If training observation at time t has a label horizon ending at t + h, and the test fold contains observations between t and t + h, the training label for t was computed using prices that overlap with the test window. The model trained on that observation has implicit knowledge of what happens during the test period.

The fix is purged k-fold cross-validation. For each test fold:

1. Identify the test interval [t_start, t_end].
2. **Purge**: remove from training every observation i whose label horizon reaches into the test interval, i.e., i + h_i greater than or equal to t_start.
3. **Embargo**: remove from training every observation i within an embargo window immediately after the test interval, i.e., t_end less than i less than or equal to t_end + embargo_count. The embargo handles serial correlation leakage that purging alone does not eliminate.

Both are required. Purging addresses label-horizon overlap inside the test window; embargo addresses autocorrelation leakage from the test window into post-test training data. The default embargo is 5% of T, which LdP gives illustratively rather than derivationally; the principled choice depends on the autocorrelation decay time of the return series and should ultimately be data-driven.

Combinatorial Purged Cross-Validation (CPCV) extends purged k-fold to a combinatorial setting. Partition T into N groups, then test all C(N, k) combinations of k groups held out. Each observation appears in exactly C(N-1, k-1) of the C(N, k) test sets. The total number of distinct, non-overlapping backtest paths is phi(N, k) = (k/N) * C(N, k). For N = 6, k = 2 that gives 5 paths from a single dataset.

The implication for pit-backtest's architecture is that the backtesting layer must not return a single Sharpe scalar from a CPCV run. It must return a distribution of Sharpes (one per path), and the reporting layer must render that distribution (min, 10th percentile, median, 90th percentile, max), flagging high-variance distributions as regime-sensitive. Any UI or API that exposes "the Sharpe ratio" from a CPCV run as a single number should be treated as a correctness bug. This is a non-negotiable design constraint.

CSCV (for PBO) and purged CV (for walk-forward validation) address different leakage problems and should run simultaneously rather than alternatively. The CSCV block boundaries should align with the CPCV walk-forward windows so that the two estimates are comparable.

## 6. The Sharpe canon and the required analytics

Every backtest report produced by pit-backtest must include, at minimum:

1. **Raw annualized Sharpe ratio**, with the return frequency documented.
2. **Probabilistic Sharpe Ratio (PSR)** against SR_star = 0, using the Bailey-LdP 2012 formula with observed gamma_3 and gamma_4. The observed moments are reported alongside.
3. **Deflated Sharpe Ratio (DSR)** using the False Strategy Theorem SR_0 benchmark and the effective N from the trial registry. If N is unknown (a one-off run not registered against any sweep), DSR is undefined and explicitly marked as such; this is not a silent default to N = 1.
4. **Minimum Track Record Length (MinTRL)** at the 95% confidence level, given the observed SR, gamma_3, gamma_4. If MinTRL exceeds the observed T, the report flags the SR as "insufficient track record" in amber.
5. **Drawdown statistics**: maximum drawdown, average drawdown, drawdown duration, Calmar ratio. These both feed the MinTRL denominator and separately characterize tail risk.
6. **HHI concentration of returns** (Herfindahl-Hirschman Index applied to bar-level PnL). Values close to 1 mean a single bar drives the entire result.
7. **Year-by-year and subsample decomposition** of returns. Aggregate Sharpe hides Chan's pattern of single-year concentration; the decomposition forces it to be visible.

The presence of a raw SR without DSR in any report should be a configuration error caught at construction time, not a runtime warning. The architecture ADR will specify the exact API but the rule is: it is not possible to call `report.render()` on a result that has a raw SR but no DSR computation, unless the user explicitly passes a flag asserting that N = 1 and no selection occurred. This is the structural enforcement equivalent of LdP's "every backtest must be reported with all trials involved in its production."

Lopez de Prado's full chapter 14 scorecard (general characteristics, performance, runs and drawdowns, implementation shortfall, risk-adjusted efficiency, attribution) is the target output format. Strategy reports that omit any of the six categories should not exist as a supported output mode.

## 7. The execution canon and the cost model hierarchy

The Almgren-Chriss 2000 model is the canonical framing for execution: a trader holding X shares chooses a liquidation trajectory v(t) to minimize a mean-variance cost functional `E[C] + lambda * Var[C]`, where E[C] sums permanent and temporary impact and Var[C] is exposure to price drift while inventory remains. The closed-form solution under linear impact is a hyperbolic-sine inventory trajectory governed by a single urgency parameter kappa = sqrt(lambda * sigma^2 / eta).

The empirical extension by Almgren, Thum, Hauptmann, Li (2005) calibrated the model on 700,000 institutional orders from Citigroup and found that linear impact is wrong: temporary impact scales as approximately the 3/5 (0.6) power of the participation rate, not linearly. The Bouchaud, Farmer, Lillo (2009) review documents the square-root law (exponent close to 1/2) across asset classes, time periods, and exchanges. The two findings agree directionally (concave impact) and differ on the precise exponent depending on whether the normalization is per-rate or per-total-volume. For pit-backtest's purposes, both are approximations of the same underlying empirical concavity, and the model exposes both as configurable exponents.

The Obizhaeva-Wang (2013) transient-impact model refines this further: impact decays over time with a finite resilience half-life, neither purely permanent nor purely instantaneous. The propagator framework (Gatheral 2010) generalizes the impact-with-decay setup and proves that not all combinations of impact function and decay kernel are arbitrage-free; an exponential decay kernel is consistent only with linear impact, and power-law decay exponents must be at most 1/2 to preclude round-trip arbitrage.

The cost-model hierarchy for pit-backtest, in order of increasing sophistication:

1. **NoImpact**: fill at arrival mid-price. Useful only for debugging strategy logic; overstates returns by 100 to 1000 bps depending on turnover. Marked as an unsuitable-for-deployment tier.
2. **FixedBps**: fill at arrival mid plus a fixed-bps offset on the trade side. Configurable. Appropriate for liquid large-cap equity strategies where order size is small relative to ADV.
3. **LinearImpact**: Almgren-Chriss 2000 linear model with parameters eta (temporary), gamma (permanent), epsilon (half-spread). Appropriate when the strategy targets a known execution horizon and the user accepts the linearity assumption.
4. **SquareRootImpact** (recommended default): the Almgren 2005 calibration with eta = 0.142, beta = 0.6, gamma = 0.314. Temporary impact in basis points: `eta * sigma_D * |Q / (V_D * T)|^beta`. Permanent impact: `0.5 * gamma * sigma_D * (Q / V_D) * (Theta / V_D)^(1/4)`. Inputs are observable (sigma_D from realized volatility, V_D from daily volume, Theta from shares outstanding).
5. **TransientImpact (Obizhaeva-Wang)** as an optional plugin: impact decays exponentially with a per-instrument resilience half-life. Required for strategies that execute large orders in sequential child orders within a single day.

The critical implementation constraint: **permanent impact must feed back into the price series, not only the fill price**. If a strategy sells a large block, the permanent impact permanently lowers the instrument's mid-price; that lowered mid must be visible in subsequent fills, unrealized P&L, and any signal computation. A backtester that only adjusts fill price but not the carried mid-price will report artificially favorable subsequent fills, especially for multi-trade strategies in the same direction. The implementation is a `permanent_impact_register` per instrument that adds an additive adjustment to the raw price feed before downstream consumers see each bar.

The second critical constraint: **the cost model must expose `pre_trade_cost_estimate(instrument, shares, direction)` to the portfolio optimizer**. The QSTrader gap noted in phase 1 is that target weights are computed without knowing execution costs; high-turnover rebalances proceed even when the net benefit of trading is negative after impact. Closing the gap requires the optimizer to query expected impact for any candidate trade list and prefer smaller rebalances when impact is high.

Borrow costs and dividend cash flows on shorts are a separate cost category from impact, with different inputs (securities lending rates, dividend calendars) and different timing semantics (daily accrual rather than per-trade). The cost layer should have two independent pluggable modules: `ImpactCostModel` and `CarryCostModel`. Combining them at the total-cost level is straightforward; conflating them in implementation creates the kind of subtle bug that Aiello et al. (2026) documented.

## 8. The data canon and the point-in-time API

Point-in-time data is not one problem. It is five.

1. **Price-level adjustments**. Backward-adjusted prices break ratio signals around large corporate actions. The fix is the zipline `dt` / `perspective_dt` pattern: every historical price lookup carries both the date the price occurred and the date it is being observed from, with adjustments applied only in the half-open window. Adjusted prices for return computation and unadjusted prices for ratio computation must be exposed as separate columns.

2. **Fundamentals reporting lag**. Compustat records `datadate` (period end) and `rdq` (earnings announcement date). A naive backtest that uses `datadate + 1 day` accesses data that was not public for another 45 to 60 days. The dual-timestamp model requires `period_end_dt` and `available_dt` on every fundamental record, with the engine gating on `available_dt <= simulation_dt`. Sharadar's SF1 ARQ dimension and Compustat Snapshot are the PIT-correct products; the standard FUNDA/FUNDQ files and Sharadar MRQ/MRY are not.

3. **Universe membership over time**. Using today's S&P 500 to backtest 20 years introduces survivorship bias (1.45 percentage points CAGR for broad equal-weight strategies; 26.84 percentage points for small-cap tilts; Analytical Platform 2025 empirical demonstration). The fix is a typed Universe API: `is_member(asset_id, date) -> bool` backed by a `(asset_id, start_date, end_date)` membership table. CRSP's `dsp500list` covers from 1958; Sharadar's `SP500` event log covers from 1957; Norgate provides a 25,000+ delisted universe.

4. **Corporate-action date semantics**. Adjustment factors must be applied on the ex-date, not the declaration date or record date. CRSP records ex-dates and is the academically correct convention. Vendors that record declaration dates introduce timing errors in any daily or higher-frequency backtest.

5. **Analyst estimate revisions**. The standard I/B/E/S history file stores only the most recently revised estimate per analyst-period pair. The I/B/E/S Point in Time product (launched December 2017) addresses this with daily snapshots from January 2000 and activation dates from January 1980. For pit-backtest v1, analyst data is out of scope; the engine should reject any analyst feed that does not expose explicit availability timestamps.

The data-layer requirements that follow:

- **Dual-timestamp model**: every record has both `period_end_dt` and `available_dt`. The engine filters on the latter.
- **Persistent asset identifiers**: ticker reuse and CUSIP changes are common; the data layer must maintain a `(identifier, type, start_date, end_date, canonical_asset_id)` resolution table populated from vendor cross-reference files. CRSP's PERMNO is the conceptual reference.
- **Typed Universe API**: `is_member(asset_id, date) -> bool` with O(1) indexed lookup. Backtest construction validates that the membership source spans the requested date range.
- **Delisting requirement**: every asset with a membership record must have either a valid delisting record (with a final return) or confirmed active status. An open membership spell with no price data past a date raises a validation error rather than silently dropping a position.
- **Vendor adapter protocol**: a `PitDataSource` protocol with methods `get_price`, `get_fundamental`, `get_members`, `get_delisting`. Ship Sharadar and Norgate adapters in v1; CRSP and Compustat adapters in v2 if there is institutional access.

The recommended primary data source for v1 is Sharadar SF1 ARQ plus `SHARADAR/SP500` for fundamentals and membership, combined with Norgate for PIT-correct prices and delisted history. This is the cheapest credible PIT-correct stack for independent practitioners. CRSP and Compustat via WRDS are the institutional gold standard but are gated.

This data-layer differentiation is the clearest design opportunity surfaced by phase 1: no surveyed library handles PIT correctly. nautilus_trader explicitly defers it (issue #3307); zipline provides the adjustment math but no membership data; the rest have no concept of either. A focused U.S. equity backtester that solves this correctly is a meaningful contribution.

## 9. Practitioner patterns that academic literature underweights

The practitioner postmortems add three patterns to the canon that the academic literature does not consistently cover.

**Library-level commission bugs.** Aiello, Hladkyy, Kakoulli, Gural (2026) ran the same 15 strategies across six open-source backtesters and found systematic divergence under nonzero transaction costs, correlated at 0.93 with cost intensity. The most-cited example is the backtrader `/100.0` silent rescale of percentage commissions (when `percabs=False`), which produces commission charges 100 times smaller than the user intended. The class of bug is not detectable by inspecting strategy logic; it requires auditing the library's commission implementation against a known reference. The mitigation in pit-backtest is to require typed units on every cost parameter (e.g., `commission_bps` rather than `commission`) and unit tests that verify a known trade produces a known commission to within floating-point precision.

**Fill at structurally unavailable prices.** Chan (2015, 2016) documented that consolidated daily closing prices are not tradable at close. The US equity market operates across 60+ market centers; the consolidated close is the last trade received by the SIP from any venue, which can differ from the primary exchange auction price (where MOC orders actually execute). A pair trading strategy showed Sharpe ratio approximately 1.0 in backtest using consolidated close and lost money on every trade in live execution using BBO data at the close. The mitigation is to require an explicit fill-price model declaration (consolidated close, primary auction close, midpoint at close, next-open) and to validate the configured data source against the declaration at backtest construction.

**Regime change as the silent killer.** Chan (2012) documented a buy-on-gap mean-reversion strategy with 19% APR and 1.4 Sharpe through October 2008 that produced -6% APR after, never recovering. The attribution is structural: decimalization and HFT changed the microstructure that retail panic-selling created the original signal in. The historical backtest showed no warning because the regime change happened after the backtest period. There is no API-level prevention for this; the mitigation is monitoring (live drawdown duration relative to historical maximum) and explicit per-regime attribution in the backtest report so single-year concentration is visible.

## 10. Aggregated design implications

The design requirements that follow from the methodology canon, organized by component.

**Analytics layer**
- Compute PSR, DSR, MinTRL, drawdown stats, HHI, year-by-year decomposition by default for every backtest.
- Refuse to render a report with raw SR but no DSR unless explicit N = 1 assertion.
- DSR uses ONC or PCA-derived effective N; default to PCA for simplicity.
- Support Harvey-Liu Bonferroni haircut as an alternative metric, with disagreement visible.
- Trial registry persists every backtest run keyed by (dataset fingerprint, strategy family).

**Backtesting layer**
- Distinguish research mode (iteration logged, results stamped exploratory) from validation mode (frozen spec, single run).
- Support purged k-fold and CPCV via a CV splitter that consumes label-horizon metadata.
- CPCV emits a distribution of Sharpes (one per path); single-Sharpe API is a correctness bug.
- CSCV runs alongside CPCV for PBO estimation; PBO > 0.1 warns; PBO > 0.3 blocks deployment.

**Data layer**
- Dual-timestamp data model (`period_end_dt`, `available_dt`) on every record.
- Typed Universe API with `is_member(asset_id, date)`.
- Persistent asset identifiers; vendor cross-reference resolution table at ingest.
- Delisting requirement validated at backtest construction.
- Adjusted and unadjusted prices as separate exposed columns.
- `PitDataSource` protocol with Sharadar and Norgate adapters in v1.

**Cost / execution layer**
- Required slippage and commission models, no zero default.
- Cost hierarchy: NoImpact (debugging only), FixedBps, LinearImpact, SquareRootImpact (default), TransientImpact (optional plugin).
- Permanent impact register feeds the price series, not only the fill price.
- `pre_trade_cost_estimate` exposed to the portfolio optimizer.
- Borrow cost as a separate first-class `CarryCostModel`.
- Commission parameters have typed units; unit tests verify known-trade correctness.
- Explicit fill-price model required (consolidated close, primary auction, midpoint, next-open).

**Architecture-level**
- Result `confidence_tier` field: `single-run-pre-specified`, `walk-forward-validated`, `sweep-with-DSR-correction`, `sweep-selected-no-correction`. Deployment-bound consumers refuse the last tier without explicit override.
- Backtest and live execution share the same fill model, commission model, data model. The kernel-sharing pattern (nautilus_trader's NautilusKernel) is the right structural defense against backtest-live divergence even when live trading is not a v1 goal.
- TestClock vs LiveClock injected at construction.

These requirements are concrete enough that they will appear in the architecture ADR (`docs/decisions/0003-architecture.md`) as constraints on the class and protocol hierarchy.

## 11. Open questions for the architecture ADR

Items unresolved by the methodology research, to be answered before engine implementation.

- **Effective N estimation algorithm.** ONC versus PCA versus raw N as a conservative lower bound. Default for v1.
- **Embargo length default.** LdP gives 5%; the principled choice is data-driven, related to autocorrelation decay time. Mechanism for user override.
- **CPCV path construction.** The exact mapping of C(N, k) test-set combinations onto phi(N, k) non-overlapping paths. Implementation needs to verify against the AFML chapter 12 algorithm.
- **Harvey-Liu versus LdP severity.** At moderate N the two frameworks differ by approximately 20% on haircut magnitude; at large N they diverge logarithmically versus linearly. The default in v1 must be chosen, and the disagreement surfaced in the report.
- **Permanent impact visibility.** Whether the permanent-impact-adjusted price is visible to the signal model (more realistic, can create circular dependencies in mean-reversion strategies) or only to portfolio valuation. Probably the latter for v1.
- **Transient impact half-life default.** Equity literature suggests 5 to 30 minutes; a sensible default is 10 minutes. Mechanism for per-instrument calibration.
- **Sharadar versus CRSP versus Norgate as v1 reference data.** Cost, coverage, and license tradeoffs.
- **Capacity model.** Required at v1 or deferred. Tied to the cost model hierarchy.
- **Sweep mode versus event-driven mode separation.** How the API enforces that sweep results cannot feed deployment without an acknowledgment step.

These will be addressed in the spec critique (ADR 0001), the roadmap review (ADR 0002), and the architecture ADR (0003).

## 12. Suggested reading order

For a reviewer looking to evaluate this work, the suggested order through the per-source files:

1. [`sources/methodology-practitioner-postmortems.md`](sources/methodology-practitioner-postmortems.md) for the patterns that motivate the design (most concrete failures).
2. [`sources/methodology-afml-backtesting.md`](sources/methodology-afml-backtesting.md) for the validation framework (purged CV, CPCV, the scorecard).
3. [`sources/methodology-backtest-overfitting.md`](sources/methodology-backtest-overfitting.md) for the multiple-testing math (PSR, DSR, PBO, MinTRL).
4. [`sources/methodology-almgren-chriss.md`](sources/methodology-almgren-chriss.md) for the execution model.
5. [`sources/methodology-point-in-time.md`](sources/methodology-point-in-time.md) for the data architecture.

For a reviewer looking only at this synthesis, sections 4 (multiple testing), 7 (cost model), and 8 (PIT data) are the most consequential for what pit-backtest will actually build.

## 13. Status and next steps

This document closes research phase 2. The next milestones per [`docs/ROADMAP.md`](../ROADMAP.md):

- **ADR 0001: spec critique and skeptical review**. The original spec is opinionated about features but not yet stress-tested against the constraints surfaced here. A skeptical-reviewer agent persona will critique it, captured as the ADR.
- **Phased roadmap M1 through Mn**, with the spec critique findings folded in. Each milestone independently demoable. M1 validates against known-answer tests.
- **ADR 0002: skeptical review of the roadmap**.
- **ADR 0003: core architecture sketch and review**. Class and protocol hierarchy, event-loop diagram, the policy-versus-execution split, the data-layer PIT API.

Only after these land does engine code start.
