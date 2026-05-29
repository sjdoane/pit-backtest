# Dataset versioning

Status: locked for M1.
ADR cross-references: ADR 0001 decision 10 (v1 data inventory); ADR 0002 decision 3 (Sharadar pull SHA256 commitment).
Audience: implementers of the Sharadar adapters; anyone trying to reproduce a backtest result months after it was first produced.

## Goal

Every backtest result in this repo must be reproducible. The reproducibility surface includes:

- The code that produced the result (pinned by the git commit SHA).
- The Python environment that ran the code (pinned by `uv.lock`).
- The input data the code consumed (pinned by the SHA256 of each parquet file).

This document specifies the third item: how raw data from Sharadar and SSGA enters the repo, how it is named, how it is hashed, and how the hash commitment makes a SPY reconciliation result run today bit-identical to the same reconciliation run six months later.

## V1 data inventory

The v1 inventory is fixed by ADR 0001 decision 10. The sources, their roles, and their pull procedures are:

| Source | Sharadar table | Role | Update cadence |
|---|---|---|---|
| Sharadar SEP | SEP | Daily OHLCV with raw close (`closeunadj`) plus a back-adjusted `close`; per-bar `lastupdated` for as-of joins | Daily; vendor updates with one-day lag |
| Sharadar ACTIONS | ACTIONS | Corporate events: dividends (per-share cash), splits (ratio), spin-offs, transfers, mergers. The dividend rows are the source of truth for the M1 SPY TR reconstruction (per docs/methodology/total_return_reconstruction.md the engine uses `closeunadj` + ACTIONS dividends, not SEP's back-adjusted `close`). | Event-driven; vendor updates same-day for U.S. equities |
| Sharadar SF1 ARQ | SF1 (filtered to `dimension == "ARQ"`) | Point-in-time fundamentals (revenue, earnings, book value, shares outstanding) as originally reported | Quarterly per filing; vendor updates within 24h of SEC submission |
| Sharadar TICKERS | TICKERS | Identifier history: `permaticker`, ticker, CUSIP, first/last price dates, delisting metadata | Daily |
| Sharadar SP500 | SP500 | S&P 500 membership event log: add/drop events with effective dates | Event-driven; vendor updates within days of S&P announcement |
| SSGA SPY | (CSV export from fund page) | SPY NAV TR for M1 reconciliation; SPY distributions history for the dividend table cross-check | Daily for NAV; per-distribution for dividends |

Sharadar is the v1 source for all five equity tables. The five tables are pulled together as a single snapshot bundle on every refresh so that the dual-timestamp model (period_end_dt, available_dt) is internally consistent across tables at the pull moment. The SSGA SPY data is pulled separately and is required only for the M1 reconciliation harness.

Documented gaps deferred to v1.1 per ADR 0001 decision 11: borrow availability and rate feed; full PIT S&P 500 reconstitution effective dates beyond what the SP500 event log captures.

## Pull procedure

Each pull produces a snapshot bundle. The procedure for a fresh pull:

1. Acquire a Sharadar API key with subscriptions to SEP, SF1, TICKERS, SP500 (the four core tables; ~$50/month at the time of writing).
2. Run `python -m pit_backtest.data.sources.sharadar.pull --output data/snapshots/sharadar_<YYYY-MM-DD>/`. This script (M1 deliverable) downloads each table as parquet via the Sharadar bulk-export API and writes them to the dated subdirectory.
3. Pull the SSGA SPY snapshot manually from https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy (the older `etfs/spy` URL no longer redirects). From the Document section, download `spdr-etf-historical-distributions.xlsx` (ETF Historical Distributions link) and `spdr-product-data-us-en.xlsx` (Download Product Data link). Save both into `data/snapshots/spy_ssga_<YYYY-MM-DD>/`. The loader reads the XLSXs natively (filters to TICKER='SPY' and extracts SPY's row); see `docs/vendor/nasdaq-data-link-pull.md` for the workflow.
4. Run `python -m pit_backtest.data.sources.manifest update`. This script (M1 deliverable) computes the SHA256 of every parquet and CSV in `data/snapshots/`, updates `data/snapshots/manifest.toml`, and prints a diff against the prior manifest.
5. Update [this document's pull-log table](#pull-log) with the new entry (pull date, snapshot bundle name, short SHA256 prefix per file, notes on any vendor data changes observed).
6. Commit the manifest update and the doc update in a single commit with subject `chore(data): refresh Sharadar snapshot <YYYY-MM-DD>`.

The data itself is gitignored (see [Repository policy](#repository-policy) below). The manifest and the doc are the canonical commitment.

## Snapshot path convention

```
data/snapshots/
  sharadar_2026-05-28/
    sep.parquet
    actions.parquet
    sf1.parquet
    tickers.parquet
    sp500.parquet
  spy_ssga_2026-05-28/
    performance.csv
    distributions.csv
  manifest.toml
```

Rules:

- Directory names are always `<source>_<YYYY-MM-DD>` where `<source>` is one of `sharadar` (the four-table bundle) or `spy_ssga` (the SSGA SPY bundle). The date is the pull date in America/New_York.
- File names within a snapshot bundle are stable across pulls (e.g., `sep.parquet` is always the SEP table). Variant names (e.g., `sep_v2.parquet`) are not permitted; if the schema changes, the snapshot is a new pull with a new date.
- The `manifest.toml` lives at the top of `data/snapshots/` and references each snapshot bundle.

## Manifest format

`data/snapshots/manifest.toml`:

```toml
[snapshots.sharadar_2026-05-28]
source = "sharadar"
pull_date = "2026-05-28"
sharadar_api_key_fingerprint = "fp_a9c..."  # last 4 hex of SHA256 of the API key used; identifies WHO pulled, not the key itself
notes = "Initial M1 pull."

[snapshots.sharadar_2026-05-28.files]
"sep.parquet" = { sha256 = "<64-hex>", size_bytes = 0, row_count = 0 }
"actions.parquet" = { sha256 = "<64-hex>", size_bytes = 0, row_count = 0 }
"sf1.parquet" = { sha256 = "<64-hex>", size_bytes = 0, row_count = 0 }
"tickers.parquet" = { sha256 = "<64-hex>", size_bytes = 0, row_count = 0 }
"sp500.parquet" = { sha256 = "<64-hex>", size_bytes = 0, row_count = 0 }

[snapshots.spy_ssga_2026-05-28]
source = "ssga_spy"
pull_date = "2026-05-28"
source_url = "https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy"
notes = "Initial M1 reconciliation reference."

[snapshots.spy_ssga_2026-05-28.files]
"performance.csv" = { sha256 = "<64-hex>", size_bytes = 0 }
"distributions.csv" = { sha256 = "<64-hex>", size_bytes = 0 }
```

The schema is loaded at engine start by `src/pit_backtest/data/sources/manifest.py`. The adapter constructor takes a snapshot bundle name (e.g., `"sharadar_2026-05-28"`), looks up the file list, verifies each file's SHA256 against the manifest, and refuses to construct if any file is missing or modified.

## Restatement handling

Sharadar restates SF1 records. Per the WRDS Compustat literature (see [`research/sources/methodology-point-in-time.md`](../research/sources/methodology-point-in-time.md) Axis 2), a firm-quarter row's reported revenue or earnings can change after the original filing because:

- The company files a restated quarterly report (Form 10-Q/A).
- An auditor or regulator forces a correction.
- Reclassifications cascade through prior-period comparatives.

Sharadar handles restatements by updating the row in place rather than appending a new row. A pull on 2026-05-28 sees Apple's 2018-Q3 revenue at one value; a pull on 2026-08-15 may see it at a different value if Apple filed a restatement in the interim. Both values are the "as-reported" figure given the vendor's snapshot at pull time, but they describe different facts.

This matters for the engine in two ways:

1. **Backtest reproducibility.** A SPY reconciliation that passes today using `sharadar_2026-05-28` must still pass six months from now. The SHA256 commitment is what guarantees this: the test pins the bundle name; the adapter verifies the SHA256 on load; if the parquet has been replaced, the test fails loudly rather than silently producing a different number.

2. **PIT semantics.** A fundamental-factor backtest in M3 onward must use the as-reported value at each historical decision date, not the as-restated value. Sharadar's SF1 ARQ dimension is the as-reported filter, but the SHA256 commitment is the second line of defense: it pins which "as-reported" the test means, since "as-reported" itself drifts as Sharadar refreshes their own historical reconstructions.

The combination of (a) ADR 0001 decision 9's dual-timestamp model with `available_dt` gating, and (b) this document's SHA256 commitment, is the full reproducibility story. Either alone is insufficient.

## Repository policy

The `data/snapshots/` parquet files are gitignored (`.gitignore` rules `data/` and `*.parquet`). The manifest is the exception.

`.gitignore` is updated to permit the manifest:

```
data/
!data/.gitkeep
!data/snapshots/manifest.toml
```

Anyone reproducing a result must:

1. Acquire their own Sharadar API key and SSGA download.
2. Run the pull script with the exact pull date from the manifest.
3. Verify the SHA256 of their pulled parquet against the manifest entry.
4. If the SHA256 matches: their snapshot is bit-identical to the original; the test passes.
5. If the SHA256 does not match: vendor data has shifted; the test fails with a diagnostic. This is the correct failure mode.

CI does not have access to the Sharadar API. CI runs only the tests that ship fixtures inline (the toy three-day fixture; the Bailey-LdP DSR=0.971 fixture; the corp-action test fixtures in M3). The M1 SPY reconciliation runs locally before any PR merges to `main` and the PR description includes the line `M1 SPY reconciliation: PASS (delta = X.XX bps annualized, snapshot = sharadar_YYYY-MM-DD)` or an equivalent FAIL line.

This is acknowledged as a CI gap, made explicit here so it does not surprise a reviewer. The v1.1 backlog includes Git LFS or cloud-storage integration to lift the reconciliation gate into CI.

## Sharadar API key handling

The Sharadar API key is a secret. It must never be committed. The pull script reads it from:

1. `SHARADAR_API_KEY` environment variable (preferred), or
2. `~/.config/pit-backtest/sharadar_key` (a single-line file with the key) as fallback.

The manifest records a key fingerprint (the last 4 hex of the SHA256 of the key) so the team can tell which person's key was used without exposing the key itself. The fingerprint is not security-sensitive; it cannot be reversed to the key.

Per [session_rules.md](../../../.claude/projects/C--Users-SamJD-OneDrive-Desktop-AI-Projects/memory/session_rules.md), no secrets are pasted in chat or commits.

## Pull log

Each pull appends an entry below. Entries are appended on commit; never modified or deleted (the manifest is the corrective surface for any data-quality issue, not this log).

| Pull date | Snapshot bundles | Files | Notes |
|---|---|---|---|
| TBD | sharadar_<YYYY-MM-DD>, spy_ssga_<YYYY-MM-DD> | TBD | Initial M1 pull. Hashes filled in on first commit. |

## What this prevents

Concrete failure modes that the SHA256 commitment plus the manifest plus the pull-log catch:

1. **Silent vendor restatement.** Apple revises 2018-Q3 revenue in Sharadar's June 2026 refresh. Without the SHA256 commitment, a researcher's M3 backtest results drift by an unknown amount; the explanation is buried in the vendor's release notes if at all. With the commitment, the adapter raises `SnapshotMismatchError` on load; the researcher pulls the new snapshot, updates the manifest, observes the diff, and decides whether the change is benign or material.

2. **Accidental schema drift.** Sharadar adds a new column to SEP. Without the commitment, the adapter's column-position-based parsing silently shifts; with the commitment, the SHA256 changes and the engine refuses to load until the manifest is updated.

3. **Two-machine drift.** Sam's laptop pulled SEP on 2026-05-28; a co-collaborator pulled on 2026-06-15. Their results would differ by every restatement and addition between those dates. With the commitment, the M1 reconciliation explicitly says which snapshot it ran against, and discrepancies are attributable to data, not engine.

4. **Stale-data laundering.** A backtest produced six months ago is rerun today against a fresh pull and the result is presented as "today's number." With the commitment, the result is annotated with the snapshot bundle name; a reader can verify that the snapshot is current or stale.

## Cross-references

- ADR 0001 decision 10: v1 data inventory.
- ADR 0002 decision 3: Sharadar pull hash committed.
- [`docs/methodology/total_return_reconstruction.md`](total_return_reconstruction.md): the SPY reconciliation that consumes both the Sharadar SEP and the SSGA SPY snapshots.
- [`docs/methodology/determinism.md`](determinism.md): the broader determinism invariant; the snapshot commitment is the data-layer instance of it.
- [`research/sources/methodology-point-in-time.md`](../research/sources/methodology-point-in-time.md): the five PIT axes; restatement handling is Axis 2.
