# AFML Chapters 11-15 on Backtesting

> **Source document for pit-backtest architecture decisions.**
> Research date: 2026-05-28. Primary source: Marcos Lopez de Prado, *Advances in Financial Machine Learning* (Wiley, 2018). All chapter and section references are to that book unless otherwise stated.

---

## Executive summary

- **Backtesting is not a research tool.** Lopez de Prado's central thesis (Ch. 11) is that iterating a strategy design against a backtest engine is epistemically equivalent to training on the test set. The correct sequence is: form hypothesis from theory, specify model completely, then run one backtest as a sanity check, not as a search procedure.

- **Standard k-fold CV is broken for financial time series.** Because financial labels (e.g., triple-barrier outcomes) depend on future price paths, training observations whose label horizons overlap with the test window introduce forward leakage. Purged k-fold CV (Ch. 12) removes those observations; the embargo period removes the residual serial-correlation leakage that purging alone cannot eliminate.

- **Combinatorial Purged Cross-Validation (CPCV) replaces walk-forward as the primary backtesting paradigm.** By partitioning data into N groups and testing all C(N,k) combinations, CPCV produces a *distribution* of out-of-sample Sharpe ratios across phi(N,k) = (k/N) * C(N,k) distinct backtest paths. This distribution is the right object to analyze, not a single scalar.

- **The Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR) are the minimum-viable statistics for any production backtester.** The DSR applies the False Strategy Theorem to compute the expected maximum Sharpe ratio that a purely random strategy could have produced after N independent trials, then tests whether the observed SR clears that bar. MinTRL tells you how many return observations are required before a given SR estimate is statistically credible.

- **Chapter 15 models strategy risk as a function of four parameters** (precision p, frequency n, profit target pi+, stop-loss pi-) and shows that, for symmetric payouts, Sharpe depends only on p and n. Capacity, drawdown, and Time Under Water are presented as the three risk dimensions that matter for real-money deployment. The Triple Penance Rule (from companion papers) provides the key drawdown-recovery relationship.

---

## Sources accessed

**Directly read (full text or substantial excerpt):**
- Bailey and Lopez de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality," davidhbailey.com preprint. Full formulas extracted.
- Bailey, Borwein, Lopez de Prado, Zhu (2015), "The Probability of Backtest Overfitting," davidhbailey.com preprint. Partial extraction (PDF binary on some fetches; key methodology obtained via the SDM reprint).
- Wikipedia: Purged Cross-Validation article. Algorithm and formulas confirmed.
- Wikipedia: Deflated Sharpe Ratio article. Formulas confirmed and variable definitions extended.
- reasonabledeviations.com chapter notes for AFML (secondary summary).
- portfoliooptimizationbook.com section 8.3 "The Dangers of Backtesting" (secondary, cites LdP 2018a).
- CanerIrfanoglu/advances_in_ml GitHub chapter summaries, Ch. 11-15.
- quantbeckman.com CPCV implementation article (secondary with code).
- marti.ai blog post on DSR with explicit formulas.
- random-docs.readthedocs.io mlfinlab backtest statistics documentation.
- quantresearch.org Innovations page (Lopez de Prado's own publication list).
- quantconnect.com PSR implementation article.
- bhakta-works.medium.com article on CV pitfalls.

**Identified but full text not accessible (403 or binary PDF only):**
- davidhbailey.com/dhbpapers/stop-out.pdf (binary; Triple Penance Rule obtained via secondary sources and SSRN abstracts).
- davidhbailey.com/dhbpapers/backtest-prob.pdf (binary PDF; PBO methodology obtained via the SDM reprint summary).
- SSRN abstract page for abstract_id=2460551 (403 forbidden; formulas confirmed via davidhbailey.com preprint).
- O'Reilly chapter previews for Ch. 11, 12, 13 (paywall; structure and section headings confirmed, not full text).

**What is reconstructed from secondary sources:**
- The specific historical examples LdP may cite in Ch. 11 (case studies) could not be verified from available text; the chapter structure is confirmed from O'Reilly TOC.
- The exact pseudocode for PurgedKFoldCV from the book (Ch. 7/12) is reconstructed from Wikipedia's purged CV article and the bhakta-works Medium article, both of which cite AFML as primary source.
- Chapter 15 formula for SR under symmetric payouts is reconstructed from the CanerIrfanoglu summary and the Medium AFML Part 3 article, which directly paraphrase LdP's derivations.

---

## Ch 11: The Dangers of Backtesting

### LdP's taxonomy of backtest errors

Lopez de Prado frames the core problem as follows (paraphrased from Ch. 11 and confirmed from multiple secondary sources):

> "Backtesting is one of the most essential, and yet least understood, techniques in the quant arsenal. Most backtests published in journals are flawed, as the result of selection bias on multiple tests." (LdP 2018, Ch. 11, cited in portfoliooptimizationbook.com sec. 8.3 and Caner Irfanoglu summary)

The chapter draws on the "Seven Sins of Quantitative Investing" framework attributed to Luo et al. (2014) from Deutsche Bank (portfoliooptimizationbook.com sec. 8.2, confirmed). LdP applies that taxonomy to backtesting specifically:

1. **Survivorship bias.** The live investment universe excludes companies that went bankrupt, delisted, or merged. A backtest run on the current universe implicitly assumes you would have known which firms would survive, inflating returns. The fix requires point-in-time universe construction with a survivorship-free database.

2. **Look-ahead bias (information leakage).** Using data that was not publicly available at the simulated decision timestamp. Common sources: using adjusted close prices that incorporate future dividends or splits without reconstruction, using quarterly earnings data filed months after the period, or using end-of-day prices to simulate intraday signals. In ML pipelines, look-ahead also enters through feature normalization computed over the full dataset before the train-test split.

3. **Storytelling bias (post-hoc rationalization).** Identifying a strategy's parameters empirically and then constructing a plausible narrative to justify them. LdP calls this "researching under the influence of a backtest." The narrative makes the model appear principled when it is actually a curve fit. The mitigation is to specify the economic hypothesis *before* examining the data; the backtest then tests the hypothesis rather than generating it.

4. **Data snooping / multiple testing.** Evaluating many parameter configurations and reporting only the best performer creates a selection bias. With enough trials, any desired Sharpe ratio can be achieved on historical data by chance. This is the core motivation for Chapters 13 and 14. The False Strategy Theorem (Bailey and Lopez de Prado 2018, SSRN 3221798) shows that the expected maximum Sharpe ratio from N independent trials is unbounded as N grows, meaning no fixed SR threshold can guard against this without adjusting for N.

5. **Transaction cost neglect.** Ignoring commissions, bid-ask spread, market impact, borrowing costs for short positions, and financing costs for leverage. LdP notes that institutional strategies are often tested on closing prices with no slippage model, which can hide a fundamentally unprofitable strategy behind frictionless execution assumptions.

6. **Non-repeatable outliers.** Performance driven by one or two extreme return periods. LdP's concentration metric (HHI applied to returns; see Ch. 14) detects this. A strategy whose entire backtest alpha comes from three weeks in 2008 has not demonstrated a persistent edge.

7. **Asymmetric payoff and shorting mechanics.** Shorting has costs and constraints (uptick rules, borrow availability, margin requirements) that are rarely modeled accurately. Strategies long volatility or gamma face asymmetric payoff distributions that are not captured by Sharpe ratio alone.

### The "backtest is a hypothesis test" framing

LdP frames backtesting as hypothesis testing rather than discovery. The null hypothesis is "the strategy has no edge; any observed performance is due to chance." The backtest is a test of that null. The key implication: just as a hypothesis test is invalidated by examining the data before specifying the hypothesis, a backtest is invalidated by iterating strategy design against the same data.

From portfoliooptimizationbook.com sec. 8.3 (directly citing LdP 2018):

> "LdP's Second Law: Backtesting while researching is like drink driving. Do not research under the influence of a backtest."

Defensible backtesting practice, per LdP, requires:

- Completing all model specification (features, signal logic, position sizing, exit rules) before running the first backtest.
- Recording *every* trial conducted on a dataset, not just the final submission.
- Reporting the DSR or PSR alongside the raw Sharpe, so readers can judge how much of the SR is accounted for by trial count.
- Applying the deflation methodology (Ch. 14) to adjust for the number of configurations tested.

### What constitutes evidence vs. confirmation bias

LdP's standard for a backtest that constitutes genuine evidence (reconstructed from Ch. 11 section headings and secondary summaries):

- The strategy has an a priori economic rationale that predicts the specific form of the edge.
- The backtest was run once (or the number of runs was recorded and deflation applied).
- The strategy works across multiple instruments in the same asset class, not just on the one instrument used to fit parameters.
- Performance is not attributable to one non-repeatable event.
- Transaction costs are calibrated to execution data, not assumed zero.
- The track record length satisfies MinTRL (Ch. 14) at a stated significance level.

A backtest that fails any of these criteria is, in LdP's terminology, "confirmation bias masquerading as evidence."

---

## Ch 12: Backtesting Through Cross-Validation

### Why standard k-fold fails for financial time series

Standard k-fold CV assumes observations are independently and identically distributed. In financial ML, this assumption fails in two distinct ways:

**1. Label horizon overlap.** Supervised learning on financial data often uses labels derived from future prices: a triple-barrier label for trade t depends on prices from t through t+h. If fold i contains t in the training set and fold j contains t+k (where k < h) in the test set, the training label for t was computed using price data that overlaps with the test period. Training the model on that observation gives it implicit knowledge of what happens during the test window. Standard k-fold does not remove these observations.

**2. Serial correlation leakage.** Even without label overlap, nearby returns are serially correlated. An observation immediately before the test window carries information about the test window through autocorrelation in prices and volatility. Simply removing overlapping labels is not sufficient.

From Wikipedia's purged cross-validation article (confirming AFML Ch. 7/12):

> "Purging involves removing any observation whose label horizon overlaps with the test period, ensuring that future information does not influence model training."

### Purged k-fold: definition, embargo period, why both purge AND embargo

**Purging** removes from the training set any observation i whose label horizon [i, i+h_i] overlaps with the test interval [t_start, t_end]. Formally, any observation i with i+h_i >= t_start is excluded from training for that fold.

**Embargoing** removes the observations immediately *after* the test set, before they become part of the next training fold. Even after purging, the model could still see observations that are serially correlated with the test window because the embargo window comes at the *start* of the post-test training period, not inside the test period. With a 5% embargo and 1000 observations, the 50 observations immediately following each test fold are excluded from training (Wikipedia purged CV article, confirmed).

Both mechanisms are required because they address different failure modes:
- Purging addresses *label horizon overlap* inside the test window.
- The embargo addresses *autocorrelation leakage* from the test window into the subsequent training data.

Removing only one leaves a residual information channel.

**Algorithm: Purged K-Fold CV** (reconstructed from Wikipedia purged CV article, bhakta-works Medium article, both citing AFML Ch. 7):

```
Input: observations indexed 1..T with label horizons h_1..h_T
       k (number of folds), embargo_pct (fraction of T to embargo)
       embargo_count = floor(T * embargo_pct)

1. Split [1..T] into k contiguous, non-overlapping folds F_1, F_2, ..., F_k

2. For each fold F_j as the test set:
   a. Let t_start = min(F_j), t_end = max(F_j)
   b. PURGE: Remove from training any observation i where i + h_i >= t_start
      (label horizon reaches into the test window)
   c. EMBARGO: Remove from training any observation i where
      t_end < i <= t_end + embargo_count
      (observations immediately after the test window)
   d. Training set = {1..T} \ F_j \ purged \ embargoed
   e. Fit model on training set, evaluate on F_j

3. Aggregate performance across k folds
```

### Combinatorial Purged Cross-Validation (CPCV): construction, count of paths, statistical properties

CPCV is the extension of purged k-fold to a combinatorial setting. Instead of rotating a single held-out fold, CPCV tests all C(N,k) combinations of k held-out groups out of N total groups.

**Construction:**

1. Partition [1..T] into N non-overlapping, contiguous groups G_1, G_2, ..., G_N (ordered in time).
2. Enumerate all C(N,k) = N! / (k! * (N-k)!) subsets of size k. Each subset defines a test set (the union of the k groups), with the remaining N-k groups as training.
3. For each split: apply purging and embargo as above.
4. Aggregate test-set predictions across all C(N,k) splits by constructing contiguous backtest paths.

**Number of backtest paths:**

Each observation appears in exactly C(N-1, k-1) of the C(N,k) test sets (since fixing one group in the test set leaves C(N-1, k-1) ways to choose the remaining k-1 groups). The total number of distinct, non-overlapping backtest paths is:

```
phi(N, k) = (k / N) * C(N, k)
```

For N=6, k=2: phi = (2/6) * 15 = 5 paths. (Confirmed: Wikipedia purged CV, Towards AI CPCV article.)

**Statistical properties:**

CPCV produces a *distribution* of out-of-sample Sharpe ratios (one per path), not a single scalar. This distribution enables:
- Estimation of the variance of the strategy's SR under different market regimes.
- Detection of path-dependence: if the SR distribution has high variance, the strategy's performance is sensitive to which historical period it happens to see.
- Computation of the 10th-percentile SR as a worst-case estimate (quantbeckman.com CPCV article).
- Use with the DSR: the variance of the SR distribution across paths provides the V[{SR_n}] term in the DSR formula (see Ch. 14).

The walk-forward method is a degenerate case of CPCV with N=T and k=1, producing exactly one path. CPCV generalizes this to phi(N,k) paths using the same data.

**Computational complexity:**

C(N,k) grows combinatorially. For typical parameters (N=6-10, k=2-3), complexity is manageable: C(6,2)=15, C(8,2)=28, C(10,2)=45, C(10,3)=120. For large N or k, practitioners subsample the combination space by drawing S random splits rather than exhausting all C(N,k) (quantbeckman.com implementation; S=200 to 1000 is typical).

---

## Ch 13: Backtesting on Synthetic Data

### Motivation and the multiple-testing problem

Chapter 13 addresses a subtler overfitting mode than data snooping: even if only one backtest is run, the *strategy design* (the choice of trading rule parameters) was informed by the researcher's intuition about historical data. Running the strategy through a synthetic generator sidesteps this by replacing the one observed path with a distribution of plausible paths.

From the O'Reilly chapter preview and CanerIrfanoglu summary (both citing AFML Ch. 13):

> "An alternative backtesting method uses history to generate a synthetic dataset with statistical characteristics estimated from the observed data. The advantage is that conclusions are not connected to a particular observed realization but to an entire distribution of random realizations."

The multiple-testing problem arises because a researcher evaluating a 20x20 grid of (stop-loss, profit-take) parameter combinations is effectively running 400 trials. Even without iterating on the data, the best-performing configuration from 400 independent trials will have a materially inflated SR. The synthetic-data framework addresses this by evaluating *all* configurations simultaneously against a shared distribution of paths, then selecting the winner based on the SR *distribution* (e.g., the median across synthetic paths), not the SR on one specific history.

### The Ornstein-Uhlenbeck framework (Ch. 13)

LdP models price dynamics via the discrete Ornstein-Uhlenbeck (O-U) process, which captures mean reversion, a feature of spread or pairs-trading strategies. The O-U process has two key parameters:
- phi: mean-reversion speed (daily autocorrelation of price changes)
- sigma: conditional volatility

These are estimated from observed data. Then the synthetic path generation proceeds as:

```
Step 1: Fit O-U parameters (phi, sigma) to historical price series.

Step 2: Define trading rule grid.
        Stop-loss levels: -0.5*sigma, -1*sigma, ..., -10*sigma  (e.g., 20 values)
        Profit-take levels: +0.5*sigma, +1*sigma, ..., +10*sigma (e.g., 20 values)
        Grid: 400 rule combinations.

Step 3: Generate M synthetic price paths from the estimated O-U process
        (e.g., M=100,000 paths, each of the same length as the historical series).

Step 4: For each of the 400 rules and each of the M paths, simulate the strategy
        and compute the Sharpe ratio. Each rule produces a distribution of M Sharpe
        ratios across paths.

Step 5: Select the optimal rule by comparing distributions rather than point estimates.
        Three modes:
        (a) Unconstrained: choose rule that maximizes E[SR] or median(SR).
        (b) Constrained profit-take: fix profit-take, optimize stop-loss.
        (c) Constrained stop-loss: fix stop-loss, optimize profit-take.
```

(From CanerIrfanoglu GitHub summary Ch. 13; Caner Irfanoglu Medium article AFML Part 3; confirmed with O'Reilly TOC structure.)

LdP noted in AFML that finding the closed-form optimal trading rule for the O-U process was an open problem at time of writing. A closed-form solution was subsequently found by Lipton and Lopez de Prado (2020) using heat potential methods (SSRN 3534445).

### Block bootstrap considerations for time series

LdP's synthetic path generation in Ch. 13 uses a parametric bootstrap (generating from the fitted O-U model) rather than a non-parametric block bootstrap. The parametric approach has the advantage of producing unlimited synthetic paths and allowing explicit control over the data-generating process (DGP). The limitation is model risk: conclusions are conditional on the O-U process being a good approximation of the true DGP.

For strategies not well-described by O-U (e.g., trend-following, cross-sectional momentum), a block bootstrap is more appropriate. The block length should be chosen to preserve the autocorrelation structure of the returns series; standard guidance is block length of order T^(1/3) to T^(1/4) (not from AFML; this is general bootstrap theory - flagged as secondary knowledge).

### The "trial-and-error" multiple-testing problem

The False Strategy Theorem (Bailey and Lopez de Prado 2018, SSRN 3221798; referenced in AFML Ch. 11 and 14) provides the theoretical underpinning for why any fixed SR threshold fails when N is large. Ch. 13's synthetic-data framework provides the *operational* solution: instead of trying to correct for the number of trials after the fact, the researcher evaluates all rules simultaneously against the *same* synthetic distribution and uses distributional statistics (median SR, 10th-percentile SR) rather than point estimates as the selection criterion. This transforms the multiple-testing problem from a correction problem into a design problem.

---

## Ch 14: Backtest Statistics

### Sharpe ratio standard error

The sample Sharpe ratio SR_hat is an estimator of the true SR. For IID normal returns with T observations, the variance of SR_hat is approximately:

```
Var[SR_hat] ~= (1 + SR^2 / 2) / T   (IID Normal approximation)
```

For non-normal returns, Bailey and Lopez de Prado (2012, SSRN 1821643) derived the exact asymptotic variance accounting for skewness (gamma_3) and excess kurtosis (gamma_4 - 3):

This leads directly to the PSR denominator (see below). The key insight: negative skewness and positive excess kurtosis inflate the standard error of SR_hat, meaning a given observed SR is less statistically credible when returns are fat-tailed and left-skewed, exactly the distributional features typical of hedge fund strategies.

### Probabilistic Sharpe Ratio (PSR): definition, formula, interpretation

PSR is the probability that the true (population) Sharpe ratio exceeds a benchmark SR* given the sample SR_hat and the non-normal return distribution. It is a z-test on SR_hat normalized by its estimated standard error under non-normality.

**Formula** (confirmed: mlfinlab readthedocs documentation, QuantConnect PSR article, marti.ai DSR article, all citing Bailey and Lopez de Prado 2012 SSRN 1821643):

```
PSR(SR*) = Phi(  (SR_hat - SR*) * sqrt(T - 1)
                 / sqrt(1 - gamma_3 * SR_hat + ((gamma_4 - 1) / 4) * SR_hat^2)  )
```

Where:
- Phi = standard normal cumulative distribution function (CDF)
- SR_hat = estimated (non-annualized) Sharpe ratio from the sample
- SR* = benchmark Sharpe ratio (minimum acceptable threshold; commonly 0 or the risk-free-rate-adjusted SR of a passive benchmark)
- T = number of return observations in the sample
- gamma_3 = skewness of the return series
- gamma_4 = kurtosis of the return series (not excess; this is the fourth standardized moment)

**Interpretation:** PSR(SR*) is the confidence level that the true SR exceeds SR*. PSR increases with (a) larger observed SR, (b) longer sample length, (c) positive skewness. PSR decreases with (d) fat tails (high kurtosis), (e) negative skewness.

A commonly used benchmark is SR* = 0 (testing whether the strategy beats cash), but LdP recommends setting SR* equal to the SR of a simple passive strategy in the same asset class to test whether there is genuine alpha beyond beta exposure.

### Deflated Sharpe Ratio (DSR): formula and effective number of trials

The DSR extends PSR to the multiple-testing setting. When a researcher has conducted N independent trials and reports the best one, the appropriate benchmark is not SR* = 0 but the *expected maximum Sharpe ratio that could be achieved by a purely random strategy in N trials*.

**False Strategy Theorem benchmark (SR_0)** (from marti.ai, confirmed against Wikipedia DSR article and Bailey/LdP davidhbailey.com preprint):

```
SR_0 = sqrt(V[{SR_n}]) * ((1 - gamma) * Phi_inv(1 - 1/N) + gamma * Phi_inv(1 - 1/(N * e)))
```

Where:
- V[{SR_n}] = cross-sectional variance of Sharpe ratio estimates across the N trials
- gamma = Euler-Mascheroni constant ~= 0.5772
- e = Euler's number ~= 2.718
- Phi_inv = inverse standard normal CDF
- N = number of independent strategy trials

This approximates E[max{SR_hat_n}] - E[{SR_hat_n}] under the null of zero true SR. As N grows, SR_0 grows (roughly as sqrt(2 * log(N))), so the hurdle for any individual strategy rises with the number of candidates evaluated.

**DSR formula** (from marti.ai, confirmed against davidhbailey.com preprint and Wikipedia DSR article):

```
DSR = Phi(  (SR_hat - SR_0) * sqrt(T - 1)
             / sqrt(1 - gamma_3 * SR_hat + ((gamma_4 - 1) / 4) * SR_hat^2)  )
```

DSR is numerically identical to PSR except that SR* is replaced by SR_0 (the trial-count-adjusted benchmark).

**Numerical example** (from marti.ai article, citing Bailey/LdP): A strategy with annualized SR_hat = 2.5, selected from N = 100 trials with cross-sectional SR variance V[{SR_n}] = 0.5, tested on T = 1250 daily returns, with gamma_3 = -3 (negative skewness), gamma_4 = 10 (fat tails), yields DSR ~= 0.90. This means there is a 10% probability that the strategy is purely spurious.

**Effective number of independent trials (N):** N is not simply the number of configurations tested; correlated configurations count as less than one independent trial each. LdP proposes using clustering methods (ONC, hierarchical clustering, or spectral methods on the correlation matrix of trial SR series) to estimate N. In practice, if all 400 parameter combinations from a 20x20 grid are correlated through the same underlying price series, the effective N is much smaller than 400 (Wikipedia DSR article; quantresearch.org innovations page).

### Minimum Track Record Length (MinTRL)

MinTRL answers: given a target SR, how many return observations must I collect before the PSR at SR* reaches a given significance level (e.g., 0.95)?

**Formula** (confirmed: mlfinlab readthedocs, Wikipedia DSR article, portfoliooptimizer.io PSR article):

```
MinTRL = 1 + (1 - gamma_3 * SR_hat + ((gamma_4 - 1) / 4) * SR_hat^2) * (z_alpha / (SR_hat - SR*))^2
```

Where:
- z_alpha = Phi_inv(significance level); for 95% confidence, z_alpha ~= 1.645
- SR_hat = observed (non-annualized) Sharpe ratio
- SR* = benchmark threshold (commonly 0)
- gamma_3, gamma_4 = skewness and kurtosis as above

**Interpretation:** MinTRL is measured in number of *return observations* (not calendar time). To convert to trading days, months, or years, divide by the observation frequency. A strategy reporting annualized SR_hat = 0.95 (corresponding to approximately 0.06 in daily terms at 252 trading days) with zero skewness and normal kurtosis requires approximately 3 years of daily returns to be significant at the 95% level (example from davidhbailey.com preprint).

MinTRL rises with fat tails and negative skewness and falls with higher observed SR. This means hedge fund strategies with characteristic left-skewed, fat-tailed return distributions require *longer* track records to achieve the same statistical credibility as strategies with normally distributed returns.

**Design implication:** MinTRL should be displayed in the backtest report whenever a Sharpe ratio is shown, so that analysts can immediately see whether the track record length is sufficient. A common antipattern is reporting an impressive SR from a 6-month backtest without checking whether MinTRL ~= 36 months.

### The full Ch. 14 backtest scorecard

LdP organizes backtest statistics into six categories (from CanerIrfanoglu summary and Caner Irfanoglu Medium AFML Part 3):

1. **General characteristics:** time range, average AUM, capacity, leverage, maximum position size, long/short ratio, bet frequency, average holding period, annualized turnover, correlation to underlying.

2. **Performance:** total PnL separated by long/short, annualized return, hit ratio, average profit on winning trades, average loss on losing trades.

3. **Runs and drawdowns:** maximum drawdown, maximum Time Under Water, HHI concentration of returns (see Ch. 15 / drawdown discussion).

4. **Implementation shortfall:** costs versus PnL, return on execution costs (alpha generated per dollar of transaction cost).

5. **Risk-adjusted efficiency:** Sharpe Ratio, PSR, DSR. LdP's Third Law (from multiple secondary sources citing LdP 2018): "Every backtest must be reported with all trials involved in its production."

6. **Attribution:** PnL decomposed by risk factor (duration, credit, sector, currency, macro regime) to identify whether skill is genuine or is beta exposure to a known factor.

---

## Ch 15: Understanding Strategy Risk

### The binomial bet model

Ch. 15 models a strategy as a sequence of independent binomial bets characterized by four parameters:

- n: number of bets per year (bet frequency)
- p: probability of winning each bet (precision)
- pi+: profit target per winning bet
- pi-: stop-loss per losing bet (expressed as a positive number representing the loss magnitude)

**Annualized Sharpe ratio under symmetric payouts (pi+ = pi-, called pi):**

From the model, the mean annual return is n * (2p - 1) * pi and the annual standard deviation is 2 * sqrt(n) * sqrt(p * (1-p)) * pi. The pi cancels:

```
SR_annual = (2p - 1) * sqrt(n) / (2 * sqrt(p * (1 - p)))
```

This is the key insight of Ch. 15: *under symmetric payouts, the Sharpe ratio is entirely determined by precision (p) and frequency (n).* Payout magnitude is irrelevant to risk-adjusted performance. A strategy with p=0.55 and n=252 has the same Sharpe as a strategy with p=0.55, n=252 but ten times the position size.

(From CanerIrfanoglu summary, Caner Irfanoglu Medium article, both paraphrasing AFML Ch. 15. The cancellation of pi is a standard algebraic result derivable from the model definition.)

**Under asymmetric payouts (pi+ != pi-):**

```
SR_annual = (p * pi+ - (1 - p) * pi-) * sqrt(n)
             / sqrt(p * (1 - p)) / sqrt(pi+^2 + pi-^2)  ... (approximate form)
```

The exact form from the model (CanerIrfanoglu summary): SR is a function of all four parameters {p, n, pi+, pi-} and pi no longer cancels. The chapter provides visualizations (heat maps over p vs. n, p vs. pi+/pi-) to show how each parameter affects SR.

**Design implication for pit-backtest:** The engine should support a "strategy decomposer" that, given a stream of trade outcomes, fits (p, n, pi+, pi-) and surfaces the implied SR from the model alongside the empirical SR. Discrepancy between the two reveals non-stationarity or model error.

### Capacity analysis

Capacity is defined as the maximum AUM a strategy can absorb before performance degrades due to market impact and transaction costs (CanerIrfanoglu summary; confirmed from Ch. 15 section headings). Key inputs to a capacity estimate:
- Average daily volume of the traded instruments.
- Typical position size as a fraction of ADV.
- The assumed market impact model (linear or square-root; LdP cites standard square-root impact but does not derive a specific formula in Ch. 15, this is reconstructed from the chapter's treatment as described in secondary sources).
- Turnover rate (annualized dollar traded / AUM).

A strategy with annualized turnover 500% and a capacity of $100M at 5bps of one-way slippage will degrade significantly as AUM grows, because the strategy must trade a larger fraction of ADV per day.

### Risk decomposition by source

The attribution category in the Ch. 14 scorecard (also discussed in Ch. 15 context) requires decomposing PnL into:
- Long vs. short contributions (to identify if the strategy is net-long beta).
- Time-of-day or calendar effects (to isolate implementation artifacts).
- Factor exposures: duration, credit spread, sector, currency, market-cap (for equity strategies), macro regime.

This decomposition requires the backtester to tag each trade with its factor loadings at trade entry, not just aggregate PnL. The engine must track position metadata through time.

### Bet sizing and Kelly application

LdP covers bet sizing in Ch. 10 (not Ch. 15), but the connection is established in Ch. 15 through the binomial model. The Kelly fraction for a symmetric bet is:

```
f* = p - (1 - p) = 2p - 1   (for unit-sized bets)
```

For asymmetric bets, the Kelly fraction is:

```
f* = p / pi- - (1 - p) / pi+   (standard Kelly formula; from quantbeckman.com, not directly from AFML Ch. 15)
```

LdP's position in AFML is that full Kelly is too aggressive for real strategies because the estimates of p, pi+, and pi- are uncertain. He recommends fractional Kelly (e.g., half-Kelly) to account for estimation error. The binomial model in Ch. 15 provides the framework for computing the Kelly fraction from backtest-derived parameters.

### Drawdown statistics and the Triple Penance Rule

LdP and Bailey (2013, SSRN 2201302; also SSRN 2254668) derived closed-form expressions for drawdown and Time Under Water (TuW) under serially correlated returns. The central result is the **Triple Penance Rule**:

> "Recovery from the expected maximum drawdown takes three times longer than the time required to produce it." (Bailey and Lopez de Prado 2013, summarized from quantresearch.org innovations page and SSRN 2201302 abstract)

Formally, if the time to reach the maximum drawdown is tau, the expected TuW is approximately 3 * tau. This has direct implications for stop-out rules:

- A portfolio manager with SR = 1 is expected to recover from a standard-deviation drawdown in roughly three times the drawdown duration.
- Setting stop-outs at shorter intervals than 3 * (expected time to drawdown) will fire on legitimate strategies by chance.
- Strategies with positive serial correlation (e.g., trend followers) have *shorter* penance periods than IID strategies because their returns compound more smoothly in favorable regimes.
- Strategies with negative serial correlation (mean-reverting) have *longer* penance periods and are more likely to trigger stop-out rules.

The companion paper (Bailey and LdP 2013, SSRN 2201302) provides a closed-form expression for expected maximum drawdown under first-order serial correlation. The details of that formula are not reproduced here because the original PDF was not accessible as plain text; the Triple Penance Rule qualitative result is confirmed from multiple secondary sources and the quantresearch.org innovations page.

---

## Hooks into pit-backtest design

### Ch. 11 design requirements

| Requirement | Where it lives in pit-backtest |
|---|---|
| Record trial count per dataset | Session/experiment metadata: every run must record a run_id and the dataset fingerprint; the reporting layer must aggregate trial count per dataset key. |
| Enforce hypothesis-first workflow | The engine should require a strategy spec document at instantiation, not allow parameter changes after first data touch. A "research mode" flag could allow iteration but must disable live DSR computation and flag all output as "exploratory, not reportable." |
| Report DSR alongside SR | The analytics module must compute DSR whenever N>1 trials are logged for the same dataset key. |

### Ch. 12 design requirements

| Requirement | Where it lives in pit-backtest |
|---|---|
| Label horizon metadata | Every observation in the feature matrix must carry t_start and t_end (the label horizon). The CV splitter reads these, not just the row index. |
| PurgedKFoldCV splitter | A dedicated CV class that accepts the horizon array and embargo_pct parameter. The splitter yields (train_indices, test_indices) with purging and embargo applied. |
| CPCV path generator | A CPCV class that takes N, k, and the horizon array; yields all C(N,k) splits and a path-index mapping so per-path SR can be computed. |
| Embargo as engine-level concept | Embargo is not a data-preprocessing step; it must be enforced by the splitter at CV time, using the observation timestamps, so it works correctly across different bar types (tick, volume, dollar bars). |
| SR distribution output | The backtesting layer must return a distribution of SRs (one per path), not a single SR. The reporting layer consumes this distribution. |

### Ch. 13 design requirements

| Requirement | Where it lives in pit-backtest |
|---|---|
| Synthetic path generator | A pluggable DGP interface: O-U process as the default; block bootstrap as an alternative. Takes estimated parameters and returns M synthetic price paths of length T. |
| Rule grid executor | Given a rule grid (stop-loss, profit-take combinations), the engine must simulate all rules against all synthetic paths efficiently (vectorized). This is a separate execution path from the event-driven live/backtest engine; it does not need tick-level realism but does need speed. |
| Distributional selection criterion | The rule selection layer must accept a selection function (e.g., median SR, 10th-percentile SR, mean SR) to allow researchers to specify their risk tolerance explicitly. |
| Multiple-testing documentation | The synthetic backtesting report must display the full rule grid SR heatmap, not just the selected rule, so that the researcher can see the selection context. |

### Ch. 14 design requirements

| Requirement | Where it lives in pit-backtest |
|---|---|
| PSR calculator | analytics.sharpe.psr(sr_hat, sr_star, T, gamma3, gamma4) -> float. Requires the full returns series (not just SR) to compute moments. |
| DSR calculator | analytics.sharpe.dsr(sr_hat, T, gamma3, gamma4, v_sr, N) -> float. Requires the SR variance across trials and the effective trial count N. The trial registry provides V[{SR_n}] and N. |
| MinTRL calculator | analytics.sharpe.min_trl(sr_hat, sr_star, z_alpha, gamma3, gamma4) -> int. Should be included in every report that shows a Sharpe ratio. |
| HHI concentration metric | analytics.concentration.hhi(returns) -> float. Returns 0 for perfectly distributed PnL, 1 for single-trade alpha. |
| Attribution tagging | Trade objects must carry factor tags. The analytics layer computes PnL grouped by factor tag. This requires a factor model interface plugged into the position management layer. |
| Full scorecard output | The reporting layer generates all six Ch. 14 categories for every completed backtest. The scorecard is the required output format; individual statistics are not reported in isolation. |

### Ch. 15 design requirements

| Requirement | Where it lives in pit-backtest |
|---|---|
| Binomial bet decomposer | analytics.strategy.fit_binomial(trade_outcomes) -> (p, n, pi_plus, pi_minus, sr_model, sr_empirical). Should flag discrepancy as a model-mismatch warning. |
| Capacity estimator | analytics.capacity.estimate(strategy, adv_by_instrument, impact_model) -> max_aum. Pluggable impact model (linear, square-root). |
| Drawdown and TuW tracking | The position management layer must track high-water mark continuously and compute drawdown and TuW series. These are not post-hoc calculations; they feed into real-time risk monitoring. |
| MaxDD and TuW in scorecard | analytics.drawdown.max_dd(equity_curve) and analytics.drawdown.max_tuw(equity_curve) are required scorecard fields. |
| Kelly fraction display | analytics.position_sizing.kelly(p, pi_plus, pi_minus) -> f_star. The report should show full-Kelly and half-Kelly for reference, with a note that full-Kelly is not recommended due to estimation error in p. |

### Where is the embargo logic?

The embargo belongs in the CV splitter layer, not in data preprocessing. Preprocessing the data to add an embargo flag would bleed information about the embargo decision into the data objects seen by the model, creating subtle bugs when the embargo length changes. The correct design is: the splitter knows the embargo length (in units of observation timestamps), and it emits train/test index arrays with embargo already excluded.

### How is multiple-testing correction surfaced to the user?

The engine maintains a trial registry: a persistent store keyed by (dataset_fingerprint, strategy_family). Every backtest run writes its SR, T, gamma3, gamma4, and a run timestamp to the registry. When the user requests a DSR report, the registry computes V[{SR_n}] and N (either raw trial count or estimated effective N via clustering) and displays DSR alongside SR. The report header states: "N=47 trials recorded for this dataset; SR_0=1.24; DSR=0.72 (strategy passes DSR>0.5 threshold but does not pass DSR>0.95)."

### How do we make "this Sharpe is overfit" structurally obvious?

Design rule: **no backtest output may display a raw Sharpe ratio without also displaying PSR, DSR, and MinTRL**. If MinTRL > actual T, the display renders the SR in amber with the label "insufficient track record." If DSR < 0.50, the display renders the SR in red with the label "likely spurious at N=[trial count] trials." The trial count is not optional; it is computed from the trial registry automatically.

---

## Open questions

1. **Effective N estimation.** The DSR formula requires N (effective number of independent trials). LdP proposes clustering methods, but the specific algorithm for computing N from correlated SR estimates is described in detail in companion papers (SSRN 3221798) rather than AFML Ch. 14 directly. The architecture ADR needs to decide whether to implement ONC, use raw trial count (conservative lower bound on deflation, actually gives a harder hurdle), or use a user-supplied N. The correct choice depends on whether researchers systematically track all configurations they evaluated.

2. **Embargo length selection.** LdP gives 5% as an illustrative embargo fraction but does not provide a principled derivation for choosing the embargo length. The correct length is related to the autocorrelation decay time of the return series, which is strategy-specific. The architecture ADR should specify a default (e.g., 5%) and a mechanism for researchers to override it with a data-driven estimate.

3. **CPCV path construction algorithm.** The Wikipedia and secondary sources describe CPCV at a high level but the exact algorithm for mapping C(N,k) test-set combinations onto phi(N,k) non-overlapping paths is not specified in available text. The CanerIrfanoglu summary describes the concept ("combine test sets from different splits into ordered paths") without pseudocode. The original AFML Ch. 12 or the SSRN companion paper (SSRN 4778909 referenced in Scribd) likely has the exact construction; this needs to be verified before implementing the path generator.

4. **Non-IID SR variance.** The PSR/DSR formula assumes the SR estimator's variance is well-approximated by the Bailey-Lopez de Prado (2012) formula, which was derived for large samples. For short track records (T < 60 monthly returns), the chi-squared correction may be material. The architecture ADR should specify the minimum T below which PSR/DSR are reported as "unreliable estimate."

5. **Capacity model.** Ch. 15 introduces capacity conceptually but does not derive a specific formula tying AUM to performance degradation. The ADR needs to specify the market impact model. Square-root impact (Kyle lambda) is the standard assumption in the literature, but LdP's specific model for capacity in AFML Ch. 15 could not be confirmed from available text.

6. **Triple Penance Rule stop-out calibration.** The original Bailey-LdP paper (SSRN 2201302) provides closed-form expressions for TuW under serial correlation, but the full formula was not accessible in plain text during this research session. Before implementing stop-out logic in the engine, the exact formula from SSRN 2201302 should be read and implemented (or the O'Reilly chapter previewed). The qualitative result (3x penance) is confirmed but the formula for the serial-correlation correction is not.

7. **Synthetic DGP scope.** Ch. 13 focuses on the O-U process for mean-reverting strategies. The architecture ADR should decide whether to support alternative DGPs (GBM for trend-following, GARCH for volatility strategies, Hawkes process for order-flow strategies) as pluggable modules, or limit the initial release to the O-U case and block bootstrap as fallback.

---

## Sources

1. Lopez de Prado, Marcos. *Advances in Financial Machine Learning*. Wiley, 2018. Primary source for all chapter attributions. ISBN 9781119482086. Not directly accessible in full text; chapter structure confirmed via https://www.oreilly.com/library/view/advances-in-financial/9781119482086/c11.xhtml and O'Reilly TOC pages for Ch. 11, 12, 13.

2. Bailey, David H., and Marcos Lopez de Prado. "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality." *Journal of Portfolio Management* 40, no. 5 (2014): 94-107. Preprint: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf. Primary source for DSR, PSR, and MinTRL formulas.

3. Bailey, David H., and Marcos Lopez de Prado. "The Sharpe Ratio Efficient Frontier." *Journal of Risk* 15, no. 2 (2012): 3-44. SSRN 1821643: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643. Primary source for PSR derivation.

4. Bailey, David H., Jonathan M. Borwein, Marcos Lopez de Prado, and Qiji Jim Zhu. "The Probability of Backtest Overfitting." *Journal of Computational Finance* 20, no. 4 (2017). SSRN 2326253: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253. Primary source for PBO and CSCV methodology.

5. Bailey, David H., and Marcos Lopez de Prado. "Stop-Outs Under Serial Correlation and the Triple Penance Rule." *Journal of Risk* 18, no. 2 (2016). SSRN 2201302: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2201302. Primary source for Triple Penance Rule and TuW formulas.

6. Bailey, David H., and Marcos Lopez de Prado. "The False Strategy Theorem: A Financial Application of Experimental Mathematics." SSRN 3221798: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3221798. Primary source for the SR_0 benchmark formula.

7. Lopez de Prado, Marcos. "Deflating the Sharpe Ratio." SSRN 2465675: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2465675. Companion paper to the DSR paper; focuses on the MinTRL derivation.

8. Wikipedia. "Purged cross-validation." https://en.wikipedia.org/wiki/Purged_cross-validation. Secondary source for purged k-fold and CPCV algorithm and formulas. Algorithm confirmed against AFML Ch. 7 citations within the article.

9. Wikipedia. "Deflated Sharpe ratio." https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio. Secondary source for DSR formula, False Strategy Theorem SR_0, and effective N estimation. Formulas confirmed against davidhbailey.com preprint.

10. Lopez de Prado, Marcos. "Innovations in Financial Research" (quantresearch.org publications list). https://www.quantresearch.org/Innovations.htm. Authoritative publication list with SSRN links for all papers. Used to verify paper titles, abstract IDs, and fields of contribution.

11. Irfanoglu, Caner. "AFML Part 3: Backtesting" (Medium). https://medium.com/@caneradilirfanoglu/advances-in-financial-machine-learning-part-3-backtesting-a9d70f0832c2. Secondary summary of AFML Chs. 11-15. Used for Ch. 13 O-U framework and Ch. 15 binomial model derivations.

12. Irfanoglu, Caner. GitHub repository: advances_in_ml, chapter summaries. https://github.com/CanerIrfanoglu/advances_in_ml. Chapter-by-chapter summaries of AFML, including Chs. 11-15.

13. Marti, Gautier. "How to detect false strategies? The Deflated Sharpe Ratio." https://marti.ai/qfin/2018/05/30/deflated-sharpe-ratio.html. Secondary source with explicit formulas for DSR, SR_0, and numerical example. Formulas confirmed against davidhbailey.com preprint.

14. mlfinlab readthedocs documentation (random-docs mirror). "Backtest Statistics." https://random-docs.readthedocs.io/en/latest/implementations/backtest_statistics.html. Secondary source confirming PSR, DSR, MinTRL, and HHI formulas with variable definitions.

15. Palomar, Daniel P. *Portfolio Optimization* (online textbook), section 8.3: The Dangers of Backtesting. https://portfoliooptimizationbook.com/book/8.3-dangers-backtesting.html. Secondary source for Ch. 11 taxonomy; explicitly cites LdP 2018a.

16. Beckman, Quant. "With Code: Combinatorial Purged Cross-Validation for Optimization." https://www.quantbeckman.com/p/with-code-combinatorial-purged-cross. Secondary source for CPCV implementation pattern, purge/embargo intuition, and PSR-based parameter selection.

17. QuantConnect. "Probabilistic Sharpe Ratio" (research article). https://www.quantconnect.com/research/17112/probabilistic-sharpe-ratio/. Secondary source for PSR formula and interpretation, with worked numerical examples.

18. Lipton, Alex, and Marcos Lopez de Prado. "A Closed-Form Solution for Optimal Mean-Reverting Trading Strategies." SSRN 3534445: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3534445. Resolves the open problem in AFML Ch. 13 regarding the closed-form optimal O-U trading rule.
