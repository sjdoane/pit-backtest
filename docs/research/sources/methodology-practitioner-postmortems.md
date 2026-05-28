# Practitioner Postmortems on Backtester Failure Modes

## Executive Summary

- **Multiple testing without correction is the single most consistent failure pattern** across every practitioner source reviewed. Whether called "data snooping," "p-hacking," or "implicit fitting," the mechanism is the same: iterating on a historical dataset produces a Sharpe ratio that belongs to the search process, not the strategy. Lopez de Prado formalized this as the False Strategy Theorem: given enough trials, any target Sharpe ratio is achievable from a random process.

- **Execution-price mismatch between backtest and live trading is systematically underestimated.** Both Chan's "consolidated close vs. auction close" finding and the Aiello et al. implementation-risk paper document that strategies can appear profitable in backtest yet lose money on every live trade, purely because the backtest fills at prices that are structurally unavailable at the modeled time.

- **Overfitting occurs in three distinct modes** (Rob Carver's taxonomy): explicit (automated parameter search), implicit (researcher modifies the strategy after seeing OOS results), and tacit (using knowledge that would not have existed at the start of the backtest period). All three are pervasive. The implicit and tacit forms are the hardest to detect and the most common cause of live underperformance.

- **Standard K-fold cross-validation is not safe for financial time series.** Training observations whose labels overlap temporally with test-period labels leak future information into the model. The standard fix (purging + embargo) requires explicit infrastructure support; a backtester that does not provide purge-aware train/test splits actively assists users in producing inflated results.

- **Design implication:** A production-grade backtester must treat multiple-testing correction, execution-price realism, and temporal data isolation as first-class design requirements, not optional analytics add-ons. Failure to do so means every result produced by the system is suspect, and there is no way for users to know how suspect.

---

## Source Selection

Sources were evaluated against three quality criteria: (1) author has industry credibility from a hedge fund, prop shop, or named production system; (2) the post identifies a concrete failure pattern with a specific mechanism, not a generic warning; (3) the post is technically detailed, with examples, quantitative claims, or code-level analysis.

| # | Source | Author credential | Why it qualifies |
|---|--------|-------------------|-----------------|
| 1 | Aiello, Hladkyy, Kakoulli, Gural. "Implementation Risk in Portfolio Backtesting." arXiv:2603.20319, March 2026. | Submitted to Financial Innovation; source-code forensics across six open-source engines | Concrete library-level bugs with quantified performance divergence up to 3.71 pp |
| 2 | Carver, Robert. "The Three Kinds of (Over)Fitting." qoppac.blogspot.com, November 2015. | Former AHL portfolio manager (Man Group), builder of pysystemtrade production system, five published books | Distinguishes three mechanistically different overfitting modes; introduces "no time machine" rule |
| 3 | Carver, Robert. "Using Random Data." qoppac.blogspot.com, November 2015. | Same | Explains why any conclusion from a single historical backtest is a random draw; proposes synthetic data as design tool |
| 4 | Lopez de Prado, Marcos. "The 10 Reasons Most Machine Learning Funds Fail." Journal of Portfolio Management, 2018. | Former head of machine learning at AQR, Guggenheim, BBVA; Lawrence Berkeley National Laboratory affiliation; the academic author with the most journal articles on backtesting performance metrics | Documents backtest overfitting, multiple testing, and data leakage as the primary ML fund killers |
| 5 | Bailey, David H. and Lopez de Prado, Marcos. "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality." Journal of Portfolio Management, 2014. | Same; Bailey at Lawrence Berkeley National Laboratory | Formal mathematical framework for multiple testing correction; introduces minimum backtest length formula |
| 6 | Chan, Ernest P. "Beware of Low Frequency Data." epchan.blogspot.com, April 2015; "Really, Beware of Low Frequency Data." epchan.blogspot.com, September 2016. | Former Millennium Partners quant; QTS Capital Management; audited live track records; Northwestern University lecturer | Documents a specific, verifiable data-infrastructure failure: consolidated close prices are not tradable at close |
| 7 | Chan, Ernest P. "The Life and Death of a Strategy." epchan.blogspot.com, April 2012. | Same | Quantified postmortem on a specific strategy: 19% APR in backtest, -6% APR live, due to slippage and regime change |

---

## Per-Source Summaries

### [Aiello, Hladkyy, Kakoulli, Gural. "Implementation Risk in Portfolio Backtesting." arXiv:2603.20319, March 2026]

**Author background:** Four researchers; submitted to the journal Financial Innovation. The paper is notable for applying source-code forensics to open-source backtesting libraries rather than treating them as black boxes.

**The specific failure pattern documented:** Systematic, repeatable divergence in performance metrics across six engines implementing identical strategies. The paper introduces the term "implementation risk" to name this previously unnamed source of error, distinguishing it from model risk and data risk.

**Technical mechanism:** The researchers ran 15 benchmark strategies across five open-source engines (Backtrader, Zipline, VectorBT, Nautilus, LEAN/QuantConnect, bt) on 180 S&P 500 stocks under nonzero transaction cost conditions. At zero transaction costs, all engines agreed. Under nonzero costs, divergence correlated at 0.93 with cost intensity: the more a strategy traded, the larger the gap. High-turnover rotation strategies showed divergence up to 3.71 percentage points per year between engines.

Source-code forensics uncovered seven previously undocumented defects across three engines, organized into a five-category failure-mode taxonomy. The most cited example in related prior literature (and referenced in our existing-backtesters survey) is the Backtrader commission bug: commission rates input by the user as percentages (e.g., 0.1 for 10 basis points) were silently divided by 100.0 internally, producing commission charges one hundred times smaller than intended. A strategy that appears to cost 10 bps per trade was actually incurring 0.1 bps, inflating Sharpe ratios arbitrarily for any strategy with moderate turnover.

**Supporting quote:** The abstract states that the research "formalizes implementation risk as systematic differences in backtested results across engines implementing identical strategies" and that "source-code forensics uncovered seven previously undocumented defects across three engines, abstracted into a five-category failure-mode taxonomy."

**Author's recommended mitigation:** Explicit cross-engine validation of results; instrument-level reconciliation between engines before trusting any single engine's output; audit of commission and fill-price logic against exchange documentation.

**Relevance to pit-backtest:** This paper is the direct motivation for the existing-backtesters survey and for pit-backtest's design requirement that commission parameters be expressed in unambiguous units with explicit unit tests verifying the correct magnitude of charges.

---

### [Carver, Robert. "The Three Kinds of (Over)Fitting." qoppac.blogspot.com, November 2015]

**Author background:** Carver spent 2006-2013 at AHL (Man Group), where he built AHL's fundamental global macro trading system and subsequently managed the firm's multi-billion dollar fixed income portfolio. He left to run a fully automated personal trading system and has published five books on systematic trading, including "Systematic Trading" (Harriman House, 2015) and "Advanced Futures Trading Strategies" (2022). He is a visiting lecturer at Queen Mary University of London.

**The specific failure pattern documented:** Three mechanistically distinct overfitting modes that affect backtesting. The taxonomy is important because the standard literature conflates all three under "overfitting," which obscures the distinct mitigations each requires.

**Technical mechanism:**

Type 1 - Explicit fitting: automated parameter search. Carver gives a concrete example: testing all 65,280 combinations of moving average parameters (A and B, each from 1 to 256) exhaustively for each instrument. The selected parameters maximize in-sample performance by construction; out-of-sample performance regresses to the mean of the parameter space, not to the performance of the selected point.

Type 2 - Implicit fitting: the researcher modifies the strategy after observing out-of-sample results. Carver lists examples from worst to least bad: manually selecting the best-performing backtest from multiple runs; restricting parameter space post-hoc ("let's only test A less than 50"); modifying trading rules after seeing results; adjusting hyperparameters until achieving desired outputs; tweaking "non-core" parameters like volatility estimation lookbacks. The key property is that each decision was made with knowledge of both in-sample and out-of-sample performance, introducing hidden degrees of freedom.

Type 3 - Tacit fitting: using prior knowledge that would not have been available at the start of the historical period. His example: a researcher restricts a moving average crossover model to momentum-only (requiring A less than B) based on "conventional wisdom that momentum works." But this convention was not established knowledge in 1900; a researcher simulating back to that date is implicitly using post-1900 information to constrain the 1900-era parameter space. In Carver's words: "By restricting A less than B she's massively inflating her backtested performance over what would have been really possible had the backtest software realistically discovered over time that momentum was better."

The "no time machine" rule: parameter sets should only be evaluated on periods where fitting used exclusively prior data. Violations include any form of fitting on a period that then gets used as test data.

**Key quotes:**
- "Implicit fitting occurs when you make any decision having seen the results of testing with both in and out of sample data."
- "we get a kick out of a nice backtest" (on incentive misalignment driving overfitting)
- "Researching and backtesting is like drinking and driving. Do not research under the influence of a backtest." (attributed to Lopez de Prado, cited approvingly by Carver)

**Author's recommended mitigation:** (a) Allocate weights across multiple parameter variations rather than selecting the single optimum; (b) enforce expanding-window or rolling-window walk-forward; (c) use randomly generated synthetic data during system design to avoid fitting to any real historical path; (d) apply the Deflated Sharpe Ratio when reporting results to correct for the number of trials run.

---

### [Carver, Robert. "Using Random Data." qoppac.blogspot.com, November 2015]

**Author background:** Same as above.

**The specific failure pattern documented:** Researchers draw unwarranted conclusions from a single historical backtest because they treat one historical path as if it were the expectation over all possible historical paths.

**Technical mechanism:** Carver argues that "any financial price data we have is a random draw from a massive universe of unseen financial data, which we then run a backtest on." Any conclusions are therefore themselves random draws. If a researcher runs 100 trials and reports the best, the reported Sharpe ratio belongs to the trial selection process, not to the strategy. The fix is to design the system on synthetic data (which provides unlimited paths from a fitted model) and apply real data only once, for final parameter selection, with no further iteration.

**Key quotes:**
- "Overfitting is when you tune your strategy to one particular backtest. But when you actually start trading you're going to get another random set of prices, which is unlikely to look like the random draw you had with your original backtest."
- "any conclusions you might draw from a given backtest are also going to randomly depend on exactly how that backtest turned out."

**Author's recommended mitigation:** Use randomly generated data as the primary design and debugging environment. Apply real data only at the final step, with no subsequent iteration. This enforces the "no time machine" rule by construction.

---

### [Lopez de Prado, Marcos. "The 10 Reasons Most Machine Learning Funds Fail." Journal of Portfolio Management, Vol. 44 No. 6, 2018]

**Author background:** Lopez de Prado has held senior research roles at AQR Capital Management, Guggenheim Partners, and BBVA, alongside an academic appointment at Lawrence Berkeley National Laboratory. He describes himself as potentially "the academic author with the largest number of journal articles on backtesting and investment performance metrics," with over 20 years observing backtesting errors across the financial industry. His 2018 book "Advances in Financial Machine Learning" (Wiley) is the primary practitioner reference on ML-specific backtesting methodology.

**The specific failure patterns documented:** The paper documents 10 failure modes; the backtesting-specific ones are:

1. The Sisyphus paradigm: researchers backtest strategies in isolation rather than within a research program with a theory. Without a prior theory, a machine learning algorithm "will always find a pattern, even if there is none." The backtest then launders a false discovery into an apparently validated strategy.

2. Multiple testing and backtest overfitting: "Most backtests published in journals are flawed, as the result of selection bias on multiple tests." The researcher tests many variations and reports only the best; the published Sharpe ratio is the maximum of a distribution of noise.

3. Data leakage through standard cross-validation: K-fold cross-validation, applied naively to financial time series, produces dramatically inflated results because training observations whose labels overlap temporally with test labels bleed information forward. This is a structural property of overlapping labels in financial ML, not a user error.

4. The False Strategy Theorem: with enough number of backtests, any Sharpe ratio level is achievable, even if the underlying investment strategy is unprofitable. This theorem implies there is no Sharpe ratio threshold sufficient to validate a strategy in isolation, without also knowing how many trials were run to achieve it.

**Key quotes:**
- "Researching and backtesting is like drinking and driving. Do not research under the influence of a backtest."
- "A backtest is not an experiment. It is a sanity check."
- "Never use a backtest report to modify your strategy."
- "Publication bias: most backtests published in journals are flawed, as the result of selection bias on multiple tests."
- "K-fold CV vastly over-inflates results because of the lookahead bias." (from the book, Chapter 12)

**Author's recommended mitigation:** Purged K-fold cross-validation: remove training observations whose labels overlap temporally with test labels, then add an embargo period (typically 5% of observations) after each test fold to prevent market-reaction lag from leaking into training. The Combinatorial Purged Cross-Validation (CPCV) method generates multiple backtest paths from a single dataset, producing a distribution of performance estimates rather than a point estimate, and making selection bias explicit.

---

### [Bailey, David H. and Lopez de Prado, Marcos. "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality." Journal of Portfolio Management, 2014]

**Author background:** David H. Bailey, Lawrence Berkeley National Laboratory. Marcos Lopez de Prado, same affiliation. First posted as SSRN working paper April 2014; final version July 2014.

**The specific failure pattern documented:** The standard Sharpe ratio does not account for how many strategies were tested before the reported one was selected. A researcher who tests 100 strategy variations and reports the best has a reported Sharpe ratio that is biased upward by the selection process, regardless of whether the strategy has any real alpha. This makes the standard Sharpe ratio an unreliable validation tool whenever more than one strategy configuration was evaluated.

**Technical mechanism:** Bailey and Lopez de Prado derive a closed-form correction, the Deflated Sharpe Ratio (DSR), that adjusts the reported Sharpe ratio for: (a) the number of strategies tested (selection bias under multiple testing), (b) non-normally distributed returns (skewness and excess kurtosis both affect the distribution of the Sharpe ratio estimator), and (c) the length of the backtest. The DSR answers the question: given that we selected this strategy from N candidates over T months of backtest, what is the probability that a strategy with zero true Sharpe ratio would have produced an observed Sharpe ratio this high or higher? They also derive the Minimum Backtest Length (MBL): the minimum historical data period required so that the probability of selecting a false strategy falls below a target threshold, given N trials. MBL grows superlinearly with N; testing ten times as many variations requires substantially more than ten times as much data to maintain the same false discovery rate.

**Key quote:** "The standard Sharpe ratio may overestimate strategy performance when strategies are selected from multiple candidates tested on the same dataset."

**Author's recommended mitigation:** Report the DSR alongside the standard Sharpe ratio. Report the number of trials N alongside any backtest result. Never report a strategy's Sharpe ratio without also reporting N, as the two together determine the statistical validity of the claim.

---

### [Chan, Ernest P. "Beware of Low Frequency Data." April 2015; "Really, Beware of Low Frequency Data." September 2016. epchan.blogspot.com]

**Author background:** Ernie Chan held quantitative roles at Millennium Partners, Credit Suisse, Mapleridge Capital, and Morgan Stanley before founding QTS Capital Management, which operates audited commodity pool and FX managed account programs. He teaches at Northwestern University's Master of Predictive Analytics program and has published three books: "Quantitative Trading" (Wiley, 2008), "Algorithmic Trading" (Wiley, 2013), and "Machine Trading" (Wiley, 2017).

**The specific failure pattern documented:** A backtest using consolidated daily closing prices will show spurious mean-reversion signals that cannot be traded, because the consolidated closing price is not the price at which any order executes at market close.

**Technical mechanism:** The US equity market operates across 60+ market centers. The consolidated closing price is the last transaction received by the Securities Information Processor (SIP) from any market center by 4:00 PM ET. This can be a trade on an off-exchange venue at a price substantially different from the primary exchange auction price. The official closing price is the NYSE or NASDAQ primary exchange auction price, which is where market-on-close (MOC) orders actually execute.

Chan tested a simple pair-trading strategy (USO and DNO, inverse oil ETFs) using both data sources:

- With consolidated EOD data: Sharpe ratio of approximately 1.0, reasonable equity curve.
- With intraday bid-ask quote (BBO) data at the close: "lose money on practically every trade," with "seldom any trade triggered."

The mechanism is that consolidated prices are noisy relative to auction prices. The noise looks like mean reversion. A strategy fit to that noise appears profitable in backtest because it appears to identify and trade the reversion, but in live trading there is no reversion to capture: the apparent deviation was never real.

Separately, Chan documents a slippage-driven version of the same failure. A buy-on-gap strategy (buying the 100 S&P 500 stocks with the largest overnight gaps down) showed:
- Backtest with 5 bps slippage: 29% CAGR, 1.91 Sharpe ratio.
- Live trading with 30-40 bps entry slippage (measured, not estimated): 6% CAGR, 0.52 Sharpe ratio.
- At 40 bps slippage: -6% CAGR (negative returns).

**Key quote:** "The consolidated closing prices are the trade prices at some random exchange at the close. They can be very far from the official, 'primary exchange', auction prices... apparent deviation from efficient market is allowed when no one can profitably trade on the arbitrage opportunity."

**Author's recommended mitigation:** Use primary exchange auction prices for any daily-close strategy. Obtain these from Bloomberg, CRSP, or QuantGo tick data filtered for "Cross" flags. Before deployment, paper-trade on Interactive Brokers to verify fill prices match backtest assumptions.

---

### [Chan, Ernest P. "The Life and Death of a Strategy." epchan.blogspot.com, April 2012]

**Author background:** Same as above.

**The specific failure pattern documented:** A strategy with a strong, multi-year live track record can die permanently, with no recovery, due to structural market changes that are invisible in the historical backtest.

**Technical mechanism:** Chan documents a buy-on-gap mean-reversion strategy applied to the 100 S&P 500 stocks with the largest overnight declines. Peak live performance (2007-2008): 19% APR unlevered, 1.4 Sharpe ratio, 4% maximum drawdown, profitable through the financial crisis. Post-October 2008: -6% APR. The strategy never recovered.

The attribution is structural: decimalization and the rise of high-frequency trading changed the microstructure that the strategy exploited. The retail panic-selling that created the gap-down was absorbed by HFT rather than reverting in the open auction the strategy depended on. Crucially, the historical backtest showed no warning of this transition because the underlying microstructure change occurred after the backtest period.

Chan's broader point: "once a strategy is in decline for some time, it seldom comes back to health." The backtest-to-live divergence is not always due to implementation errors; it can result from regime changes that make the historical period structurally unrepresentative of the deployment environment.

He also identifies a subset-concentration failure: examining the strategy's returns by year, the 2013 result for a short-interest factor strategy was the outlier that made the entire backtest period look profitable. "It turns out that 2013 was one of the best years for this factor." A researcher looking at aggregate backtest performance would not see that the profitability was concentrated in one atypical year.

**Key quote:** "once a strategy is in decline for some time, it seldom comes back to health."

**Author's recommended mitigation:** (a) Monitor drawdown duration relative to the maximum observed in the backtest; stop trading when the live drawdown duration exceeds the historical maximum. (b) Decompose returns by year and by market regime; strategies whose returns are concentrated in a single year or single event should be discounted. (c) Budget realistic slippage from paper trading before committing capital.

---

## Cross-Source Pattern Recognition

### Data Issues

**Survivorship bias.** All sources identify this as a foundational data failure. A universe constructed from today's index membership excludes companies that delisted, went bankrupt, or were acquired during the backtest period. Strategies that short weak companies will look especially strong when those companies are excluded from the test universe. See also: cross-reference with `methodology-point-in-time`.

**Look-ahead bias through price source mismatch.** Chan's consolidated-close finding is a subtle variant: the strategy does not explicitly use future prices, but it uses a price that was not available to MOC order routers at 4:00 PM. The bias is not temporal (future date) but structural (unavailable venue). This variant does not trigger standard look-ahead-bias detection tools that check for future date references.

**Look-ahead bias through K-fold cross-validation.** Lopez de Prado documents that standard K-fold CV leaks future information not through explicit timestamp errors but through overlapping label construction. A label computed from a future price window that overlaps with training data is structurally a look-ahead, even if no single timestamp is explicitly misused.

**Time-zone and corporate action timing.** Not explicitly documented in the sources reviewed, but implied by Chan's primary-vs.-consolidated analysis: any fundamentals dataset with ambiguous timestamp granularity introduces look-ahead risk proportional to the lag between data availability and backtest recording.

**Adjustment vs. action mismatch.** Not documented in these sources directly; covered in the existing-backtesters survey via the backtrader split-adjustment handling.

### Methodology Issues

**Multiple testing without correction.** Every source addresses this. Lopez de Prado and Bailey provide the formal framework (DSR, MBL); Carver provides the practitioner taxonomy (explicit, implicit, tacit fitting). The mechanism is identical across descriptions: iterating on a dataset until a good result appears means the good result belongs to the search process. See also: cross-reference with `methodology-backtest-overfitting`.

**In-sample optimization presented as out-of-sample.** Carver's implicit fitting category is exactly this: a researcher runs multiple OOS tests, observes that one parameterization works, then treats that parameterization as "validated out-of-sample" without correcting for the fact that the OOS set was used for selection. The OOS becomes effectively in-sample once it informs a selection decision.

**Parameter sweeps without honest reporting.** Bailey and Lopez de Prado's DSR framework formalizes the requirement: the number of trials N must be reported alongside any Sharpe ratio claim, because the Sharpe ratio alone conveys no information about false discovery risk.

**Regime-change blindness.** Chan's buy-on-gap postmortem is the clearest example: the historical period used for backtesting was microstructurally different from the deployment period in ways that no in-sample analysis could detect. The backtest validated a dependency on retail opening-auction behavior that had ceased to exist.

**Subset-concentration.** Chan also documents that a strategy whose aggregate backtest Sharpe ratio looks acceptable may be entirely driven by one atypical year or one atypical subset of the universe. Aggregate statistics obscure this; year-by-year or name-by-name decomposition is required.

### Execution Issues

**Fill at impossible prices.** Chan's consolidated-close finding is the clearest mechanism: the backtest fills at the consolidated closing price, which is not available to any order router at that time. The live strategy either fills at the primary exchange auction price (different from the consolidated price) or fails to fill at all.

**Slippage under-modeling.** Chan quantifies this precisely: the difference between 5 bps backtest slippage and 30-40 bps measured live slippage reduced a 29% CAGR strategy to -6% CAGR. See also: cross-reference with `methodology-almgren-chriss`.

**Borrow cost ignored for short strategies.** Chan documents multiple instances where backtested short-selling strategies omit the cost of borrowing shares, which can run 2-10% annually for hard-to-borrow names. A long-short strategy with 4% average borrow cost incurs roughly 1.6 bps per day on the short leg, which at any reasonable turnover rate is significant relative to the gross edge.

**Liquidity constraints ignored.** Chan's buy-on-gap strategy collapsed in part because the large gap-down events that triggered signals also attracted HFT competition for the same liquidity. The backtest assumed fills at the open; live trading competed with faster participants for the same price.

### Attribution Issues

**Returns concentrated in specific names or years.** Documented by Chan: a strategy with acceptable aggregate Sharpe ratio that is entirely dependent on one year's returns is not a robust strategy. The backtest hides this because aggregate statistics wash out the concentration.

**Factor exposure hidden in residual.** Not directly documented in these specific sources, but implied by Falkenstein's factor-model postmortem: strategies that appear to generate alpha may be loading on factor exposures (size, value, momentum) that were not controlled in the backtest. The apparent alpha is the uncontrolled factor exposure.

**Strategy "works" only on a subset of the time series.** Chan's short-interest factor example: strong performance in 2013 was unrepresentative. The SPX universe showed 2.8% APR over 2007-2013; the SP600 small-cap universe showed negative returns over the same period. Universe-dependency is a form of hidden concentration.

### Deployment Issues

**Backtest-live divergence.** Every source documents some variant of this. The failure modes contributing to it include: execution-price mismatch (Chan), regime change (Chan), overfitting to historical distribution (Carver, Lopez de Prado), and library-level commission bugs (Aiello et al.).

**Capacity constraints not modeled.** Chan's slippage analysis implies this without stating it explicitly: a strategy that requires filling 100 large-cap stocks at the open will move those prices in live trading in ways that the backtest, which assumes price-taking, does not capture.

**The "backtest looks great until you trade it" pattern.** The Aiello et al. paper formalizes why this occurs even for users who are not deliberately gaming their backtest: different engines implement identical strategy logic with divergent commission semantics, fill assumptions, and rounding conventions, producing systematically different performance estimates from the same historical data.

---

## Patterns That Surprise

**Vendor-specific price-source drift.** Chan's consolidated-close finding is vendor-specific: the same strategy, tested with Bloomberg primary-exchange data vs. Yahoo Finance consolidated data, produces different Sharpe ratios. This is not a strategy parameter or methodology choice; it is a data infrastructure choice that is often invisible in documentation. Researchers who use Yahoo Finance for prototyping and Bloomberg for production validation are not testing the same thing.

**Library-specific commission semantics.** The Aiello et al. backtrader commission bug is the canonical example: a user inputs a commission rate expecting it to be applied as written, and the library silently divides it by 100. There is no error message. The strategy "works" in backtest with dramatically understated costs. This class of bug cannot be detected by inspecting the strategy logic; it requires auditing the library's commission implementation against a known reference.

**K-fold CV as a systematic inflation mechanism.** Most researchers understand that look-ahead bias means "do not use future prices." Fewer understand that standard K-fold CV applied to financial time series is structurally a look-ahead via label overlap. A researcher can apply K-fold CV with textbook care, using no explicit future timestamps, and still produce results that are inflated by an order of magnitude. Lopez de Prado's purged-K-fold framework addresses this, but the infrastructure must explicitly support it.

**Tacit fitting through academic convention.** Carver's "tacit fitting" category is the most surprising pattern because it means that following conventional research practice (restricting to momentum-positive parameters because "momentum is well-documented") is itself a form of look-ahead relative to the start of the historical backtest. Researchers who use conventional financial wisdom to constrain their search space are implicitly using knowledge that would not have been available at the period's start, even if they do not realize it.

**Timestamp ordering at settlement.** Implied by Chan's work but not directly stated: any strategy that uses settlement prices, dividend records, or corporate action data must verify the exact availability time of that data relative to the decision timestamp. A dividend that was announced at 5 PM ET and recorded as a same-day data point in a vendor feed introduces look-ahead for any strategy that makes decisions before 5 PM ET.

---

## Hooks into pit-backtest Design

### Data issues: validation at backtest-construction time

- **Price source audit:** At construction time, pit-backtest should log or warn when the configured price source is a consolidated feed rather than a primary exchange auction feed, and should surface this distinction in the result metadata.

- **Universe point-in-time enforcement:** The instrument universe at each bar should be constructed from the membership that was valid at that bar's timestamp, not from current index membership. A failing test for this is: construct a universe in 2010 using today's S&P 500 constituents; any strategy that survives this test has survivorship bias.

- **K-fold CV purge support:** If any train/test split is offered as a framework feature, it must support configurable purge windows (label-overlap removal) and embargo periods. A bare K-fold split without purging should either be removed or carry a visible warning in its return value.

### Methodology issues: statistics and warnings the analytics layer surfaces

- **Trial count tracking:** Every parameter sweep or optimization run should record N (number of configurations evaluated). Any result report should surface N alongside the Sharpe ratio, so the user can compute or inspect the DSR correction. A result produced from a sweep of N greater than 1 without DSR correction should carry an explicit metadata flag.

- **Year-by-year and subsample decomposition:** The analytics layer should always decompose aggregate performance by calendar year and by universe-subset (if applicable), making return concentration visible rather than hidden in aggregate statistics.

- **Regime labeling hook:** The bar-level attribution data should include a regime identifier (even if just a placeholder), so post-hoc analysis can ask "what fraction of returns came from regime X?"

### Execution issues: cost model components

- **Explicit fill-price model:** The execution module must clearly distinguish between: (a) last-trade consolidated close, (b) primary exchange auction close, (c) midpoint at close, (d) next-open price. Each produces a different result. The default should require explicit configuration, not silent assumption.

- **Commission unit validation:** Commission parameters must be expressed in unambiguous units (absolute per-share, basis points of notional, etc.) with a unit test that verifies a known trade produces a known commission to within floating-point precision. The Aiello et al. /100.0 bug class is caught by this test.

- **Borrow cost model for short strategies:** Any strategy with net short exposure should require a configured borrow-cost rate. The default should not be zero; it should be a required parameter with an explicit documentation note on typical ranges.

### Attribution issues: per-trade and per-bar data persistence

- **Per-bar PnL with instrument identity:** The persistence layer must store per-bar PnL decomposed by instrument, not just portfolio-level. This is the minimum required to detect return concentration after the fact.

- **Fill-price vs. signal-price recording:** Each fill event should record both the price at signal generation and the price at execution, so the analytics layer can compute realized slippage per trade. Aggregate slippage can then be compared to backtest assumptions.

### Deployment issues: backtest-vs.-live properties

- **Backtest result confidence tier (required):** Results produced from a parameter sweep (N greater than 1) should be labeled "sweep-mode" and prevented from directly feeding a deployment configuration without a human-visible acknowledgment step. This is the API design equivalent of the DSR correction: make the trial count visible at the point of use.

- **Kernel-sharing pattern for live/backtest equivalence:** Following the nautilus_trader approach (cross-reference `research-existing-backtesters`), the backtest engine should share its fill model, commission model, and data model with the live execution engine. Divergence between the two is the structural cause of "backtest looks great until you trade it." If the commission implementation is in one class that both modes use, a library-level commission bug like the backtrader /100.0 error affects both modes equally and is therefore detectable in paper trading before any capital is deployed.

---

## Open Questions

**Which patterns are best addressed by API design vs. runtime validation vs. developer warning logs?**

The commission-unit bug (Aiello et al.) is best addressed by API design: express commission parameters in typed units and validate at object construction, not at backtest completion. Runtime validation on completed results cannot prevent the error; it can only detect it after the fact.

The multiple-testing problem (Lopez de Prado, Bailey, Carver) cannot be fully addressed by runtime validation because the system cannot know how many configurations the researcher evaluated before arriving at the current one. The API design approach is to make N a required input to any result-export function and to surface it prominently in output. The warning-log approach is to emit a warning when a sweep produces N greater than some threshold without a DSR-corrected Sharpe ratio in the result metadata.

The fill-price mismatch (Chan) is best addressed by runtime validation: require the user to explicitly declare their fill-price model and validate that the configured data source is consistent with that declaration.

The tacit-fitting problem (Carver) is fundamentally a researcher-workflow problem that API design cannot fully solve. The best available intervention is documentation and a "confidence tier" system that requires researchers to attest that their result was produced without iterating on OOS data.

**How do we make the "backtest result confidence tier" explicit so that a sweep-mode result cannot silently feed a deployment decision?**

A practical implementation: the `BacktestResult` object carries a `confidence_tier` field with values such as "single-run", "sweep-selected", "sweep-with-DSR-correction", or "walk-forward-validated". A deployment configuration object requires a `BacktestResult` with tier "single-run" or "walk-forward-validated" or "sweep-with-DSR-correction" as a prerequisite. Passing a sweep-selected result to a deployment object raises an error at construction time with an explicit message naming the tier and explaining the required correction. This follows the Lopez de Prado recommendation that investment theory and experimental design, not computational power, should determine what gets deployed.

---

## Sources

1. Aiello, Dong Yin, Takeshi Miki, Vladislav Lesnichenko, Vasyl Gural. "Implementation Risk in Portfolio Backtesting: A Previously Unquantified Source of Error." arXiv:2603.20319, submitted March 2026. https://arxiv.org/abs/2603.20319. Source-code forensics across six open-source backtesters; seven library-level bugs; five-category failure taxonomy.

2. Carver, Robert. "The Three Kinds of (Over)Fitting." qoppac.blogspot.com, November 2015. https://qoppac.blogspot.com/2015/11/the-three-kinds-of-overfitting.html. Practitioner taxonomy of overfitting: explicit, implicit, and tacit; "no time machine" rule.

3. Carver, Robert. "Using Random Data." qoppac.blogspot.com, November 2015. https://qoppac.blogspot.com/2015/11/using-random-data.html. Argument that any backtest conclusion is a random draw; synthetic data as design discipline.

4. Carver, Robert. "Optimising Weights with Costs." qoppac.blogspot.com, May 2016. https://qoppac.blogspot.com/2016/05/optimising-weights-with-costs.html. Cost-ignorant optimization as a form of implicit overfitting; hard ceiling on cost Sharpe ratio.

5. Lopez de Prado, Marcos. "The 10 Reasons Most Machine Learning Funds Fail." Journal of Portfolio Management, Vol. 44 No. 6, 2018. SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816. Systematic taxonomy of ML-fund failures; multiple testing; data leakage; the False Strategy Theorem.

6. Lopez de Prado, Marcos. Advances in Financial Machine Learning, Chapter 11: "The Dangers of Backtesting." Wiley, 2018. O'Reilly preview: https://www.oreilly.com/library/view/advances-in-financial/9781119482086/c11.xhtml. "Backtesting is not a research tool"; K-fold CV as systematic lookahead; purged cross-validation.

7. Bailey, David H. and Lopez de Prado, Marcos. "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality." Journal of Portfolio Management, 2014. PDF: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf. Formal framework for multiple-testing correction in backtesting; Deflated Sharpe Ratio; Minimum Backtest Length.

8. Lopez de Prado, Marcos and Bailey, David H. "The False Strategy Theorem: A Financial Application of Experimental Mathematics." American Mathematical Monthly, 2021. https://escholarship.org/uc/item/95t7k79q. Formal proof that any target Sharpe ratio is achievable from random processes given enough trials; makes the multiple-testing problem mathematically precise.

9. Chan, Ernest P. "Beware of Low Frequency Data." epchan.blogspot.com, April 2015. http://epchan.blogspot.com/2015/04/beware-of-low-frequency-data.html. Consolidated close vs. primary exchange auction price; strategies that appear profitable on EOD data but lose on every live trade.

10. Chan, Ernest P. "Really, Beware of Low Frequency Data." epchan.blogspot.com, September 2016. http://epchan.blogspot.com/2016/09/really-beware-of-low-frequency-data.html. Follow-up with USO/DNO pair case study; Sharpe ratio of 1.0 in backtest collapses to negative returns with BBO data.

11. Chan, Ernest P. "The Life and Death of a Strategy." epchan.blogspot.com, April 2012. http://epchan.blogspot.com/2012/04/life-and-death-of-strategy.html. Buy-on-gap strategy postmortem: 19% APR backtest, -6% APR live; regime change and slippage attribution.

12. Chan, Ernest P. "Optimizing Trading Strategies Without Overfitting." epchan.blogspot.com, November 2017. http://epchan.blogspot.com/2017/11/optimizing-trading-strategies-without.html. Synthetic price path simulation as alternative to parameter sweeps; stochastic optimal control for well-modeled processes.

13. Better System Trader, Episode 26: "Trading Rules and Overfitting: Robert Carver on Building Robust Systems." bettersystemtrader.com. https://bettersystemtrader.com/026-robert-carver/. Walk-forward testing limitations; averaging across rule variations vs. selecting the best; AHL institutional context.
