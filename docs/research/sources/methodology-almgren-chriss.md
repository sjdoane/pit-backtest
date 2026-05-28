# Almgren-Chriss and the Market-Impact Literature

*Research note for pit-backtest cost-modeling layer. Compiled 2026-05-28.*

---

## Executive Summary

- **The canonical model (Almgren-Chriss 2000)** frames optimal liquidation as a mean-variance tradeoff: liquidate too fast and market impact dominates; liquidate too slowly and price risk dominates. Under linear impact the solution is a hyperbolic-sine inventory trajectory, fully determined by a single urgency parameter kappa = sqrt(lambda * sigma^2 / eta).
- **Linear impact is empirically wrong.** Almgren et al. (2005) and the broader Bouchaud-Lillo-Farmer literature agree that temporary impact scales roughly as a 3/5 (0.6) power of participation rate rather than linearly. The square-root law (exponent 1/2 in order size) is the de-facto industry default.
- **The field has converged on three distinctions:** (1) permanent impact shifts the mid-price permanently and is independent of execution schedule; (2) temporary (instantaneous) impact is a price concession paid during execution that then reverts; (3) transient impact, formalized by Obizhaeva-Wang (2013), decays with a finite resilience half-life, which neither purely permanent nor purely instantaneous impact captures.
- **Gatheral (2010)** shows that not all impact functions are arbitrage-free: exponential decay is consistent only with linear impact, and power-law decay exponents must satisfy gamma <= 1/2 to preclude round-trip arbitrage.
- **Design implication for pit-backtest:** implement a hierarchy of cost models (NoImpact, FixedBps, LinearImpact, SquareRootImpact) where SquareRootImpact is the recommended default. Permanent impact must feed back into the mid-price series, not merely reduce fill price, otherwise multi-trade strategies are systematically mispriced.

---

## Sources Accessed

The following sources were read directly during this research session. Several primary PDFs (Almgren-Chriss 2000, Obizhaeva-Wang 2013, Gatheral 2010) returned binary-encoded content that resisted text extraction; their equations are reproduced here from derivative secondary sources and cross-checked across multiple independent expositions.

1. Almgren-Chriss 2000 PDF: `https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf` (binary, equations reconstructed from cross-sources)
2. Almgren et al. 2005 PDF: `https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf` (binary, calibration numbers from secondary search)
3. Bouchaud-Farmer-Lillo 2009 arXiv abstract: `https://arxiv.org/abs/0809.0822`
4. Obizhaeva-Wang 2013 PDF: `https://web.mit.edu/wangj/www/pap/ObizhaevaWang13.pdf` (binary)
5. Gatheral 2010 SSRN: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1292353` (403, metadata from search)
6. Gatheral propagator slides PDF: `https://pdfs.semanticscholar.org/...` (binary)
7. PrettyQuant market impact models: `https://www.prettyquant.com/post/2022-09-03-market-impact-models/` (read directly)
8. BSIC backtesting transaction cost modelling: `https://bsic.it/backtesting-series-episode-5-transaction-cost-modelling/` (read directly)
9. Anboto Labs IS deep dive: `https://medium.com/@anboto_labs/deep-dive-into-is-the-almgren-chriss-framework-be45a1bde831` (read directly)
10. Dean Markwick Almgren-Chriss walkthrough: `https://dm13450.github.io/2024/06/06/Solving-the-Almgren-Chris-Model.html` (read directly)
11. Ibrahim Adedimeji Almgren-Chriss execution article: `https://medium.com/@ibrahimlanre1890/trading-execution-algorithms-the-almgren-chriss-framework-56717dd650ce` (read directly)
12. Bouchaud square-root law substack: `https://bouchaud.substack.com/p/the-square-root-law-of-market-impact` (read directly)
13. Kyle 1985 slides: `https://www.kahnfrance.com/cmk/fin590/0326Kyle%2085%20Slides.pdf` (binary)
14. Alex Chinco two-period Kyle derivation: `https://alexchinco.com/two-period-kyle-1985-model/` (read directly)

---

## Kyle (1985) Baseline

### The Lambda Model

Albert Kyle's "Continuous Auctions and Insider Trading" (Econometrica, 53(6), 1985, pp. 1315-1335) is the conceptual ancestor of every linear impact model in use today. Kyle considers a stylized market with three types of agents: a single risk-neutral insider who observes a fundamental value v; noise traders who submit random aggregate order flow z ~ N(0, sigma_z^2); and competitive risk-neutral market makers who set prices on the combined observable order flow y = x + z, where x is the insider's demand.

The key equilibrium result is a linear pricing rule. Market makers set:

```
p = mu + lambda * y
```

where mu is the prior expectation of v and y is total signed order flow. Kyle's lambda (lambda) is:

```
lambda = sigma_v / (2 * sigma_z)
```

where sigma_v is the standard deviation of the fundamental value and sigma_z is the standard deviation of noise trader volume. This is the famous result: price impact is proportional to the ratio of information uncertainty to noise-trading volume. Market depth is 1/lambda, a thick noise-trader base or a small information advantage implies low lambda and cheap execution.

In the continuous-time limit Kyle showed that:

- All private information is gradually incorporated into prices by trading close
- Price follows a Brownian motion in equilibrium
- Market depth (1/lambda) is constant throughout the trading day
- The insider's optimal strategy is to trade at a constant rate proportional to (v - p) / (lambda * remaining time)

### Why Kyle Is the Conceptual Ancestor

Every subsequent impact model, Almgren-Chriss included, inherits the linear-in-order-flow price response intuition from Kyle. Permanent price impact in Almgren-Chriss (g(v) = gamma * v) is a direct instantiation of Kyle's lambda relationship applied to a liquidation context. The "information" interpretation of permanent impact comes directly from Kyle: trades reveal something about the trader's private view of value.

### Limitations of the Kyle Model

Kyle's model has three serious limitations as a practitioner tool:

1. **Linearity.** Impact is linear in order flow by construction. Empirical data (see Bouchaud-Lillo-Farmer below) shows clearly concave impact: doubling order size less than doubles impact. Kyle's model cannot capture this without structural modification.
2. **No transient component.** In Kyle's equilibrium, impact is fully permanent, the price response to a trade persists forever. There is no mechanism for price resilience or mean-reversion of impact. This rules out any modeling of order-book recovery.
3. **Single-instrument, single-period fundamentals.** The model assumes a terminal liquidation date and a single risky asset, making portfolio-level execution modeling awkward.

---

## Almgren-Chriss (2000): The Canonical Optimal-Execution Model

### Setup

Robert Almgren and Neil Chriss, "Optimal Execution of Portfolio Transactions," Journal of Risk, 3(2), 2000, pp. 5-39.

The trader holds X shares at time t = 0 and must reduce the position to zero by terminal time T. Trading occurs at N discrete intervals of length tau = T/N. Let x_j denote shares held at time t_j = j * tau. The trading list is n_j = x_{j-1} - x_j shares executed during interval j, so the trading rate is v_j = n_j / tau.

The mid-price follows arithmetic Brownian motion:

```
S_j = S_{j-1} + sigma * sqrt(tau) * xi_j - g(v_j) * tau
```

where xi_j are i.i.d. N(0,1) shocks, sigma is the per-unit-time volatility, and g(v_j) is the permanent impact from executing at rate v_j. The actual execution price for batch j is:

```
S_j^exec = S_j - h(v_j)
```

where h(v_j) is the temporary (instantaneous) impact.

### Impact Functions

Under the original linear specification (Almgren-Chriss 2000, Section 2):

**Permanent impact:**
```
g(v) = gamma * v
```

where gamma > 0 has units of ($/share) / (shares/time). Each infinitesimal block of shares traded at rate v permanently moves the mid-price by gamma * v per unit time.

**Temporary impact:**
```
h(v) = epsilon * sign(v) + eta * v
```

where epsilon >= 0 represents half the bid-ask spread (a fixed cost per trade direction) and eta > 0 is the price-depth coefficient for instantaneous slippage. The temporary impact does not persist: it affects only the execution price of that batch, not subsequent mid-prices.

### The Cost Functional

Implementation shortfall is the difference between the paper portfolio value (all shares at the arrival price S_0) and the actual proceeds. In the continuous-time limit, the expected shortfall and its variance are (Almgren-Chriss 2000, equations near Section 3):

**Expected cost:**
```
E[C] = (1/2) * gamma * X^2 + eta * integral_0^T v_t^2 dt
```

The first term (1/2) * gamma * X^2 is the permanent impact cost. Crucially, this is independent of the execution trajectory: no matter how you liquidate X shares, you pay gamma * X^2 / 2 in permanent impact. Only temporary impact (the second term) is trajectory-dependent.

**Variance of cost:**
```
Var[C] = sigma^2 * integral_0^T x_t^2 dt
```

The variance measures the exposure to price drift while inventory remains. It is minimized by executing as fast as possible (small x_t for all t), which conflicts with minimizing temporary impact.

**Objective (the trader's dilemma):**
```
min_{v_t} { E[C] + lambda * Var[C] }
```

where lambda >= 0 is the trader's risk aversion. When lambda = 0 the trader is risk-neutral and minimizes only expected cost; the optimal solution is uniform execution (TWAP): v* = X/T. When lambda > 0 the trader is willing to pay more in expected impact to reduce variance.

### Closed-Form Solution Under Linear Impact

The Euler-Lagrange equation for the optimization is:

```
eta * x_tt - lambda * sigma^2 * x_t = 0
```

where x_tt denotes the second time derivative. Defining:

```
kappa^2 = lambda * sigma^2 / eta
```

the general solution satisfying x(0) = X and x(T) = 0 is:

**Optimal inventory trajectory (continuous time):**
```
x*(t) = X * sinh(kappa * (T - t)) / sinh(kappa * T)
```

**Optimal trading rate (continuous time):**
```
v*(t) = X * kappa * cosh(kappa * (T - t)) / sinh(kappa * T)
```

In the discrete version (N periods of length tau), the discrete kappa satisfies:

```
kappa = arccosh(0.5 * kappa_tilde^2 * tau^2 + 1) / tau
```

where kappa_tilde^2 = lambda * sigma^2 / eta_tilde and eta_tilde = eta - 0.5 * gamma * tau is a correction term that accounts for the fact that half the permanent impact from a trade affects the next trade's mark.

The discrete optimal holdings are:

```
x_j = X * sinh(kappa * (T - t_j)) / sinh(kappa * T)
```

and the discrete trade sizes are:

```
n_j = 2 * sinh(0.5 * kappa * tau) / sinh(kappa * T) * cosh(kappa * (T - t_{j-0.5})) * X
```

### The Trader's Dilemma: Kappa Interpretation

The parameter kappa has units of inverse time; 1/kappa is the "urgency half-life." When kappa * T is large (high risk aversion, high volatility, or cheap temporary impact), the sinh ratio decays sharply and the trader front-loads execution. When kappa * T is small (low risk aversion), the trajectory is nearly linear, approaching TWAP.

Specifically:
- kappa is an increasing function of risk aversion (lambda) and volatility (sigma)
- kappa is a decreasing function of temporary impact coefficient (eta)
- The risk-neutral limit lambda = 0 gives kappa = 0 and the TWAP trajectory x*(t) = X(1 - t/T)

### Calibration: What Are Eta and Gamma in Practice?

Almgren and Chriss (2000) suggest rough calibration rules in their paper:

- **Temporary impact eta:** Trading 1% of daily volume should cause temporary impact equal to roughly one full bid-ask spread. If the daily volume is V shares and the bid-ask spread is b dollars, then eta ~ b / (0.01 * V / (T/tau)) per share.
- **Permanent impact gamma:** Trading 10% of daily volume over a day should cause permanent impact equal to roughly one full bid-ask spread. This implies gamma ~ b / (0.10 * V).

These rules are crude. Almgren et al. (2005) (see next section) replaced them with empirical regression results from 700,000 institutional orders.

### Efficient Frontier

Because permanent impact is trajectory-independent, the efficient frontier (the set of Pareto-optimal mean-variance pairs) is traced by varying lambda. Higher lambda yields strategies with lower Var[C] and higher E[C]. The frontier is convex and admits a clean parametric form in terms of the hyperbolic functions above. No strategy can simultaneously achieve minimum E[C] and minimum Var[C] under nonzero impact.

---

## Almgren et al. (2005): Empirical Calibration

Robert Almgren, Chee Thum, Emmanuel Hauptmann, and Hong Li, "Direct Estimation of Equity Market Impact," Risk magazine, 18, July 2005, pp. 57-62.

### Data and Approach

The authors analyzed approximately 700,000 US equity order executions from Citigroup institutional trading desks, covering the 19 months from December 2001 through June 2003. Orders were filtered for quality and matched to contemporaneous market data (daily volume, volatility, shares outstanding) to allow cross-sectional regression.

The central insight is that market impact should be expressed as a fraction of daily volatility, normalized by the participation rate and a measure of relative order size. The authors rejected the linear impact assumption of the original model and fit power-law exponents directly.

### Functional Forms

The total transaction cost (in basis points relative to arrival price) takes the form (Almgren et al. 2005, equation in Section 3):

```
tcost = (1/2) * gamma * sigma * (X/V) * (Theta/V)^(1/4) + eta * sigma * |X / (V*T)|^(3/5)
```

where:
- sigma = daily volatility (fraction, not percent)
- X = order size in shares
- V = average daily volume in shares
- Theta = shares outstanding (a proxy for float-adjusted turnover)
- T = execution horizon expressed as fraction of a trading day
- gamma = 0.314 (dimensionless permanent impact coefficient, calibrated by regression)
- eta = 0.142 (dimensionless temporary impact coefficient, calibrated by regression)

### Key Findings: Exponents

The critical empirical result is in the temporary impact term. The exponent on the participation rate |X/(V*T)| is 3/5 = 0.6, not 1 (linear) and not 1/2 (square root). This 3/5 power was calibrated directly from the data and was found to be more consistent with the observed range of institutional order sizes than either the linear or square-root specifications.

The permanent impact term shows a 1/4 power dependence on the turnover ratio (Theta/V), reflecting that stocks with higher float relative to daily volume experience smaller permanent impact per trade, consistent with lower information asymmetry.

### Practitioner Implications

With gamma = 0.314 and eta = 0.142 as universal constants, practitioners can compute pre-trade cost estimates for any US equity using only sigma, X, V, Theta, and T. The calibration was done on dollar-weighted returns so the constants are dimensionless and broadly applicable.

However, universality is approximate. The standard errors on gamma and eta are non-trivial (reported as gamma = 0.314 +/- 0.041 and eta = 0.142 +/- 0.006), and the model does not capture cross-sectional variation by sector, market cap tier, or liquidity regime. Production systems typically run instrument-specific recalibration on their own execution data when sample sizes allow.

---

## Bouchaud / Lillo / Farmer: The Square-Root Law

Jean-Philippe Bouchaud, J. Doyne Farmer, and Fabrizio Lillo, "How Markets Slowly Digest Changes in Supply and Demand," in *Handbook of Financial Markets: Dynamics and Evolution* (Elsevier, 2009, pp. 57-160). Preprint: arXiv:0809.0822.

### The Empirical Claim

The square-root law states that the average price impact of a metaorder (a large institutional order split into child orders) is:

```
I = Y * sigma_D * (Q / V_D)^delta
```

where:
- I = realized price impact (fraction of price)
- sigma_D = daily volatility
- Q = total metaorder size in shares
- V_D = daily volume in shares
- Y = a dimensionless constant of order unity (empirically approximately 0.5 to 1.0 across studies)
- delta = empirical exponent

The striking finding is that delta is "stubbornly anchored around 1/2" across equities, futures, FX, and fixed income in multiple studies spanning different exchanges, time periods, and countries. The special case delta = 1/2 gives the canonical square-root formula:

```
I = Y * sigma_D * sqrt(Q / V_D)
```

This should be contrasted with Kyle's linear prediction (delta = 1), which systematically overestimates impact for large orders and underestimates it for small orders.

### Universality

Bouchaud, Farmer, and Lillo document universality across:
- US and European equities (various datasets, 1990s through 2000s)
- Futures markets (S&P 500, Eurostoxx, Bund futures)
- FX markets
- Individual stocks of varying market capitalization

The exponent delta = 1/2 and the order-of-magnitude of Y appear to be robust structural features of financial markets rather than instrument-specific accidents. This is remarkable because the underlying liquidity profiles of these instruments differ substantially.

### Distinction Between Permanent and Transient Impact

The Bouchaud-Lillo-Farmer framework distinguishes two regimes:

1. **During execution (transient impact):** As a metaorder executes over time T, the price moves roughly as sqrt(Q_executed / V_D). The impact is concave in cumulative volume: each additional share has less impact than the previous because the order book partially replenishes between child orders.

2. **After execution (permanent impact):** Once execution completes, impact partially decays. Empirical studies (reviewed by Bouchaud et al.) find that permanent impact is roughly half the peak transient impact, the "square-root" relationship holds approximately for the permanent component as well, but with a smaller coefficient.

The paper emphasizes that impact is "approximately independent of N and T" (the number of child orders and the execution horizon), which is the core empirical fact that the square-root law captures. A trader liquidating Q shares over 1 hour versus over 1 day with the same total volume faces similar total impact.

### Theoretical Explanation

The latent liquidity theory (LLT) provides the most supported theoretical account: as prices move against an aggressive buyer, latent liquidity (orders sitting just off-market) flows in linearly, so resistance to continued buying increases linearly with price displacement. This linear resistance to a flow of constant rate produces sqrt(Q) total displacement, the square root emerges from integrating a linearly-increasing restoring force.

### Why Linear Impact Is Wrong

The linear Kyle-type model predicts I ~ Q. In any realistic range of institutional order sizes (0.1% to 10% of ADV), the square-root law and linear model differ by factors of 3 to 10x in per-share impact estimates. Linear impact systematically overestimates the cost of small orders and underestimates it for large orders, making risk-adjusted strategy comparisons misleading.

---

## Obizhaeva-Wang (2013): Transient Impact

Anna A. Obizhaeva and Jiang Wang, "Optimal Trading Strategy and Supply/Demand Dynamics," Journal of Financial Markets, 16(1), 2013, pp. 1-32. NBER Working Paper 11444.

### Motivation

Almgren-Chriss treats impact as either fully permanent (g(v)) or fully instantaneous (h(v)), with no intermediate timescale. In reality, a liquidity-providing market maker who absorbs a large order will partially restore the order book over the next several seconds to minutes. This resilience means that impact from an aggressive trade decays over time rather than being immediately permanent or instantly reversed.

### The Limit Order Book Model

Obizhaeva and Wang model the limit order book (LOB) as a block of liquidity with depth Q_0 per unit price distributed uniformly beyond the best bid and ask. When a buyer executes n shares, they consume Q_0 shares worth of book, and the best ask jumps by n / Q_0. The book then refills at resilience rate rho, so the displaced ask recovers exponentially:

```
spread(t + dt) = spread(t) * e^(-rho * dt) + n(t) / Q_0  (after each trade n(t))
```

This gives a price process where impact from trade at time s decays as e^{-rho * (t - s)} at later time t. The overall price at time t depends on the full history of trades:

```
S(t) = S(0) + (1/Q_0) * integral_0^t e^{-rho * (t - s)} * v(s) ds
```

This is the propagator representation: impact from each infinitesimal trade decays exponentially with the resilience parameter rho.

### Optimal Strategy

Under this block-shaped LOB with exponential resilience, Obizhaeva and Wang find that the optimal execution strategy involves **discrete trades at the start and end of the execution window**, with a uniform (TWAP-like) participation schedule in between:

- An initial discrete trade of size Delta_0 at t = 0
- Continuous uniform trading at rate v* = Q / T_effective during [0, T]
- A final discrete trade of size Delta_T at t = T

This is qualitatively different from the Almgren-Chriss purely continuous solution. The intuition is that discrete blocks allow the trader to "load up" on liquidity when impact is fresh (i.e., before the book has been depleted) and again when the final deadline forces urgent completion.

The unit cost for this strategy under the OW framework takes the form:

```
c = kappa * tau * [1 - (tau/T) * (1 - e^(-T/tau))] * (Q/V)
```

where tau = 1/rho is the resilience half-life and kappa is a cost-per-unit-liquidity constant.

### Relationship to Almgren-Chriss

Obizhaeva-Wang is a refinement of Almgren-Chriss in the following sense:

- When rho -> infinity (instantaneous resilience), the OW model reduces to the Almgren-Chriss temporary impact model.
- When rho -> 0 (no resilience), impact is fully permanent and the OW model approaches the Almgren-Chriss permanent impact component.
- At finite rho, OW captures the intermediate regime where impact is neither purely instantaneous nor purely permanent, it has a half-life of 1/rho.

For production execution systems, the OW framework implies that if a trader executes a large order in sub-intervals, the cost of each sub-interval depends on the residual impact from all previous sub-intervals. A backtester that ignores this cross-interval impact will underestimate total cost for high-frequency execution of large orders.

---

## Gatheral (2010): No-Dynamic-Arbitrage Conditions

Jim Gatheral, "No-Dynamic-Arbitrage and Market Impact," Quantitative Finance, 10(7), 2010, pp. 749-759.

### The Propagator Model

Gatheral formalizes the general class of transient impact models via a propagator G(t) >= 0. The price at time t given a history of trading rates v(s) for s in [0, t] is:

```
S(t) = S(0) + integral_0^t G(t - s) * f(v(s)) ds
```

where f(.) is the instantaneous impact function (monotonically increasing in signed volume) and G(t) is the decay kernel (propagator) that governs how impact from the past fades. Special cases include:

- G(t) = delta(t) (Dirac delta): instantaneous / temporary impact only (half of Almgren-Chriss)
- G(t) = 1 (constant): fully permanent impact (other half of Almgren-Chriss)
- G(t) = e^{-rho * t}: exponentially decaying transient impact (Obizhaeva-Wang)
- G(t) = t^{-gamma} for gamma in (0, 1): power-law decaying impact

### The No-Dynamic-Arbitrage Condition

Gatheral imposes the condition that a round-trip trade (buy some quantity then immediately sell it, or vice versa) should cost a non-negative expected amount on average. This "no-buy-sell-round-trip" condition requires that the integral of G(t) over its domain is non-negative in a precise functional-analytic sense. Specifically, the condition is equivalent to G being a positive semi-definite kernel.

This has immediate consequences for which functional forms are admissible:

1. **Exponential decay** G(t) = e^{-rho*t} is consistent with no-dynamic-arbitrage only if the instantaneous impact function f is **linear**: f(v) = c * v. Combining exponential decay with concave impact (f(v) = v^beta for beta < 1, as the empirical square-root law suggests) creates an arbitrage opportunity by buying, waiting for impact to partially decay, then selling.

2. **Power-law decay** G(t) = t^{-gamma}: the no-arbitrage condition restricts the exponent to gamma <= 1/2. Faster decay (gamma > 1/2) allows dynamic arbitrage. The boundary case gamma = 1/2 is particularly important because it is consistent with the empirically supported square-root impact function.

3. **Permanent impact** G(t) = constant: is trivially consistent with any monotone f because buy-then-sell round trips cost 2 * (permanent impact) regardless of timing.

### Practical Constraints for Implementation

Gatheral's result creates a consistency test for any proposed cost model: the pair (G, f) must jointly satisfy the no-arbitrage condition. Practitioners who combine an exponential decay kernel with empirical square-root impact (as is common in simplified production models) are technically using an arbitrage-prone model. In practice, the arbitrage is not exploitable at institutional scale, but the model can produce inconsistent pre-trade cost estimates for highly dynamic execution schedules.

---

## Practitioner Notes

### Industry Conventions

Modern institutional execution desks and algorithmic trading systems have broadly converged on square-root impact as the default cost model for pre-trade analysis. Key reasons:

1. **Universality.** The square-root law holds across asset classes, markets, and decades. Practitioners trust a model that generalizes.
2. **Concavity.** Linear models systematically over-penalize large orders in pre-trade analysis, making seemingly expensive strategies look worse than they are; concave impact corrects this.
3. **Simplicity.** The square-root formula I = Y * sigma * sqrt(Q/V) requires only three observable inputs (Y calibrated once, sigma from recent history, Q/V from order metadata).

### The 3/5 vs 1/2 Controversy

Almgren et al. (2005) find a 3/5 exponent for temporary impact as a function of the participation rate Q/(V*T), while the Bouchaud-Lillo-Farmer literature finds a 1/2 exponent for impact as a function of Q/V (without the time normalization). These are measuring slightly different things:

- The 1/2 exponent applies to **total metaorder size Q relative to ADV V** and is approximately independent of execution time T.
- The 3/5 exponent in Almgren 2005 applies to the **participation rate** (shares per unit time divided by daily volume rate), which bakes in T.

In many practical regimes (moderate participation rates of 5-30% ADV) the two formulas produce similar numerical estimates. For extreme participation rates (very fast or very slow), the two diverge. The Almgren 2005 formula is generally preferred when the execution horizon T is well-defined and controllable; the Bouchaud-Lillo formula is preferred when execution time is uncertain.

### Calibration Challenges

The calibration problem is harder than it appears:

1. **Heterogeneity across instruments.** The universal constants Y (or gamma, eta) have substantial cross-sectional dispersion. Small-cap stocks with low ADV may have Y 2-3x the S&P 500 value.
2. **Regime shifts.** Volatility regimes change the effective liquidity profile. A model calibrated on calm markets understimates costs during volatility spikes.
3. **Self-fulfilling selection bias.** Execution desks calibrate on their own trades, which are already chosen to minimize expected impact. A sample of intentionally-optimized executions underestimates the cost of naive execution, creating downward bias in the model.
4. **Permanent vs. temporary decomposition.** Separating the two empirically requires observing the mid-price both immediately after and well after each trade, a multi-hour window, which is complicated by other news and order flow in the interim.

---

## Hooks into pit-backtest Design

### Recommended Impact Model Hierarchy

Implement four levels of cost model, selectable per-run:

**Level 0: NoImpact**
Fill at the arrival mid-price. Zero slippage. Useful only for strategy logic debugging; will systematically overstate strategy PnL.

**Level 1: FixedBps**
Fill at arrival mid + fixed_bps * side_sign. Parameters: `fixed_bps` (default 5 bps). Appropriate for liquid large-cap equity strategies where the analyst wants a rough conservative adjustment without calibrating to specific instruments. No permanent impact feedback.

**Level 2: LinearImpact**
Implements the Almgren-Chriss (2000) linear model in its simplest form:
```
temporary_impact = eta * v    (dollars per share)
permanent_impact = gamma * v  (dollars per share, fed into price series)
```
where v = trade_size / (ADV * tau) is the participation rate. Parameters: `eta`, `gamma`, `epsilon` (half-spread). Requires daily ADV and execution interval tau. Appropriate when the strategy explicitly targets a known execution horizon and linear impact is an acceptable simplification.

**Level 3: SquareRootImpact (recommended default)**
Implements the empirical square-root law calibrated to Almgren et al. (2005) with optional override to the Bouchaud-Lillo exponent:

For temporary impact:
```
temp_impact_bps = eta * sigma_D * |Q / (V_D * T)|^beta
```
Default: eta = 0.142, beta = 0.6 (Almgren 2005), or beta = 0.5 (Bouchaud-Lillo).

For permanent impact:
```
perm_impact_bps = (1/2) * gamma * sigma_D * (Q / V_D) * (Theta / V_D)^(1/4)
```
Default: gamma = 0.314 (Almgren 2005).

Required calibration inputs: `sigma_D` (daily return volatility series, aligned to bar frequency), `V_D` (daily volume series), `Theta` (shares outstanding, static or rolling), configurable constants eta and gamma.

### Permanent vs. Temporary Impact: Fill Semantics

This is the most consequential design decision in the cost-modeling layer.

**Temporary impact** affects only the fill price for the specific order bar. The execution price is:
```
fill_price = mid_price - temp_impact * sign(direction)
```
(sell fills at a discount; buy fills at a premium.) This does not affect the price series visible to the strategy on subsequent bars.

**Permanent impact** must be reflected in the mid-price series from the next bar onward. If the strategy sells a large block, the permanent impact permanently lowers the instrument's mid-price, which should be visible in subsequent fills, unrealized P&L calculations, and portfolio-level risk metrics. A backtester that only adjusts fill price but not the carried mid-price will report artificially favorable subsequent fills on the same instrument, especially for strategies that trade repeatedly in the same direction.

Implementation: maintain a `permanent_impact_register` (per instrument, per bar) that accumulates permanent impacts from fills and adds them to the raw price series when simulating subsequent bar prices. This register decays to zero only if a transient impact model (OW-style) is selected; under the standard Almgren-Chriss permanent model, the displacement is permanent.

### Transient Impact Option (Obizhaeva-Wang)

For strategies that execute over multiple sequential child orders within a single day (e.g., a VWAP execution spanning 6 hours), a simple permanent/temporary split understates the cross-order cost interaction. A transient impact plugin (Level 4) should implement:

```
S(t) = S(0) + sum_{j: t_j < t} (1/Q_0) * n_j * e^(-rho * (t - t_j))
```

where rho = resilience rate (e.g., 1/hour for equities), Q_0 = book depth, n_j = shares in child order j. This requires the user to supply rho (or the half-life 1/rho in minutes) as a calibration parameter.

### Integration with Portfolio Policy

The QSTrader gap noted in the existing-backtesters research is directly relevant here: most existing backtesters compute target weights from a signal model that is oblivious to expected execution costs. The correct integration is:

1. **Pre-trade:** the portfolio optimizer should receive expected_impact(instrument, target_delta_shares) as a cost term. This allows the optimizer to prefer smaller rebalances when impact is high and larger ones when impact is low.
2. **At fill:** execute at mid_price - temp_impact, record permanent_impact, update price series.
3. **Post-trade:** report actual vs. predicted impact. Over time this data feeds model recalibration.

For pit-backtest v1, the minimum viable path is to expose a `CostModel.pre_trade_cost_estimate(instrument, shares, direction)` method that the portfolio layer can query when computing trade lists. The cost estimate should use SquareRootImpact by default.

### Default Model Recommendation

**SquareRootImpact with Almgren 2005 coefficients (eta=0.142, beta=0.6, gamma=0.314) is the recommended default for new users.** Rationale:

- Calibrated on a large, diverse institutional dataset
- Accepts only three widely-available data inputs (sigma, ADV, shares outstanding)
- Concavity is empirically correct across the institutional order size range
- Permanent/temporary decomposition is explicit, enabling correct price-series feedback
- Coefficients are universal enough to be useful without instrument-specific calibration

Users with their own execution data can override eta and gamma from regression. Users running liquid large-cap strategies with small order sizes can use FixedBps for speed without material accuracy loss.

---

## Open Questions

**Should permanent impact be part of the price series after the fill?**

Yes, for the reasons stated above. The implementation challenge is that in an event-driven backtester, bar prices typically arrive from a data feed. Permanent impact must be applied as an additive adjustment to the raw feed price before other consumers (e.g., the signal model) see the bar. This requires a "price adjustment stack" in the data layer, separate from the fill engine. Whether this adjustment should be visible to the signal model (affecting signal generation) or only to portfolio valuation (affecting P&L) is a design choice: visible adjustment is more realistic but can create circular dependencies in mean-reversion strategies.

**How does our backtester represent the half-life of transient impact?**

The transient impact module requires a per-instrument resilience parameter rho (or equivalently, half-life = ln(2)/rho). In the absence of instrument-specific calibration, the literature suggests equity order-book resilience half-lives of approximately 5-30 minutes. A sensible default is rho = 1/600 per second (10-minute half-life). This parameter should be configurable per instrument and per liquidity regime.

**Borrow costs vs. impact: how does our cost model layer split these?**

Short selling introduces borrow costs that are economically distinct from market impact. Borrow costs (expressed as annualized basis points) are a financing charge that accrues while the short position is open, independent of order size or trading rate. They should be modeled in a separate `BorrowCostModel` that integrates daily borrow rates against the short position schedule. The cost model layer should have two independent pluggable modules: `ImpactCostModel` (fills, price effects) and `CarryCostModel` (borrow, financing, dividends on shorts). These are summed at the total-cost level but should not be conflated in implementation, because they have different dependencies (trading data vs. position data) and different calibration sources (execution data vs. securities lending rates).

---

## Sources

1. Almgren, R. and Chriss, N. (2000). "Optimal Execution of Portfolio Transactions." *Journal of Risk*, 3(2), pp. 5-39. `https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf`, The original model; linear impact, mean-variance optimal trajectory, efficient frontier.

2. Almgren, R., Thum, C., Hauptmann, E. and Li, H. (2005). "Direct Estimation of Equity Market Impact." *Risk*, 18, pp. 57-62. `https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf`, Empirical calibration on 700k Citigroup orders; gamma=0.314, eta=0.142; 3/5 exponent for temporary impact.

3. Bouchaud, J.-P., Farmer, J.D. and Lillo, F. (2009). "How Markets Slowly Digest Changes in Supply and Demand." In *Handbook of Financial Markets: Dynamics and Evolution*, Elsevier, pp. 57-160. `https://arxiv.org/abs/0809.0822`, Square-root impact law; universality across markets; delta approximately 1/2.

4. Obizhaeva, A.A. and Wang, J. (2013). "Optimal Trading Strategy and Supply/Demand Dynamics." *Journal of Financial Markets*, 16(1), pp. 1-32. `https://web.mit.edu/wangj/www/pap/ObizhaevaWang13.pdf`, Transient impact via block-shaped LOB; exponential resilience; discrete-plus-continuous optimal strategy.

5. Kyle, A.S. (1985). "Continuous Auctions and Insider Trading." *Econometrica*, 53(6), pp. 1315-1335. `https://www.econometricsociety.org/publications/econometrica/1985/11/01/continuous-auctions-and-insider-trading`, Linear price impact lambda = sigma_v / (2*sigma_z); conceptual ancestor of all modern impact models.

6. Gatheral, J. (2010). "No-Dynamic-Arbitrage and Market Impact." *Quantitative Finance*, 10(7), pp. 749-759. `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1292353`, Propagator model; conditions impact functions must satisfy; exponential decay requires linear impact; power-law exponent <= 1/2.

7. PrettyQuant (2022). "Market Impact Models." `https://www.prettyquant.com/post/2022-09-03-market-impact-models/`, Practitioner overview with Almgren 2005 implementation and Kissell comparison; includes Python code.

8. BSIC (2022). "Backtesting Series Episode 5: Transaction Cost Modelling." `https://bsic.it/backtesting-series-episode-5-transaction-cost-modelling/`, Survey of slippage models in backtesting; Obizhaeva-Wang vs Almgren cost formulas; practical calibration advice.

9. Anboto Labs (2023). "Deep Dive into IS: The Almgren-Chriss Framework." `https://medium.com/@anboto_labs/deep-dive-into-is-the-almgren-chriss-framework-be45a1bde831`, Detailed practitioner exposition of the cost functional and kappa parameter.

10. Bouchaud, J.-P. (2024). "The Square-Root Law of Market Impact." Substack. `https://bouchaud.substack.com/p/the-square-root-law-of-market-impact`, Accessible review of the empirical evidence for delta=1/2 and the latent liquidity theory.

11. Markwick, D. (2024). "Solving the Almgren-Chriss Model." `https://dm13450.github.io/2024/06/06/Solving-the-Almgren-Chris-Model.html`, Step-by-step mathematical derivation of the optimal trajectory with full notation.

12. Chinco, A. "Two-Period Kyle (1985) Model." `https://alexchinco.com/two-period-kyle-1985-model/`, Discrete exposition of Kyle's equilibrium; lambda derivation from covariance/variance of order flow.

13. Almgren, R. (2008). "Algorithmic Trading and Market Microstructure." *Encyclopedia of Quantitative Finance*. `https://www.smallake.kr/wp-content/uploads/2016/03/eqf.pdf`, Concise overview of Almgren's impact models including 3/5 exponent and universal coefficients.

14. Adedimeji, I.L. (2023). "Trading Execution Algorithms: The Almgren-Chriss Framework." `https://medium.com/@ibrahimlanre1890/trading-execution-algorithms-the-almgren-chriss-framework-56717dd650ce`, Clear derivation of E[IS], Var[IS], and optimal inventory trajectory.
