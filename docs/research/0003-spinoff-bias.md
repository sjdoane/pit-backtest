# 0003: Spin-off treatment in the v1 worked studies

How spin-offs are handled in the two v1 worked studies (the JT1993 momentum study
and the S&P 500 survivorship study), the literature on why the handling matters,
and an honest real-data quantification of the resulting bias. This is the
deliverable promised by [ADR 0002](../decisions/0002-roadmap-review.md) decision
14.

This is a documentation and analysis note, NOT an engine change. Replacing the
spin-off treatment with actual share-distribution semantics is a separate v1.1
backlog item (ADR 0002 v1.1 list, "spin-offs as actual share distributions rather
than cash equivalent"); it is deliberately not done here. The numbers below come
from a one-time analysis against the `sharadar_2026-05-31` bundle.

## 1. The literature

A corporate spin-off distributes shares of a subsidiary to the parent's
shareholders. The empirical literature finds that both the spun-off units and
their parents tend to outperform size and industry benchmarks in the years after
completion:

- Cusatis, Miles, and Woolridge (1993), "Restructuring through spinoffs",
  *Journal of Financial Economics* 33(3). Spun-off firms and their parents earn
  positive abnormal returns of roughly 10 to 20 percent over the 36 months
  following the spin-off, which the authors attribute largely to subsequent
  takeover activity among the restructured firms.
- McConnell and Ovtchinnikov (2004), "Predictability of long-term spinoff
  returns", *Journal of Investment Management* 2(3). Re-examines the result with a
  longer sample and risk adjustment, and finds the spun-off-unit excess return is
  real but smaller and concentrated in a few names, while the parent effect is
  fragile: it largely disappears once a handful of outliers are excluded and
  returns are risk- and size-adjusted.

The honest reading is that the post-spin outperformance is real but benchmark- and
period-sensitive, and weakens out of sample. It is the SECOND-ORDER component of
the bias below. The FIRST-ORDER component needs no anomaly at all: it is the
mechanical fact that on a spin-off ex-date the parent's price falls by the value
distributed, and the v1 studies read that fall as a loss with nothing to offset
it.

## 2. Three treatments, and which one the studies use

A backtester can treat a spin-off three ways. They differ in how much of the
parent's total return they capture:

1. **Reality (hold the distributed shares).** The holder's wealth is preserved
   across the ex-date (the price drop is exactly offset by the new shares), and the
   spun-off shares then appreciate per section 1.
2. **Cash-equivalent (credit the distributed value as cash at the ex-date).** Wealth
   is preserved across the ex-date, but the cash earns nothing afterward, so this
   misses the subsequent appreciation. This is the treatment ADR 0002 decision 14
   anticipated and the treatment the engine's M3 per-row API implements
   (`data/sources/sharadar.py::_dispatch_action_row` dispatches `spinoff` to a
   `spinoff_cash_equivalent` CashFlow).
3. **Drop entirely (no shares, no cash).** The ex-date price fall is read as a pure
   loss.

The load-bearing finding of this note: **the v1 worked studies use treatment 3, not
treatment 2.** Neither study consumes the engine's `spinoff_cash_equivalent` path.
All three places where a study turns Sharadar ACTIONS into cash flows or price
adjustments read only `action == "dividend"` (exact string) for cash and
`SPLIT_LIKE_ACTIONS = {split, adrratiosplit}` for price:

- the momentum signal's forward total-return reconstruction
  (`signal/momentum.py::_momentum_score_for_ticker`, the `dividends` filter on
  `action == "dividend"` and the `splits` filter on `SPLIT_LIKE_ACTIONS`);
- the backtest's dividend cash-crediting (`engine/bar_loop.py`, the per-bar
  `divs_at` built from `read_actions_dividends`, which is
  `_as_traded_dividend_values` filtered to `action == "dividend"` at
  `data/sources/sharadar.py`);
- the survivorship buy-and-hold total return
  (`examples/sp500_survivorship.py::_member_tr_series`, splits via
  `read_actions(action_filter=SPLIT_LIKE_ACTIONS)`, dividends via
  `read_actions_dividends`).

A spin-off is none of those action codes (the bundle records spin-offs as
`spinoff`, `spinoffdividend`, and `spunofffrom`, and even the engine's M3 dispatch
table only handles `spinoff`, never `spinoffdividend` / `spunofffrom`). So in the
studies a spin-off is dropped: on the ex-date the parent's `closeunadj` falls
because it now trades ex the distributed value, the studies split-adjust that price
but do not spin-off-adjust it, and no compensating shares or cash arrive. The
parent's total return is understated by the full distributed value.

Treatment 3 is a STRICTLY larger negative bias than the cash-equivalent treatment
2 that decision 14 anticipated: treatment 2 understates a parent's return by the
spun-off shares' subsequent appreciation (section 1, second-order, possibly small);
treatment 3 understates it by the full distributed value PLUS that appreciation.
This note does not call the engine wrong: the `spinoff_cash_equivalent` CashFlow
exists and is correct as far as it goes. The finding is that the worked studies do
not call it.

## 3. The distributed value carries the ADR 0018 back-adjustment defect

Sharadar records two rows per clean pro-rata spin-off: a `spinoff` row whose
`value` is the share RATIO (GE distributed 0.33333 GE HealthCare shares per GE
share) and a `spinoffdividend` row whose `value` is the distributed value in
DOLLARS per parent share. That dollar value carries the exact defect
[ADR 0018](../decisions/0018-as-traded-dividend-basis.md) found in the ordinary
`dividend` value: it is back-adjusted to the data-pull date by the product of the
parent's strictly-later split ratios, NOT the as-traded value on the ex-date.

On `sharadar_2026-05-31`, 36 of the 241 distinct spin-off events (about 15 percent)
are back-adjusted this way. The clean example is GE's 2019 Wabtec spin-off: the
stored `spinoffdividend` value is 3.242, but GE did a 1-for-8 reverse split in 2021
(Sharadar ratio 0.125), so the as-traded distributed value is 3.242 times 0.125,
which is 0.405 per share. Any quantification of the bias must decode the stored
value to the as-traded basis first (via the same `to_as_traded_dividends` machinery
ADR 0018 introduced for dividends) or it overstates the affected events by up to
the later-split product. GE's 2023 GE HealthCare and 2024 GE Vernova distributions
need no decode because both post-date the 2021 split.

## 4. Worked example: General Electric

GE is the canonical case: a member of both the 2010 and the current S&P 500 (so a
survivor, held buy-and-hold across the whole window in the survivorship study), and
a serial spinner. Its spin-off-family ACTIONS rows over 2010 to 2026, with the
`closeunadj` bar before and on each ex-date:

| Child | Ex-date | Type | Pre close | Ex close | Recorded drop | As-traded value/sh | Value as % of price |
|---|---|---|---|---|---|---|---|
| Synchrony (SYF) | 2014-07-31 | exchange offer | 25.64 | 25.15 | -1.9% | n/a (ratio 0) | n/a |
| Baker Hughes (BKR) | 2017-07-05 | exchange offer | 27.45 | 27.35 | -0.4% | n/a (ratio 0) | n/a |
| Wabtec (WAB) | 2019-02-26 | spinoffdividend | 10.82 | 10.66 | -1.5% | 0.405 (decoded) | 3.7% |
| GE HealthCare (GEHC) | 2023-01-04 | spinoffdividend | 84.98 | 70.20 | -17.4% | 19.13 | 22.5% |
| GE Vernova (GEV) | 2024-04-02 | spinoffdividend | 175.36 | 136.47 | -22.2% | 34.68 | 19.8% |

The two large pro-rata distributions (GEHC and GEV) lop 22.5 and 19.8 percent of
the share value off the parent, and the study reads each as a loss. The recorded
price drop differs from the distributed value because GE itself moved on the day
(on the GEHC ex-date the price fell 17.4 percent against a 22.5 percent
distribution, i.e. GE rose about 5 percent on its own merits), which is why the
correction in section 6 uses the distributed value, not the raw price drop. The SYF
and BKR events are not pro-rata distributions: they are split-off exchange offers
(holders tendered GE shares for subsidiary shares), recorded as `spinoff` rows with
ratio 0 and no `spinoffdividend`, and the price barely moved because non-tendering
holders kept their GE intact. They are classified here, not used in the magnitude
math.

## 5. Affected events

Spin-off-family events in the bundle, restricted to each study's universe and
window (parent matched by ticker; `spunofffrom` is the child-side mirror row and is
reported separately, not counted as a parent-side loss):

| Set | Window | spinoff rows / parents | spinoffdividend rows / parents | spunofffrom rows |
|---|---|---|---|---|
| Bundle-wide | all | 274 / 192 | 259 / 187 | 101 |
| Momentum ever-member union (930 names) | 2005-2024 | 193 / 140 | 181 / 138 | 69 |
| Survivorship PIT-2010 cohort (500 priced) | 2010-2026 | 117 / 91 | 110 / 89 | 2 |

So 89 of the 500 priced names in the PIT-2010 survivorship cohort (about 18
percent) distributed at least one pro-rata spin-off in the window, and 138 of the
930 names in the momentum union. The events are common, but as section 6 shows the
aggregate effect on the headline numbers is small because the per-name losses are
diluted across the cohort and (for the delta) largely cancel.

## 6. Honest quantification

### Per-name (large)

For a buy-and-hold holding, dropping a spin-off costs the name its distributed
value at the ex-date. The cash-equivalent floor (treatment 2) credits that value
back as cash at the ex-date and FREEZES it (it earns nothing afterward): per event,
add `W_pre * (distributed_value / pre_close)` to the recorded terminal, where
`W_pre` is the buy-and-hold total return (the holder's wealth) at the bar just
before the ex-date and `pre_close` is the parent's as-traded price there, both on
the as-traded basis of section 3. This is ADDITIVE: the frozen cash does not ride
the parent's own post-ex return. A tempting shortcut, multiplying the whole
terminal by `(ex_close + distributed_value) / ex_close`, is WRONG because it scales
the entire post-ex path and so credits the distributed value with the parent's
subsequent return; for GE, which rallied after its spins, that shortcut reports
5.99x rather than the 4.24x floor below. Treatment 1 (holding the distributed
shares, which then appreciate per section 1) is higher; this frozen-cash figure is
the floor. Events on a split ex-date are skipped (the bases align only across a
non-split day; verified, none of GE's land on one, though Aimco's 2020 spin sits
two weeks after a split, which is why the rule is needed).

On the real bundle the per-name understatement ranges from material to large:

| Name | Recorded terminal TR | Cash-equivalent floor | Understatement | Driver |
|---|---|---|---|---|
| AIV (Aimco) | 0.61x | 3.22x | ~81% | 2020 Apartment Income REIT stub-spin |
| TNL (Travel + Leisure) | 5.44x | 8.93x | ~39% | 2018 Wyndham Hotels |
| NI (NiSource) | 5.19x | 7.32x | ~29% | 2015 Columbia Pipeline |
| EBAY | 5.17x | 6.77x | ~24% | 2015 PayPal |
| GE | 3.62x | 4.24x | ~15% | 2023 GEHC + 2024 GEV (+ 2019 WAB) |
| RTX (UTX) | 3.69x | 4.31x | ~14% | 2020 Carrier + Otis (one ex-date) |

AIV is the extreme: a stub-spin where the distributed entity (Apartment Income
REIT) was worth more than the remaining parent, so dropping it turns a
value-preserving holding into an apparent near-total loss (0.61x recorded versus a
3.22x floor). GE is more typical and shows why the per-name number is not simply the
headline price drop: GE was a long laggard (its buy-and-hold wealth was still only
about 0.93x when it spun off GE HealthCare in early 2023), so the dropped value,
though 22.5 percent of the share price, was small in WEALTH terms; its 3.62x
recorded terminal is mostly the 2023 to 2026 rally that followed. The frozen-cash
floor lifts GE to about 4.24x, a roughly 15 percent understatement; treatment 1
(holding the GEHC and GE Vernova shares, which appreciated) would be higher.

### Survivorship cohort and delta (small)

Summed across the 89 affected PIT-cohort parents and divided over the 500 priced
names, the cash-equivalent floor lifts the PIT-2010 cohort mean terminal TR from
7.16x to 7.24x, raising its equal-weight CAGR from 12.749 to 12.830 percent (+8.1
bps). The current cohort (60 affected parents of 416 priced) lifts from 14.46x to
14.52x, +3.3 bps. The survivorship delta (current cohort CAGR minus PIT cohort
CAGR) therefore moves from the reported 493.6 bps to 488.8 bps, a 4.8 bps (about 1
percent) reduction.

The delta shift is small and would only shrink the headline, for two reasons.
First, a survivor like GE is in both cohorts and held buy-and-hold over the same
window in each, so its understatement is identical in both and cancels in the
difference (the residual is only the `1/N` cohort-weight difference). The net delta
effect comes from parents in exactly one cohort, two one-sided sums that largely
offset. Second, the PIT cohort starts from a lower mean (7.16x vs 14.46x), so the
same absolute per-name lift moves its CAGR more, which is why the delta narrows
rather than widens. The median terminal TR (PIT 3.70x, current 7.44x) is unchanged:
the affected parents are not the median name. The 4.8 bps sits well inside the
study's existing inclusion-bias caveat (the survivorship study already documents
that its buy-and-hold delta is dominated by current-cohort inclusion bias, not pure
delisting survivorship), so the spin-off treatment does not change any conclusion
of that study.

### Momentum study (second-order)

Dropping a spin-off makes the parent read a large negative 12-1 score for the
roughly 12 months the ex-date sits in the lookback, which pushes the parent DOWN
the cross-sectional ranking and OUT of the top-quintile long around the event. For
a long-only strategy this is a false negative (it can only wrongly exclude a name,
never fabricate a spurious winner), so it is conservative. The only realized-PnL
channel is a parent that was already top-quintile GOING INTO its spin month and
held through the one-month fabricated drop before the next rebalance dropped it.

Checking the parent's own 12-1 score at the rebalance before each of the
largest-value spin-offs in the momentum union: most rank low or negative going in
(Motorola -1%, ITT -10%, Match -13%, Aimco -39%), confirming they were not held.
A few had high momentum and could plausibly have been held through their spin
(Leidos +44%, RR Donnelley +26%, Tyco +22%, Hilton +18%). The realized impact is
bounded by that handful: each held-through-spin event costs roughly one
equal-weighted name-month (around 0.5 to 1 percent of the roughly 100-name book for
one month on a large stub-spin), and there are only a few such events across 20
years. The headline zero-cost Deflated Sharpe (0.9552) is therefore second-order
affected, not materially. This is the opposite direction from the ADR 0018
dividend-basis bug, which inflated scores and pulled names INTO the book (and moved
the DSR from a contaminated 0.9713 to 0.9552); the spin-off drop pushes names OUT,
so its footprint is smaller and conservative. A precise figure would require
re-running the roughly fifteen-minute worked study with a modified action filter
that cash-credits spin-offs; given the bound above, that is not warranted for a
second-order effect.

## 7. Conclusion

The verified treatment is honest in direction (negative bias, parent total return
understated) and worse in magnitude than ADR 0002 decision 14 anticipated (the
studies drop spin-offs entirely rather than crediting the cash equivalent). The
quantification is the point: per-name the understatement is material (about 15
percent for GE, up to roughly 80 percent for stub-spins like Aimco where most of the
value left), but in aggregate it is small (the survivorship delta would shrink about
5 bps, roughly 1 percent, and only the headline; the momentum Deflated Sharpe is
second-order affected and the ranking pollution is conservative for a long-only
book). Per-name material and aggregate small is the honest result, and it is small
for the right reasons: dilution across the cohort, cancellation across the two
cohorts in the delta, and the momentum signal ranking the affected names out rather
than holding them through the drop.

The fix that would remove the bias, distributing the actual spun-off shares rather
than dropping or cash-crediting the value, is tracked as a v1.1 backlog item and is
out of scope for this note.

## References

- Research: [`methodology-point-in-time.md`](sources/methodology-point-in-time.md);
  this note is linked from [`docs/METHODOLOGY.md`](../METHODOLOGY.md) pillar 5.
- Decision: [ADR 0002](../decisions/0002-roadmap-review.md) decision 14 (the
  requirement) and its decision-14 amendment footer (the note shipped);
  [ADR 0018](../decisions/0018-as-traded-dividend-basis.md) (the back-adjustment
  decode reused in section 3).
- Implementation (the consumption points that drop spin-offs):
  `signal/momentum.py`, `engine/bar_loop.py`,
  `examples/sp500_survivorship.py`; the unused cash-equivalent path is
  `data/sources/sharadar.py::_dispatch_action_row`.
