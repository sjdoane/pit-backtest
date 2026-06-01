# ADR 0017: Snapshot-based `SharadarSP500Universe`, date-agnostic member resolution, and the add/drop event log as a cross-check

Status: Accepted.
Date: 2026-05-31.
Authors: Sam Doane (with the M5 Plan + Plan-reviewer pass; the Plan-reviewer returned a MODIFY verdict with two Critical findings, both empirically resolved against the real bundle before this ADR was finalized).

## Context

The M3 PR 4 `SharadarSP500Universe` (`src/pit_backtest/data/universe.py`) modeled S&P 500 membership as an event log: it replayed `added`/`removed` rows into per-asset intervals and RAISED `UniverseValidationError` on any other `action` value. That model was validated only against synthetic add/remove fixtures.

The real Sharadar SP500 table, pulled in full for M5 (`data/snapshots/sharadar_2026-05-31/sp500.parquet`, 59,158 rows), has FOUR `action` types, not two:

- `historical` = 56,690 rows: a point-in-time membership SNAPSHOT at each of 113 quarter-ends (1998-03-31 through 2026-03-31), 500 to 505 members per quarter-end.
- `current` = 503 rows: the latest roster, dated 2026-05-30 (the pull date, one day after the last SEP price bar 2026-05-29).
- `added` = 1,231 rows and `removed` = 734 rows: the add/drop effective-date event log, reaching back to 1957-03-04 (far deeper than the SEP price era, which starts 2004-01-02).

The event-replay universe RAISES on the 56,690 `historical` and 503 `current` rows, so it crashes on the real bundle. More fundamentally, the replay is the wrong model: Sharadar publishes PIT membership DIRECTLY via the quarterly snapshots, so reconstructing it from the add/drop log is both harder and redundant.

This ADR reworks `SharadarSP500Universe` to read membership from the snapshots, demotes the add/drop log to a consistency cross-check, reframes the SP500 data-quality contract, and records the resolution and PIT-safety reasoning. It is the prerequisite for the M5 momentum worked study (PR 3), which needs a survivorship-bias-free PIT S&P 500 universe over 2005-2024.

### Empirical findings that shaped the design (verified against the real bundle, clean venv)

1. **PIT safety of "most-recent snapshot <= t".** Every quarter-end roster was effective on or before its snapshot date: across all 113 snapshot transitions, all 686 new joiners have an `added` effective date on or before the snapshot that first lists them, and no ticker appears in a snapshot dated before its `added` effective date. The model LAGS reality (quarterly staleness, up to about 92 days) and never LEADS it, so it cannot leak look-ahead.

2. **The resolution invariant.** All 1,155 distinct snapshot tickers (historical + current) resolve to exactly one TICKERS permaticker by ticker string: zero have no TICKERS row, zero map to more than one permaticker. There is no within-snapshot ticker reuse in this bundle.

3. **The boundary members.** Within the SEP price era (2004-2026), 7 `historical` snapshot rows sit one to five trading days outside the member's `[firstpricedate, lastpricedate]` price interval: spin-offs listed at the quarter-end snapshot just before their first regular-way bar (TDC 2007-09-30 vs firstprice 2007-10-02; NWSA 2013-06-30 vs 2013-07-01; BXLT 2015-06-30 vs 2015-07-01; FTV 2016-06-30 vs 2016-07-05), and acquisition targets whose last bar precedes the quarter-end removal (GDW 2006-09-30 vs lastprice 2006-09-29; BLS 2006-12-31 vs 2006-12-29; ANDV 2018-09-30 vs 2018-09-28). An 8th such row predates the SEP window (DI1, snapshot 1998-09-30 vs lastprice 1998-09-29), so 8 across the full snapshot history. Each resolves to exactly one permaticker; only a date-interval-contains check fails, by a few days. NWSA and FTV are current members so they additionally appear in the 2026-05-30 `current` snapshot, one day past their lastpricedate (= SEP max).

4. **Cross-check residual is zero.** Reconciling the 495 within-window `removed` and 498 within-window `added` events against the snapshot transitions, with a within-quarter offsetting-event exemption (decision 5), yields zero unexplained residuals.

5. **Consumer divergence is bounded.** Over the 240 month-ends 2005-2024, of 120,519 (member, month) observations, 220 (0.18%) are members the universe certifies but whose ticker has no tradeable price at the rebalance date (a name delisted between the quarterly snapshot and the monthly rebalance). The maximum in any month is 8. The naive "stronger reconciliation" the Plan-reviewer proposed (every `removed` ticker absent from the next snapshot; every `added` ticker present) would FALSE-FAIL on 44 added + 1 removed legitimate intra-quarter churn cases (e.g. SOLS added 2025-10-30 and removed 2025-12-22, both inside Q4; BMS a four-day 2019 membership), which is why decision 5 uses the offsetting-event exemption instead.

## Plan-reviewer findings and author responses

The Plan-reviewer (senior multi-strat quant persona) returned MODIFY. The findings and resolutions, all landed before code:

- **Critical 1 (consumer re-filter).** The sole v1 consumer (`Momentum12_1Signal`) resolves members to a tradeable ticker AT the rebalance date and silently drops names with no price there, so the universe is loud at construction but the consumer re-filters its output. ACCEPTED in part: the drop is economically correct (a long-only study cannot trade a delisted name) and `momentum.py` (merged in #39) is out of scope for this PR, but the divergence must not be silent. Resolution: decision 7 documents the consumer contract; a real-bundle test quantifies and bounds the divergence (finding 5); the M5 study (PR 3) reports the per-rebalance omission count.

- **Critical 2 (cross-check teeth).** A cross-check that only logs counts is decorative; but the reviewer's proposed stronger reconciliation false-fails on intra-quarter churn (finding 5). ACCEPTED with the correct middle design: decision 5 makes the cross-check a RAISING contract with a within-quarter offsetting-event exemption, scoped to the SEP window, residual proven zero.

- **High 3 (WIP obsolescence vs revert).** The uncommitted SEP-coverage-window refinement to the old SP500 contract is OBSOLETED (not reverted) by ticker-string resolution; the `FirstPriceWithinFiveDaysContract` half of the WIP is independent and preserved. ACCEPTED: decision 4 states this; the working-tree WIP is folded directly into the rewrite in one commit (it was never committed as an intermediate state, so there is no add-then-remove churn in history).

- **High 4 (inverting/vacuous tests).** Specific existing tests invert (the event-outside-interval test) or go vacuous (passes-on-clean-bundle on an event-only fixture). ACCEPTED: the tests are enumerated and rewritten, not silently carried; the shared `_SP500_ROWS` fixture gains `historical`/`current` rows.

- **High 5 (boundary sub-classes).** The boundary regression must cover both spin-off (before firstprice) and acquisition (after lastprice) sub-classes, including the current-snapshot-past-lastpricedate case. ACCEPTED.

- **Mediums/Low.** Acceptance band tightened to [500, 505] against this bundle's known snapshots (decision 1); spell staleness quantified (decision 3); the `is_member` frozenset sidecar documented as membership-only (decision 8); the never-raising-contract concern resolved by decision 5 making the cross-check raise; snapshot-date strict monotonicity guaranteed by dict-keyed-by-date construction (decision 1).

## Locked decisions

### Membership model

1. **`members_at(t)` returns the membership of the most-recent `historical`/`current` snapshot whose date is `<= t.date()`; before the first snapshot it returns `[]`.** Storage is a `dict[date, tuple[AssetId, ...]]` (sorted-by-int members) plus a sorted `tuple[date, ...]` for an `O(log N)` `bisect_right` as-of lookup. The 113 historical quarter-ends plus the 1 `current` date form 114 snapshot dates; `current` folds in as the latest with no special-case. Snapshot dates are dict keys (so a future bundle repeating a quarter-end MERGES rows rather than picking one arbitrarily) and the sorted tuple is therefore strictly increasing. Acceptance: `members_at("sp500", datetime(y,12,31,16,0))` returns a list of length in [500, 505] for y in {2005, 2010, 2015, 2020, 2024} (the S&P can legitimately reach about 506 during multi-class additions; the assertion binds at the observed 505 and the ADR records the headroom).

### Member resolution

2. **Snapshot member tickers resolve to an AssetId via the date-agnostic `IdentifierResolver.resolve_ticker_unique(ticker)`, NOT the date-gated `resolve_ticker(ticker, dt)`.** The date-gated form raises `TickerNotFoundError` for the 7 boundary members (finding 3). Date-agnostic resolution returns the single permaticker for the ticker string and raises `ValueError` on more than one (ticker reuse). This is safe and not silent masking BECAUSE the uniqueness it relies on is asserted at ingest by `Sp500SnapshotMembersResolveContract` (decision 4); resolution and validation share one invariant. A K-trading-day grace window around the price interval was REJECTED: clearing the 5-day spin-off boundary needs K >= 5, which is wide enough to begin masking a future genuinely-stale mapping, whereas the asserted-uniqueness line tolerates the legitimate boundary cases without admitting stale ones.

### `membership_spells` semantics

3. **A spell is a maximal run of consecutive snapshot dates in which the asset is present, bounded by the run's first and last quarter-end snapshot dates; the end is `None` only when the run includes the latest snapshot (a current member).** Spell boundaries are quarter-end dates, accurate to the containing calendar quarter; the effective-date error is 0 to about 92 days, so a spell is NOT a tradable effective-date interval. Reconciling spells to the finer `added`/`removed` effective dates is v1.1. `membership_spells` has no v1 consumer (`momentum.py` and `examples/sp500_survivorship.py` use `members_at`/`is_member`); it exists for audit and v1.1. The M3 PR 4 docstring describing spells as event-effective intervals is rewritten, not merely appended to, because it is now wrong.

### Data-quality contracts

4. **The SP500 resolution contract is reframed and renamed `Sp500SnapshotMembersResolveContract` (`sp500_snapshot_members_resolve_to_unique_ticker`).** It validates that every distinct `historical`/`current` snapshot member ticker maps to exactly one TICKERS permaticker (`n_permatickers != 1` is a violation: 0 = absent, >1 = reuse). `required_tables` is `{sp500, tickers}`. The pre-ADR-0017 date-interval-contains check and its SEP price-coverage window are intentionally RETIRED for snapshot members (ticker-string resolution sidesteps the price-window question that the window worked around); this OBSOLETES the uncommitted SP500-contract coverage-window WIP rather than reverting it. The independent `FirstPriceWithinFiveDaysContract` coverage-window refinement (skip `firstpricedate < min(SEP date)`) is KEPT verbatim.

5. **The add/drop event log is demoted to a RAISING cross-check, `Sp500AddedRemovedCrossCheckContract` (`sp500_added_removed_consistent_with_snapshots`), scoped to the SEP window with a within-quarter offsetting-event exemption.** For each event in `[min(SEP), max(SEP)]`, let S be the first snapshot on or after the event date: a `removed(ticker, d)` is consistent if the ticker is absent from S or an offsetting `added(ticker, d')` exists with `d < d' <= S`; an `added(ticker, d)` is consistent if the ticker is present in S or an offsetting `removed(ticker, d')` exists with `d < d' <= S`. Residuals (a real disagreement with no offsetting event) raise. The exemption admits legitimate intra-quarter churn without masking; the residual on the real bundle is zero (finding 4). `required_tables` is `{sp500, sep}`. This is the LOCKED meaning of "added/removed become a cross-check." `_DEFAULT_CONTRACTS` therefore grows from 6 to 7.

6. **`UniverseValidationError` survives with two failure modes: a snapshot member ticker absent from TICKERS, and a snapshot member ticker mapping to more than one permaticker.** The M3 PR 4 event-replay modes (double-add, remove-without-add, unknown-action) are retired: the snapshot model has no replay state machine, so a ticker simply appears in the snapshots that list it, and a genuinely unknown `action` is ignored by the `is_in(["historical","current"])` filter (the cross-check covers `added`/`removed`). `UniverseValidationError` remains importable; `examples/sp500_survivorship.py` keeps catching it.

### Consumer contract and internals

7. **`members_at` returns quarterly snapshot membership; a consumer that needs the ticker as traded on a specific date resolves at that date and omits members with no tradeable price there.** That omission (a name delisted between the snapshot and the rebalance) is economically necessary for a long-only study and is bounded at about 0.18% of member-month observations over 2005-2024 (finding 5). The divergence is surfaced loudly (a real-bundle test bounds it) and the M5 study reports the per-rebalance omission count; it is not hidden. `momentum.py` is not modified by this PR.

8. **`is_member` uses a per-snapshot `frozenset[AssetId]` sidecar for `O(1)` membership; the frozenset is membership-only and never iterated into output, so it does not violate Determinism Requirement 4.** `members_at` returns from the sorted tuple. The two structures share one source of truth (the per-date member set built once at construction).

## What this ADR does NOT do

- Does NOT change the `Universe` Protocol surface (`is_member`, `members_at`, `membership_spells`). The momentum signal and the survivorship example keep working unchanged.
- Does NOT modify `Momentum12_1Signal` (#39) or its as-of resolution. The consumer-divergence handling is documentation plus a bounding test plus PR-3 reporting (decision 7).
- Does NOT reconcile `membership_spells` to event-level effective dates (decision 3 defers it to v1.1).
- Does NOT add a Russell 1000 or custom universe; `members_at` keeps the `{"sp500"}` allowlist.
- Does NOT change `read_sp500`'s public 3-column (`ticker, date, action`) contract; the universe and contracts read the full table via `get_table`.

## Cross-references

- ADR 0002 dec 12 invariant 5 (SP500 event ticker resolves to exactly one TICKERS row): SUPERSEDED for snapshot members by decision 4; an append-only amendment footer is added to ADR 0002.
- ADR 0002 dec 6 / acceptance criterion 1 (Universe Protocol; survivorship demo): the Protocol surface is unchanged; an append-only footer on ADR 0003 records the v1 spell semantics.
- ADR 0001 dec 2 (surface ambiguity, do not paper over it): the resolution `ValueError` on ticker reuse and the cross-check residual both honor this.
- ADR 0001 dec 9 (no look-ahead): finding 1 is the PIT-safety argument for "most-recent snapshot <= t".
- ADR 0016 (M5 run_cpcv + bootstrap): the momentum study (PR 3) consumes this universe.
- `docs/methodology/dataset_versioning.md` (Sharadar SP500 table): updated to describe the four action types and the snapshot-primary model.
- `docs/methodology/determinism.md` Requirements 3 and 4: decision 8.

## Status

Accepted. The M5 universe-rework PR implements `resolve_ticker_unique`, the snapshot-based `SharadarSP500Universe`, the reframed `Sp500SnapshotMembersResolveContract`, the `Sp500AddedRemovedCrossCheckContract`, the test rewrites, and the real-bundle gated acceptance tests against the contract locked above. Revisiting any decision requires a superseding ADR.
