# Bailey-Lopez de Prado on Backtest Overfitting

Last updated: 2026-05-28. Covers the five core papers in the multiple-testing-in-finance literature.

---

## Executive Summary

- **The multiple-testing thesis**: every additional strategy configuration tested on historical data inflates the probability that the best in-sample result is a false positive. With enough trials, a Sharpe ratio of 1 or higher can be produced by pure noise. Quantifying this inflation is the shared project of all five papers.
- **Headline formulas**: (1) MinBTL tells you how much data you need given the number of trials; (2) PSR tells you the probability that a single observed SR exceeds a benchmark, corrected for non-normality; (3) DSR deflates PSR further by the expected maximum SR from N trials; (4) PBO uses combinatorial cross-validation to estimate the fraction of parameter sweeps that produce overfitted strategies.
- **Contested territory**: Lopez de Prado and Harvey-Liu both correct for multiple testing but differ in mechanism. LdP computes the expected maximum SR from order statistics of independent Gaussian draws and deflates accordingly. Harvey-Liu apply frequentist FWER / FDR corrections (Bonferroni, Holm, BHY) to test statistics, then back-convert to haircut SRs. The two frameworks produce quantitatively different haircuts, and neither requires the other's assumptions.
- **What is unresolved**: the effective number of independent trials N is hard to estimate in practice; the two schools propose different methods for its estimation. There is also no agreed method for handling autocorrelated returns when computing the SR variance.
- **Design implications for pit-backtest**: every strategy report must emit PSR, DSR, MinTRL, and drawdown statistics by default. Parameter sweeps must surface N (effective trials) and emit a PBO estimate. A hard warning must appear whenever SR is reported without a DSR correction.

---

## Sources Accessed

**Successfully read (full or near-full technical content):**

1. Bailey, D.H., Borwein, J., Lopez de Prado, M., Zhu, Q.J. (2014). "Pseudo-Mathematics and Financial Charlatanism." *Notices of the AMS* 61(5): 458-471.. Preprint at davidhbailey.com; AMS PDF returned 403. Core MinBTL theorem and worked example verified from preprint and secondary sources.
2. Bailey, D.H. and Lopez de Prado, M. (2014). "The Deflated Sharpe Ratio." *Journal of Portfolio Management* 40(5): 94-107.. Preprint at davidhbailey.com (deflated-sharpe.pdf). Full formula set confirmed; specific formula quotes cross-checked against Wikipedia, portfoliooptimizer.io, and the quantstrat R implementation.
3. Bailey, D.H. and Lopez de Prado, M. (2012). "The Sharpe Ratio Efficient Frontier." *Journal of Risk* 15(2).. Preprint at davidhbailey.com (sharpe-frontier.pdf); binary PDF not directly parseable, but PSR and MinTRL formulas confirmed from portfoliooptimizer.io detailed treatment and QuantConnect replication.
4. Bailey, D.H., Borwein, J., Lopez de Prado, M., Zhu, Q.J. (2017). "The Probability of Backtest Overfitting." *Journal of Computational Finance* 20(4): 39-69.. Multiple preprint locations; binary PDF not directly parseable. CSCV algorithm reconstructed from CRAN pbo package README, MQL5 implementation article, and Balaena Quant Insights replication.
5. Harvey, C.R. and Liu, Y. (2015). "Backtesting." *Journal of Portfolio Management* 42(1): 17-28.. Available at people.duke.edu and CME Group. Binary PDF not directly parseable; full technical content confirmed from OpenSourceQuant review and Harvey-Liu-Zhu (HLZ 2015) related work.

**Partially accessed or used as cross-checks:**
- Wikipedia: Deflated Sharpe Ratio (full formula table confirmed)
- portfoliooptimizer.io: Detailed PSR and MinTRL derivations
- rdrr.io: quantstrat R package SharpeRatio.deflated parameter documentation
- Balaena Quant Insights Medium series: PBO replication and DSR worked examples
- OpenSourceQuant: Harvey-Liu R-view with complete Bonferroni/Holm/BHY procedure

**Could not access:** SSRN abstract pages returned 403. AMS PDF returned 403.

---

## Pseudo-Mathematics and Financial Charlatanism (Bailey, Borwein, Lopez de Prado, Zhu 2014)

### Core Argument

Published in the *Notices of the American Mathematical Society* (May 2014). The paper's central claim: with enough backtests, *any* pattern can be discovered in historical data, and the reported Sharpe ratio has no predictive value for out-of-sample performance. The mechanism is standard (N independent tests at level alpha yield N*alpha false discoveries on average), but the novel contribution is quantifying the problem in Sharpe ratio terms via a concrete formula for the required backtest length.

### The Minimum Backtest Length (MinBTL) Formula

The paper's central theorem (Theorem 3.1 in the preprint; the published version uses the same result) gives the minimum backtest length in years needed to avoid selecting a strategy with expected out-of-sample SR of zero, when choosing the best among N independent trials each targeting an expected maximum Sharpe ratio of E[max_N]:

```
MinBTL >= [ ( (1 - gamma) * Z^{-1}[1 - 1/N] + gamma * Z^{-1}[1 - 1/(N*e)] )
            / E[max_N] ]^2 * (1/4)
```

Where:
- gamma = Euler-Mascheroni constant, approximately 0.5772
- Z^{-1} = inverse standard normal CDF (quantile function)
- N = number of independent strategy trials
- e = Euler's number, approximately 2.718
- E[max_N] = target annualized Sharpe ratio threshold (what the researcher wants the best strategy to achieve)
- The formula assumes monthly returns (factor of 1/4 converts to years from quarterly; derivations differ slightly by return frequency convention)

Proposition 1 in the same paper notes that this formula assumes N trials are fully independent. When trials are correlated (as is always true in practice for closely related parameterizations), the effective N is smaller and can be estimated via PCA or the ONC clustering algorithm described in Lopez de Prado (2018).

### Worked Example

From the paper's discussion: if a researcher has access to 5 years of data, and desires to find a strategy with annualized Sharpe of 1.0, no more than approximately 45 independent model configurations should be tested. Equivalently, a researcher who tests 100 independent configurations needs at least 7.5 years of data to be confident that the best in-sample SR of 1.0 is not purely an artifact of selection.

### The Simulation Corroboration

The paper includes a simulation study. The authors generate a price series with no exploitable structure, then conduct a grid search over 1,282 parameterizations of a simple moving-average strategy. The best in-sample Sharpe ratio from this purely random series reaches 2.35. This figure is the paper's most-cited concrete result: a Sharpe ratio of more than 2 can be manufactured from noise by testing enough variants.

---

## Probability of Backtest Overfitting (PBO)

### Definition

Bailey, Borwein, Lopez de Prado, and Zhu (2017, *Journal of Computational Finance*; SSRN 2014 preprint) define PBO as:

```
PBO = Pr[ rank_OOS(best_IS) < median_rank_OOS ]
```

A PBO of 0.5 means selection is no better than random. A PBO near 0 indicates robust selection. PBO is empirically estimated via the CSCV procedure rather than analytically assumed.

### Combinatorially Symmetric Cross-Validation (CSCV) Construction

CSCV avoids the two main failure modes of standard cross-validation applied to strategy selection:

1. **Temporal leakage**: Standard k-fold CV shuffles observations, allowing future data into the training set for time-series strategies.
2. **Single-path variance**: Standard holdout tests give one OOS estimate, yielding high variance and sensitivity to the particular holdout window chosen.

The CSCV algorithm:

**Step 1.** Collect bar-by-bar (or period-by-period) PnL for all N strategy configurations across the full backtest period of length T. Form a matrix M of shape (T, N).

**Step 2.** Partition the T time periods into S equal, contiguous, non-overlapping blocks. S must be even; the paper recommends S between 8 and 16. The key constraint is contiguity: blocks preserve temporal order.

**Step 3.** Generate all C(S, S/2) combinations. Each combination selects S/2 blocks as the in-sample set (ISS) and the remaining S/2 blocks form the out-of-sample set (OOSS). For S = 10, C(10, 5) = 252 combinations; for S = 16, C(16, 8) = 12,870 combinations.

**Step 4.** For each combination c:
- Concatenate the ISS blocks vertically to form the in-sample matrix.
- Concatenate the OOSS blocks vertically to form the out-of-sample matrix.
- Compute the performance metric (Sharpe ratio, or Omega ratio) for each of the N strategies on both matrices.

**Step 5.** For each combination c, identify strategy n* with the best in-sample performance. Record its out-of-sample rank among the N strategies as rank_c(n*).

**Step 6.** Compute the relative rank: omega_c = (rank_c(n*) - 0.5) / N, so omega is in (0, 1).

**Step 7.** Apply the logit transformation:
```
lambda_c = log( omega_c / (1 - omega_c) )
```
A lambda below 0 means the best in-sample strategy ranked below the median out-of-sample.

**Step 8.** Estimate the kernel density of the lambda values across all C combinations. PBO is the probability mass below zero:
```
PBO = Pr( lambda < 0 ) = (number of combinations where lambda_c < 0) / C
```

### Why CSCV Avoids Leakage

Every block appears in the ISS exactly C(S-1, S/2-1) times and in the OOSS exactly C(S-1, S/2) times. Because blocks are contiguous and splits occur at block boundaries, there is no shuffling of time-series observations and no future contamination of training data. Unlike single holdout (one OOS estimate), CSCV produces C estimates forming a distribution, enabling statistical inference about PBO itself.

### Computational Properties

Combinations: C(8,4)=70; C(10,5)=252; C(16,8)=12,870. Each combination requires N SR computations on two sub-matrices. Precomputing block-level returns and summing them is far more efficient than re-running full strategies per combination. The OpenSourceQuant replication with 8,800 parameterizations took approximately 22 minutes on a single core; vectorized implementations are substantially faster.

---

## Deflated Sharpe Ratio (DSR)

### The Full Formula

Bailey and Lopez de Prado (2014; *Journal of Portfolio Management* 40(5): 94-107) build DSR in two steps: first the multiple-testing threshold SR_0, then the PSR formula evaluated at SR_0 rather than an arbitrary benchmark.

**Step 1. Asymptotic variance of the SR estimator** (Lo 2002, extended to non-normal returns):

```
Var[SR_hat] = (1 - gamma_3 * SR + (gamma_4 - 1) / 4 * SR^2) / T
```

For normal returns (gamma_3=0, gamma_4=3) this reduces to the familiar (1 + SR^2/2)/T. Negative skewness and excess kurtosis both inflate the variance, which is why crash-prone strategies receive a larger DSR penalty.

**Step 2. The multiple-testing threshold from N trials:**

```
SR_0 = sqrt( V[SR_hat_n] ) * [ (1 - gamma) * Z^{-1}[1 - 1/N]
                                 + gamma * Z^{-1}[1 - 1/(N*e)] ]
```

Where:
- V[SR_hat_n] = cross-sectional variance of the SR estimates across all N trials
- gamma = Euler-Mascheroni constant (~0.5772)
- Z^{-1} = inverse standard normal CDF
- N = effective number of independent trials
- e = Euler's number

SR_0 is the Sharpe ratio threshold a strategy must exceed merely by chance when N independent unskilled strategies are evaluated; it comes from order statistics of i.i.d. standard normals.

**Step 3. The DSR formula:**

```
DSR = Phi( (SR_hat - SR_0) * sqrt(T - 1) / sqrt(1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2) )
```

Where:
- SR_hat = observed (estimated) annualized Sharpe ratio
- SR_0 = threshold from Layer 3
- T = sample length in observations
- gamma_3, gamma_4 = skewness and kurtosis of returns
- Phi = standard normal CDF

DSR is a probability: the probability that the true SR exceeds SR_0 given T observations, after correcting for the fact that the strategy was the best among N candidates. DSR replaces PSR's arbitrary benchmark SR* with the data-derived SR_0.

**Note on the asymptotic variance form.** The `sigma_sq` denominator uses `SR_hat` (the unrestricted-MLE plug-in) per the Bailey-LdP 2014 Wald form, matching the PSR formula at the section below. An earlier revision of this doc had `SR_0` in the denominator, which is a Score / Rao form; both are asymptotically equivalent under the null, but the Wald form is what Bailey-LdP 2014 publishes and what `analytics/sharpe.py` implements per [ADR 0013](../../decisions/0013-psr-dsr-mintrl-public-api-and-bailey-ldp-2014-pin-correction.md).

### The Effective Number of Trials

N in the DSR formula should not be the raw count of backtests if many are highly correlated. Lopez de Prado (2018, *Advances in Financial Machine Learning*) proposes clustering strategies by their correlation of OOS returns, then setting N equal to the number of clusters (using the Optimal Number of Clusters algorithm). As a simpler heuristic: if testing a parameter grid with dimensions d1 x d2 x d3, count the number of unique PCA components explaining 95% of variance in the resulting SR matrix. The quantstrat implementation accepts `nTrials` and `varTrials` as separate inputs, allowing the analyst to supply their own estimate.

### Worked Example

Inputs: SR_hat = 1.5 (annualized), T = 60 months, gamma_3 = -0.5, gamma_4 = 5, N = 30 trials, V[SR_hat_n] = 0.4.

Quantiles (verified against scipy.stats.norm.ppf):

- `Phi_inv(1 - 1/30) = Phi_inv(0.96667) = 1.834`
- `Phi_inv(1 - 1/(30 * e)) = Phi_inv(0.98774) = 2.249`

SR_0 = sqrt(0.4) * [(1 - 0.5772) * 1.834 + (0.5772) * 2.249] = 0.6325 * 2.0736 = 1.311.

sigma_sq = 1 - (-0.5) * 1.5 + (5 - 1)/4 * 1.5^2 = 1 + 0.75 + 2.25 = 4.0 (Wald form, SR_hat in the denominator per the Step 3 formula above).

DSR = Phi( (1.5 - 1.311) * sqrt(59) / sqrt(4.0) ) = Phi(0.725) = 0.766.

The strategy clears at 76.6% confidence. A raw PSR against SR* = 0 gives substantially higher (and misleading) confidence because it ignores that SR_hat was the best of 30 candidates.

**Pre-correction note.** An earlier revision of this worked example reported `SR_0 = 1.092` and `DSR = 0.971`, derived from incorrect quantile values (`1.869` and `1.624` instead of the verified `1.834` and `2.249`) and a Score / Rao `sigma_sq` form using `SR_0` instead of the Wald `SR_hat` form. The 0.971 number propagated through ADR 0002 (acceptance criterion 1), ADR 0003 (decision 14 docstring), `docs/ROADMAP.md`, and `docs/methodology/dataset_versioning.md`. [ADR 0013](../../decisions/0013-psr-dsr-mintrl-public-api-and-bailey-ldp-2014-pin-correction.md) corrects all five sites and locks the canonical pin at `DSR = 0.766 within 1e-3` for the M4 PR 1 acceptance test.

---

## Probabilistic Sharpe Ratio (PSR)

### The Full Formula

Bailey and Lopez de Prado (2012; *Journal of Risk* 15(2)) introduced PSR in "The Sharpe Ratio Efficient Frontier." PSR answers: given T observations with estimated SR, skewness, and kurtosis, what is the probability that the true SR exceeds a benchmark SR*?

```
PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(T - 1)
                / sqrt(1 - gamma_hat_3 * SR_hat + (gamma_hat_4 - 1)/4 * SR_hat^2) )
```

Where:
- Phi = standard normal CDF
- SR_hat = estimated Sharpe ratio (non-annualized, in the same units as the return observations)
- SR* = benchmark / reference Sharpe ratio (often 0 or the Sharpe of a passive benchmark)
- T = number of return observations
- gamma_hat_3 = observed skewness (third standardized moment)
- gamma_hat_4 = observed kurtosis (fourth standardized moment, non-excess form; normal = 3)

The denominator is the standard error of the SR estimator under the asymptotic distribution, incorporating the skewness and kurtosis correction from Lo (2002). For normal returns (gamma_3 = 0, gamma_4 = 3), the denominator reduces to sqrt((1 + SR_hat^2/2) / (T-1)).

### Relationship to DSR

PSR is a special case of DSR where the benchmark SR* is fixed by the analyst rather than derived from the distribution of N trials. DSR replaces SR* with SR_0, the expected maximum Sharpe from N unskilled independent trials. If N = 1 (only one strategy was ever tested), DSR reduces to PSR with SR* set to some baseline.

In practice: use PSR when reporting a single strategy in isolation. Use DSR whenever any form of parameter search, strategy selection, or model averaging was performed, because SR_0 embeds the cost of selection.

### Confidence Interval Framing

PSR(SR*) = alpha means rejecting H_0: SR <= SR* at confidence alpha. The critical minimum observed SR needed at confidence alpha_0 is:

```
SR_hat_critical = SR* + Z^{-1}(alpha_0) * sqrt( (1 - gamma_3*SR* + (gamma_4-1)/4*SR*^2) / (T-1) )
```

### Numerical Illustration

From QuantConnect's replication: SR_hat = 0.458, SR* = 0, T = 120 monthly observations.

- Normal returns (gamma_3=0, gamma_4=3): PSR(0) = Phi(4.776) = 0.9999.
- Non-normal returns (gamma_3=-2.448, gamma_4=10.164): denominator expands to 1.613, PSR(0) = Phi(3.097) = 0.9990.

The non-normal adjustment costs 0.9 percentage points here but becomes severe at shorter track records or with heavier tails.

---

## Minimum Track Record Length (MinTRL)

### The Formula

MinTRL answers the inverse PSR question: given observed SR_hat, skewness, and kurtosis, and a desired confidence level (1 - alpha), how many observations T are needed?

Inverting the PSR formula for T:

```
MinTRL(SR*) = 1 + (1 - gamma_hat_3 * SR_hat + (gamma_hat_4 - 1)/4 * SR_hat^2)
              * (Z^{-1}(1 - alpha) / (SR_hat - SR*))^2
```

Where:
- Z^{-1}(1 - alpha) = the (1-alpha) quantile of the standard normal (e.g., 1.645 for alpha=0.05, 1.960 for alpha=0.025)
- SR* = benchmark Sharpe ratio (usually 0)
- All other notation as above

When SR* is replaced by SR_0 (the DSR threshold), MinTRL gives the minimum track record needed for the DSR to exceed a target confidence level.

### Concrete Numbers

SR_hat = 1.0 annualized, SR* = 0, alpha = 0.05 (95% confidence):

- Normal returns (gamma_3=0, gamma_4=3): MinTRL = 1 + 1.5 * 2.706 = 5.1 months.
- Non-normal options-selling profile (gamma_3=-1.0, gamma_4=6): MinTRL = 1 + 3.25 * 2.706 = 9.8 months, roughly 10 months.
- SR_hat = 0.5 with the same non-normal profile: MinTRL = 1 + 1.8125 * 10.824 = 20.6 months.

The formula's sensitivity to SR_hat is quadratic: halving the Sharpe roughly quadruples the required track record.

---

## Harvey-Liu Critique and Alternative

### The Harvey-Liu Framework

Harvey and Liu (2015; *Journal of Portfolio Management* 42(1): 17-28) approach multiple testing from classical FWER / FDR correction. Each strategy t-statistic is converted to a p-value via t = SR * sqrt(T); then one of three corrections is applied:

- **Bonferroni** (controls FWER, most stringent): `p_i^Bonferroni = min(M * p_i, 1)`.
- **Holm** (sequential step-down): `p_{(i)}^Holm = min( max_{j <= i}( (M-j+1) * p_{(j)} ), 1 )`.
- **BHY** (controls FDR, least stringent): scales by harmonic constant c(M) = sum_{j=1}^{M} 1/j; for M=6, c(M)=2.45.

Harvey, Liu, and Zhu (HLZ 2015) document that at least 316 factors had been published by 2012. Assuming all tested factors are published (lower bound on publication bias), the minimum t-statistic for 5% significance is approximately 2.8 (vs. conventional 1.96). Adjusting for an estimated 71% unpublished tests, the hurdle rises to t >= 3.18. The non-uniform haircut property: marginal strategies receive the harshest proportional cuts; exceptional high-SR strategies receive only modest penalties.

### Where Harvey-Liu and LdP Agree

Both agree that unadjusted in-sample SR is invalid after any form of search, that the effective N (not the raw count) is the relevant quantity, and that the practical hurdle for genuine alpha is substantially above industry convention.

### Where They Differ

**Mechanistic**: LdP computes SR_0 from the variance of SR estimates across trials via order statistics. Harvey-Liu work directly with t-statistics and p-values, optionally adding Bayesian shrinkage on the prior probability a strategy is genuine.

**Data requirements**: DSR requires the full SR distribution (mean, variance) across all N trials. The Harvey-Liu haircut requires only individual test statistics and M, making it easier to apply to published academic results where the full trial distribution is unknown.

**Correlation handling**: BHY and Holm are valid under arbitrary test correlation; LdP's SR_0 formula assumes independence, with ONC clustering as a separate correction. Harvey-Liu's mixed-distribution model interpolates by assumed rho.

**Severity**: Harvey-Liu's haircut is non-uniform, sparing high-SR strategies and penalizing marginal ones more heavily. LdP's deflation is applied uniformly via SR_0. No empirical study has determined which is better calibrated out-of-sample.

---

## Hooks into pit-backtest Design

### Statistics to Compute by Default

Every strategy report produced by pit-backtest's analytics layer must include:

1. **Raw SR**: annualized Sharpe, with return frequency documented.
2. **PSR(SR* = 0)**: probability that the true SR is positive, corrected for skewness and kurtosis. Report the observed gamma_3 and gamma_4 alongside.
3. **DSR**: PSR deflated by SR_0, using the effective trial count N from the current run context. If N is unknown (e.g., single one-off run), DSR is undefined and must be marked as such.
4. **MinTRL**: minimum observations needed to confirm the observed SR at 95% confidence against SR* = 0 given the observed return distribution shape.
5. **Drawdown statistics**: maximum drawdown, average drawdown, drawdown duration, Calmar ratio. These feed the MinTRL denominator and also separately characterize tail risk.

### Surfacing Effective Trials in Parameter Sweeps

When a user runs a parameter sweep, pit-backtest must: record per-strategy return series; build the (T, N_raw) PnL matrix; apply ONC or PCA-eigenvalue clustering to derive N_effective; report N_raw, N_effective, and SR_0; run CSCV with S=10 (default) to produce a PBO estimate and logit distribution. Emit a warning if PBO > 0.1; escalate to critical if PBO > 0.3.

### Default Warning for Uncorrected Sharpe

Any code path outputting SR without an accompanying DSR or PSR must emit an un-suppressible warning: "Reported SR has not been deflated for selection bias. DSR requires the full strategy distribution; use analytics.deflated_sr() with N_raw and varTrials." This must appear in logs, the strategy report, and HTML output.

### Cross-Reference with Walk-Forward and Purged CV

CSCV (for PBO) and purged CV (for walk-forward validation) address different leakage problems; see [[methodology-afml-backtesting]]. The integration point: when pit-backtest runs CPCV, simultaneously compute PBO on the per-fold strategy rankings. CPCV yields the OOS performance distribution; CSCV yields the probability that the best IS configuration is a selection artifact. Neither replaces the other. The S CSCV blocks must be contiguous in calendar time and should align with the walk-forward windows.

---

## Open Questions

1. **N_effective estimation**: No agreed-upon method. LdP proposes ONC clustering on OOS return correlations; Harvey-Liu interpolate by assumed rho. PCA-based N_eff (components explaining 95% of SR variance across a parameter sweep) is likely the most tractable default for pit-backtest.

2. **Autocorrelation and SR variance**: Lo (2002)'s asymptotic formula assumes IID returns. Mildly autocorrelated returns (common intraday) inflate SR estimates. Neither PSR nor DSR addresses this directly. A Newey-West correction on the SR standard error is necessary as a pre-processing step.

3. **Harvey-Liu vs. LdP severity**: At low N (5 to 50 trials) the frameworks agree within roughly 20% on haircut magnitude. At large N (hundreds to thousands), LdP's SR_0 grows logarithmically while Bonferroni grows linearly. For typical parameter sweeps (N_effective 50 to 200), the implied hurdle difference is strategically significant. The right choice is unresolved.

4. **PBO with non-Sharpe objectives**: CSCV applies to any performance metric; the logit transformation and PBO interpretation remain valid. The metric should be pluggable in pit-backtest's implementation.

5. **MinBTL vs. MinTRL distinction**: MinBTL asks "how much data do I need to avoid selecting a false positive from N trials?" MinTRL asks "how much data do I need to confirm this single strategy is genuine?" The two answer different questions and should appear separately in every strategy report.

6. **PSR benchmark choice**: PSR(0) is standard but arbitrary; for equity strategies a more meaningful benchmark is the SR of the passive index. SR* should be a user-configurable parameter.

---

## Sources

1. Bailey, D.H., Borwein, J., Lopez de Prado, M., Zhu, Q.J. (2014). "Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance." *Notices of the American Mathematical Society* 61(5): 458-471. Preprint: https://www.davidhbailey.com/dhbpapers/backtest-pseudo.pdf

2. Bailey, D.H., Borwein, J., Lopez de Prado, M., Zhu, Q.J. (2017). "The Probability of Backtest Overfitting." *Journal of Computational Finance* 20(4): 39-69. Preprint (CARMA version): https://carmamaths.org/resources/jon/backtest2.pdf

3. Bailey, D.H. and Lopez de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality." *Journal of Portfolio Management* 40(5): 94-107. Preprint: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf

4. Bailey, D.H. and Lopez de Prado, M. (2012). "The Sharpe Ratio Efficient Frontier." *Journal of Risk* 15(2). Preprint: https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf

5. Harvey, C.R. and Liu, Y. (2015). "Backtesting." *Journal of Portfolio Management* 42(1): 17-28. Available: https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF

6. Harvey, C.R., Liu, Y., and Zhu, H. (2016). "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1): 5-68. SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314 (documents 316 tested factors; provides empirical basis for t-ratio >= 3.0 argument)

7. Lo, A.W. (2002). "The Statistics of Sharpe Ratios." *Financial Analysts Journal* 58(4): 36-52. (Foundational paper for the SR asymptotic distribution used in PSR and DSR)

8. Lopez de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. Chapters 7-8 (purged CV), Chapter 11 (backtest statistics). (Operationalizes MinTRL, ONC clustering for N_effective, and CPCV)

9. portfoliooptimizer.io detailed PSR and MinTRL derivation: https://portfoliooptimizer.io/blog/the-probabilistic-sharpe-ratio-bias-adjustment-confidence-intervals-hypothesis-testing-and-minimum-track-record-length/

10. Wikipedia: Deflated Sharpe Ratio (formula verification): https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio

11. quantstrat R package: SharpeRatio.deflated documentation (confirms parameter set for DSR implementation): https://rdrr.io/github/braverock/quantstrat/man/SharpeRatio.deflated.html

12. Balaena Quant Insights, Liana Ling: PBO CSCV replication and DSR worked examples: https://medium.com/balaena-quant-insights/the-probability-of-backtest-overfitting-pbo-9ba0ac7fb456

13. OpenSourceQuant: Harvey-Liu (2015) R-view with Bonferroni/Holm/BHY procedure: https://opensourcequant.wordpress.com/2016/11/17/r-view-backtesting-harvey-liu-2015/

14. Wikipedia: Purged Cross-Validation (CPCV vs CSCV distinction): https://en.wikipedia.org/wiki/Purged_cross-validation
