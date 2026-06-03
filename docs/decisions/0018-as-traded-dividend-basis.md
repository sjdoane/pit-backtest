# ADR 0018: as-traded dividend basis correction

Status: Accepted.
Date: 2026-06-02.
Authors: Sam Doane (with Plan + senior-quant-PM Plan-reviewer passes captured below).

## Context

The shipped M5 momentum study and the M3 survivorship study both reconstruct
total return from Sharadar's SEP `closeunadj` (the as-traded price) plus the
ACTIONS `dividend` stream. A follow-up to PR [#49](https://github.com/sjdoane/pit-backtest/pull/49)
(the survivorship split-adjustment fix) set out to clean a residual where a few
delisted/bankrupt names (Chesapeake CHKAQ, Windstream WINMQ, Frontier FTRCQ,
Nabors NBR) showed reinvested terminal total returns of 79x to 255x even though
their split-adjusted price had collapsed to ~0.

The first diagnosis attributed this to corrupt vendor dividend values and
proposed a "phantom dividend" screen. The Plan-reviewer rejected that diagnosis.
The actual cause, confirmed on the real `sharadar_2026-05-31` bundle, is a
SHARE-BASIS MISMATCH:

- Sharadar's ACTIONS `dividend value` is SPLIT-ADJUSTED to the data-pull date
  (the same cumulative-split basis as the adjusted `close`), NOT the cash
  actually paid per share on the ex-date. There is no unadjusted dividend column
  in the bundle.
- `closeunadj` is the as-traded price.

Dividing one by the other (or, equivalently, feeding the stored value into
`split_adjusted_prices_and_dividends`, which then divides it by the in-window
split factor a SECOND time) mismatches the basis by the product of the later
split ratios. For a reverse-split name this is catastrophic; for a forward-split
name it silently UNDER-credits dividends; for a name with no splits it is exact.

Empirical proof (value times the product of strictly-later split ratios recovers
the real historical dividend):

| name  | stored value | later split(s)         | as-traded decode | reality |
|-------|--------------|------------------------|------------------|---------|
| CHKAQ | 17.50        | 1-for-200 (0.005)      | **0.0875**       | Chesapeake's 2012-15 quarterly |
| WINMQ | 0.75         | reverse (0.20)         | **0.15**         | Windstream's quarterly |
| FTRCQ | 1.575        | 1-for-15 (0.0667)      | **0.105**        | Frontier's quarterly |
| NBR   | 3.00         | 1-for-50 (0.02)        | **0.06**         | Nabors' quarterly |
| GE    | 2.48 (2009)  | 1-for-8 (0.125)        | **0.31**         | GE's 2009 dividend |
| AAPL  | 0.1089 (2013)| 7:1 then 4:1           | **3.05**         | Apple's pre-split dividend |
| JNJ   | (no splits)  | none                   | unchanged        | exact already |

The contamination is not confined to the survivorship study. The momentum
backtest is a TOTAL-RETURN backtest: it credits dividends as cash
(`bar_loop.py`, `self._state.cash += shares * div`) and the signal scores 12-1
total return. Reading the back-adjusted value against `closeunadj` inflated the
12-1 momentum score of CHK / WIN / FTR to +200% to +1700% (vs price-only -70% to
+90%) at essentially every rebalance during their S&P 500 membership (2005-2018),
placing them in the top-quintile long book, AND credited 8x to 200x too much
dividend cash while they were held. The shipped M5 headline numbers (DSR 0.9713,
annualized return 6.86%, the equity curve, bootstrap, and by-year figures) were
therefore contaminated.

## Decision

Correct the dividend basis at the DATA LAYER. A new pure helper
`data/adjustments.py::to_as_traded_dividends(dividends, splits)` multiplies each
dividend's stored value by the product of the SAME TICKER's split-like ratios
whose ex-date is STRICTLY AFTER the dividend ex-date, recovering the as-traded
cash paid per share. `SharadarDataSource` computes this once over the full
ACTIONS table (a cached `_as_traded_dividend_values`) and exposes it through:

- `read_actions_dividends` (consumed by the survivorship study and the BarLoop
  dividend preload / cash-crediting), and
- `actions_with_as_traded_dividends` (the LazyFrame `BarLoop._build_pit_view`
  serves as the momentum signal's `actions` PitView).

`split_adjusted_prices_and_dividends` and `reconstruct_total_return` are UNCHANGED:
they now receive as-traded dividends and co-adjust them with the as-traded price
by the in-window split factor, which is correct for both the full-history
survivorship window and the 12-month momentum window.

Locked decisions:

1. **Decode at the data layer, before any available_dt slice.** The decode needs
   the full split history (including splits after a given bar); the PitView's
   `date < bar_dt` slice hides those. So the decode runs at the source on the
   full ACTIONS table and the sliced view inherits as-traded values. This
   placement is load-bearing: a refactor that moved the decode after the slice
   would silently re-introduce the bug for any dividend whose decoding split is
   after the rebalance.

2. **Point-in-time safety.** Using a future split to decode is NOT a leak. The
   as-traded amount is a contemporaneous fact (the cash actually paid that day),
   fixed regardless of any later split; the future split is only the key that
   inverts the vendor's back-adjusted storage. The reinvestment yield D/P at each
   ex-date is invariant to future splits. This is distinct from reading the
   back-adjusted `close`, which restates the price LEVEL in a return ratio (a
   genuine leak the momentum signal already avoids).

3. **Split-like actions = `{split, adrratiosplit}`** (`SPLIT_LIKE_ACTIONS`). The
   only `adrratiosplit` rows in the bundle are Signet (SIG): a 2004 split 3.0 +
   adrratiosplit 0.333 (net 1.0) and a 2008 split 0.5 + adrratiosplit 0.1 (net
   1-for-20). Excluding the adrratiosplit would mis-handle SIG's price AND
   dividend basis by 10x, so both the decode and the price-side split reads
   (momentum signal, survivorship study) use this set.

4. **Same-day split is excluded from the decode (strict `>`).** Mirrors the
   price-side `dt < ex_date` convention in `split_adjusted_prices_and_dividends`
   so the decode and the split adjustment compose. Verified against Transocean's
   2007 GlobalSantaFe merger: the stored 47.21 on the post-split `closeunadj`
   (~$140) is the correct as-traded special on that basis; the strictly-later
   convention recovers it.

5. **No per-name dividend is dropped, capped, or excluded.** This is a basis
   correction, not a data-quality screen. The phantom screen and a name-level
   reinvestment-multiple guard were both rejected (see below). Bankrupt names
   stay in the cohort at their honest near-zero price-only-plus-real-dividend
   terminal, preserving the survivorship signal.

## Rejected alternatives

- **Per-dividend implied-yield ("phantom") screen.** Rejected: the implied yield
  it gates on is itself the artifact of the basis mismatch, so it both
  false-positives on real large special dividends (Keurig-Dr Pepper's $103.75
  2018 special is a 467% single-period yield, Weyerhaeuser's REIT-conversion
  special 166%) and false-negatives on low-yield late-life records of the
  corrupt names. It also masks a fixable units bug behind a heuristic.
- **Name-level reinvestment-multiple guard** (reinvested TR / price-only TR >
  threshold). Empirically separable (corrupt 1863x to 281000x vs legit max GE
  10.6x) but a whole-history aggregate that does not generalize to the per-bar
  momentum signal or the BarLoop cash-crediting, and still a heuristic over a
  fixable bug.

## Consequences

- **Survivorship.** The bankrupt names now read terminals near zero (CHKAQ
  0.0006x, WINMQ 0.0018x, FTRCQ 0.0011x, NBR 0.096x), GE corrects from an
  inflated 27.66x to 3.62x, and forward-split survivors gain their correctly
  credited dividends (NVDA 458x -> 498x, AAPL 43x -> 49x). The two-panel cohort
  equity-curve figure (ADR 0002 line 96), deferred twice, now renders cleanly
  and SHIPS in `scripts/figures/`. The deferral footer at ADR 0002 is superseded.
  The CAGR delta moves and is reframed honestly: it is the inclusion-bias-plus-
  survivorship gap of a buy-and-hold construction (current members were selected
  for growing into the index), not the monthly-rebalanced Hsu-Hutchinson figure;
  the report adds a median terminal TR as the outlier-robust read.
- **Momentum.** The study is re-run on the corrected data; CHK / WIN / FTR drop
  out of the contaminated long book and the fake dividend cash is removed. The
  committed figure artifact and the shipped DSR / annualized-return headline are
  regenerated. The study's conclusion is structured to be honest whether or not
  the corrected DSR clears 0.95; the README / METHODOLOGY headline is reframed to
  report the realized value rather than assert it.
- **No-split names and the M1 reconciliation are byte-identical.** SPY / AGG /
  GLD have no splits, so the decode is a strict no-op and the M1 constant-weight
  1e-10 reconciliation and the 700+ existing tests are unchanged.

## Known limitation (deferred)

The BarLoop does not adjust the held SHARE COUNT on a split ex-date (the M1/M2
docstring already defers this). With the basis fix, a dividend credited between a
split ex-date and the next rebalance is multiplied against a pre-split share
count. Under the monthly-rebalanced momentum study a split mid-hold is rare (the
policy re-trades to target dollars each rebalance, resetting the count) and the
corrected study does not even hold the reverse-split names, so the residual is
immaterial here; split-aware position adjustment is a separate v1.1 change with
its own review.

## Process

Plan + senior-quant-PM Plan-reviewer per project rule 2A. The first reviewer pass
overturned the initial "phantom dividend" diagnosis (the basis-mismatch finding
above); the second pass reviewed the corrected direction (go, conditioned on the
test-fixture basis, the public DSR claim, the adrratiosplit and same-day
conventions, and the BarLoop limitation, all addressed here). A post-impl
reviewer pass runs before merge.
