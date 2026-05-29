# Nasdaq Data Link / Sharadar pull workflow

Distilled from the Nasdaq Data Link API documentation
(https://docs.data.nasdaq.com/) for the specific subset of operations
the project uses. The full raw docs are extensive (~3000 lines) but
mostly navigation; this file captures only what is needed to acquire
Sharadar snapshots and refresh the manifest.

Authoritative source pages, kept as URLs because the docs change:

- Tables API: https://docs.data.nasdaq.com/docs/tables
- Bulk download: https://docs.data.nasdaq.com/docs/large-table-download
- Python SDK installation: https://docs.data.nasdaq.com/docs/python-installation
- Rate limits: https://docs.data.nasdaq.com/docs/rate-limits-for-the-tables-api

## Two API surfaces

Nasdaq Data Link exposes two ways to retrieve tables. Pick by table size.

### Tables API (REST, filterable, paginated)

```
GET https://data.nasdaq.com/api/v3/datatables/{PUBLISHER}/{TABLE}.csv?<filters>&api_key=<KEY>
```

- Append filters per-column: `ticker=SPY`, `date.gte=2005-01-01`, `date.lte=2024-12-31`.
- Only columns flagged as `filterable` in the table's documentation page can be used as filters.
- Filter operators: `.eq`, `.gt`, `.gte`, `.lt`, `.lte`, `.in[]=A,B,C`, `.nin=A,B,C`, `.neq`, plus uppercase variants (`.uppereq` etc.).
- `qopts.columns=col1,col2` projects to specific columns.
- Pagination via `qopts.cursor_id`; the SDK handles this with `paginate=True`.
- Rate limit for Premium subscribers: 5,000 calls / 10 min, 720,000 / day.

Use Tables API for: SEP filtered to specific tickers; TICKERS; SP500; small targeted SF1 queries.

### Bulk download (async file generation, parquet output)

```
POST https://data.nasdaq.com/api/v1/bulkdownloads/{PUBLISHER}/{TABLE}?<filters>
  Header: X-Api-Token: <KEY>
```

- Returns `{"status": "PENDING"|"RUNNING"|"SUCCEEDED", "files": [{"url": ..., "size": ...}]}`.
- Poll the same endpoint until `status="SUCCEEDED"`, then GET each file URL (also with `X-Api-Token`).
- Files are parquet by default.
- Rate limit: 30 bulk requests / table / day for subscribers.
- For new subscribers, the first 25 bulk requests are free of throttling; ongoing daily limit kicks in after.

Use bulk download for: full-history SF1 (~480K rows); full-history ACTIONS (millions of rows when unfiltered).

## Authentication

The official env var name is `NASDAQ_DATA_LINK_API_KEY`. Set once at the user level so every new PowerShell session inherits it.

```
[Environment]::SetEnvironmentVariable("NASDAQ_DATA_LINK_API_KEY", "<your_key>", "User")
```

Then open a new PowerShell window so the variable loads.

Verify (this prints the key length, not the key):
```
$env:NASDAQ_DATA_LINK_API_KEY.Length
```

The project's `sharadar_pull.py` chore script also accepts the legacy `SHARADAR_API_KEY` for backwards compatibility; if both are set, `NASDAQ_DATA_LINK_API_KEY` wins.

Do not paste the key in chat. The pull script reads from env var; the API-key fingerprint in `data/snapshots/manifest.toml` is the last 4 hex of the SHA256 of the key, which identifies who pulled without exposing the key.

## Python SDK (preferred for our use)

Install once into the project's `dataops` optional dependency group:

```
uv sync --extra dataops
```

This pulls `nasdaq-data-link==1.0.5` into the venv. The package's import name is `nasdaqdatalink` (no dashes):

```python
import nasdaqdatalink
nasdaqdatalink.read_key()  # reads NASDAQ_DATA_LINK_API_KEY env var
df = nasdaqdatalink.get_table(
    "SHARADAR/SEP",
    ticker="SPY",
    date={"gte": "2005-01-01", "lte": "2024-12-31"},
    paginate=True,
)
```

`get_table` returns a `pandas.DataFrame`. Convert to Polars via `pl.from_pandas(df)` before writing parquet, so the on-disk schema matches what `SharadarDataSource.read_sep_prices` expects.

## Sharadar table inventory

The Premium bundle Sam subscribed to (see `dataset_versioning.md` for our v1 inventory):

| Table code | Project use | Pull size for our window | Recommended pull mode |
|---|---|---|---|
| `SHARADAR/SEP` | M1 prices (closeunadj + dividends-adjusted close) for SPY/AGG/GLD/all stocks | ~15K rows for 3 tickers, ~50M rows full | Tables API filtered by ticker for M1; bulk for M3 universe |
| `SHARADAR/ACTIONS` | M1 dividends + M3 splits + delisting events | ~30 rows for SPY/AGG/GLD; ~5M rows full | Tables API for M1; bulk for M3 |
| `SHARADAR/SF1` | M3 fundamentals (filtered to `dimension=ARQ`) | ~500K rows full | Bulk download |
| `SHARADAR/TICKERS` | M3 identifier history | ~25K rows | Tables API |
| `SHARADAR/SP500` | M3 S&P 500 membership event log | ~5K rows | Tables API |
| `SHARADAR/SFP` | M1 ETF prices (SPY/AGG/GLD live here if SEP returns empty for them) | small | Tables API filtered by ticker |

Note on SEP vs SFP: historically SEP covered common stocks only and SFP covered ETFs. The current Sharadar bundle includes both. If `SHARADAR/SEP` returns empty for SPY, fall back to `SHARADAR/SFP` with the same query shape; the columns are identical.

## SSGA SPY (separate from Sharadar)

For the M1 SPY reconciliation, the SSGA-published SPY NAV TR is the authoritative reference (per `docs/methodology/total_return_reconstruction.md`). No API; download two CSVs from the SSGA fund page:

- URL of record (verified 2026-05-29): https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy

The older `etfs/spy` URL no longer redirects; SSGA expanded the slug to include the full fund name. Always verify the URL with WebFetch when starting a new session that touches this workflow.

Scroll to the **Document** section and download two files into `data/snapshots/spy_ssga_<YYYY-MM-DD>/`:

1. **`spdr-etf-historical-distributions.xlsx`** via the "ETF Historical Distributions" link under Fund Documents. Per-distribution rows for every SPDR ETF; the loader filters to TICKER='SPY'.
2. **`spdr-product-data-us-en.xlsx`** via the "Download Product Data" link under Information & Schedules. Single-row-per-ETF snapshot containing the SSGA-published annualized Total Returns. The loader extracts SPY's row and reads the 1Y / 3Y / 5Y / 10Y / Since Inception cells.

Do not rename the files; the loader auto-detects them by exact filename. Then:

```
uv run python -m pit_backtest.data.sources.sharadar_pull --bundle spy_ssga_<YYYY-MM-DD> --refresh-hashes
```

If SSGA exports older-format CSVs (`distributions.csv` + `performance.csv`) instead, the loader also reads those as a fallback per the original schema. See `src/pit_backtest/data/sources/ssga.py` module docstring for the column mappings and `tests/data/test_ssga_loader.py` for the synthetic XLSX shape.

## Full M1 pull procedure

1. Set `NASDAQ_DATA_LINK_API_KEY` (one-time, see Authentication above).
2. Install the SDK: `uv sync --extra dataops`.
3. Run the M1 pull script: `uv run python scripts/pull_m1_data.py`. This creates `data/snapshots/sharadar_<today>/sep.parquet` and `actions.parquet` filtered to SPY/AGG/GLD over 2005-2024.
4. Manually download `spdr-etf-historical-distributions.xlsx` and `spdr-product-data-us-en.xlsx` from the SSGA SPY fund page into `data/snapshots/spy_ssga_<today>/`. Do not rename them; the loader auto-detects by exact filename.
5. Refresh manifest hashes for both bundles:
   ```
   uv run python -m pit_backtest.data.sources.sharadar_pull --bundle sharadar_<today> --refresh-hashes
   uv run python -m pit_backtest.data.sources.sharadar_pull --bundle spy_ssga_<today> --refresh-hashes
   ```
6. Run the M1 kill-gate locally:
   ```
   uv run python -m examples.spy_buy_and_hold --compare-to-ssga
   uv run python -m examples.constant_weight_three_names --diff-against-reference
   ```

Both should report PASS. If either fails, do not merge anything else; investigate per `total_return_reconstruction.md` "Known sources of drift" section.

## M3 full-universe pull (not yet wired)

When M3 begins, the bulk-download path is needed. A future `scripts/pull_m3_data.py` will:

1. POST a bulk-download request to `SHARADAR/SF1` filtered to `dimension=ARQ` for the M3 universe and date range.
2. Poll for `status=SUCCEEDED`.
3. Download the parquet file(s).
4. Same for `SHARADAR/ACTIONS` (full universe).
5. Same for `SHARADAR/TICKERS` and `SHARADAR/SP500` via Tables API.
6. Refresh manifest hashes.

Defer until M3 starts. The M1 script and Tables-API workflow are sufficient until then.

## Troubleshooting

- **`InvalidApiKey`**: the env var was not loaded into the current PowerShell session. Open a new window.
- **`LimitExceeded`**: you exceeded the 5,000 calls / 10 min Tables limit, or the 30 bulk / day limit. Wait and retry; the response includes `rate_limited_until` in UTC.
- **Empty result for `ticker=SPY`**: try `SHARADAR/SFP` instead (ETF table). Some snapshots route ETFs through SFP.
- **`paginate=True` returns nothing**: confirm the date filter is correctly nested (e.g., `date={"gte": "2005-01-01"}` not `date.gte="2005-01-01"` for the SDK; the REST API uses the latter).
- **Bulk download stuck in `PENDING`**: the request times out after 15 minutes of inactivity. Re-issue.
