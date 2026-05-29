"""SSGA SPY reference loader (M1 reconciliation reference).

Per docs/methodology/total_return_reconstruction.md, the M1 reconciliation
compares the engine's reconstructed SPY TR to SSGA's published SPY NAV TR.
This module loads the SSGA snapshot under data/snapshots/spy_ssga_<YYYY-MM-DD>/
with the same SHA256 verification pattern as the Sharadar adapter.

The loader handles two SSGA-export shapes:

1. SSGA-native XLSX (preferred, what SSGA actually publishes today):
   - spdr-etf-historical-distributions.xlsx (sheet "dividend"): per-distribution
     rows for every SPDR ETF; the loader filters to TICKER='SPY' and maps
     EX-DATE, DIVIDEND ($), to ex_date, amount_per_share.
   - spdr-product-data-us-en.xlsx (sheet "Sheet1"): wide one-row-per-ETF
     snapshot with a two-tier column header. The loader uses openpyxl
     directly to walk the headers, find SPY's row, and extract the
     "Total Returns (Annualized)" cells for 1 Year / 3 Year / 5 Year /
     10 Year / Since Inception.

2. Legacy CSV (synthetic tests and any prior manual exports):
   - distributions.csv: ex_date, record_date, payable_date, amount_per_share
   - performance.csv: period, annualized_nav_tr_pct, annualized_market_price_tr_pct

The XLSX format wins when both are present in a bundle.

Vendor URL of record (verified 2026-05-29):
https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy

SSGA's older URL (https://www.ssga.com/us/en/intermediary/etfs/spy) no
longer redirects. See docs/vendor/nasdaq-data-link-pull.md for the
download workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from pit_backtest.data.sources.manifest import load_manifest, verify_bundle


# Legacy CSV filenames (kept for synthetic tests and any prior manual exports).
_DISTRIBUTIONS_CSV_FILENAME = "distributions.csv"
_PERFORMANCE_CSV_FILENAME = "performance.csv"

# SSGA-native XLSX filenames as the vendor publishes them today.
_DISTRIBUTIONS_XLSX_FILENAME = "spdr-etf-historical-distributions.xlsx"
_PRODUCT_DATA_XLSX_FILENAME = "spdr-product-data-us-en.xlsx"
_PRODUCT_DATA_XLSX_SHEET = "Sheet1"
_DISTRIBUTIONS_XLSX_SHEET = "dividend"


# SSGA column labels in spdr-etf-historical-distributions.xlsx. Capture the
# spellings here so any future renames surface in one place.
_DIST_TICKER_COL = "TICKER"
_DIST_EX_DATE_COL = "EX-DATE"
_DIST_AMOUNT_COL = "DIVIDEND ($)"

# SSGA column labels in spdr-product-data-us-en.xlsx. The product data
# workbook has a two-tier header: row 1 carries group names, row 2 carries
# the period sub-labels. We anchor on the row-1 group name "Total Returns
# (Annualized)" and then read the next 5 columns whose row-2 sub-labels
# are "1 Year", "3 Year", "5 Year", "10 Year", "Since Inception".
_PROD_TICKER_HEADER = "Ticker"
_PROD_ANN_RETURNS_HEADER = "Total Returns (Annualized)"
_PROD_ANN_PERIOD_LABELS = ("1 Year", "3 Year", "5 Year", "10 Year", "Since Inception")
# Map SSGA's "1 Year" -> our canonical "1y" tag used everywhere downstream.
_SSGA_PERIOD_TO_TAG = {
    "1 Year": "1y",
    "3 Year": "3y",
    "5 Year": "5y",
    "10 Year": "10y",
    "Since Inception": "si",
}


class SSGAFormatError(ValueError):
    """Raised when the SSGA XLSX schema does not match the expected shape.

    Typical cause: SSGA renamed a column header or changed the row layout.
    Diagnose by opening the XLSX in Excel and comparing to the constants
    at the top of this module.
    """


class SSGASpyReference:
    """Loads the SSGA SPY snapshot for the M1 reconciliation."""

    def __init__(self, snapshot_bundle: str, snapshots_root: Path) -> None:
        self._bundle_name = snapshot_bundle
        self._snapshots_root = snapshots_root.resolve()
        manifest_path = self._snapshots_root / "manifest.toml"
        self._manifest = load_manifest(manifest_path)
        verify_bundle(snapshot_bundle, self._snapshots_root, self._manifest)
        self._bundle_dir = self._snapshots_root / snapshot_bundle

        self._distributions: pl.DataFrame | None = None
        self._performance: pl.DataFrame | None = None

    @property
    def bundle_name(self) -> str:
        return self._bundle_name

    def dividends(self) -> pl.DataFrame:
        """Return the SSGA-published SPY distribution history.

        Columns: ex_date (pl.Date), amount_per_share (pl.Float64).
        Sorted by ex_date for determinism.

        Reads spdr-etf-historical-distributions.xlsx if present (filtered
        to TICKER='SPY'); otherwise falls back to distributions.csv.
        """
        if self._distributions is not None:
            return self._distributions

        xlsx_path = self._bundle_dir / _DISTRIBUTIONS_XLSX_FILENAME
        csv_path = self._bundle_dir / _DISTRIBUTIONS_CSV_FILENAME
        if xlsx_path.is_file():
            self._distributions = _read_distributions_xlsx(xlsx_path)
        elif csv_path.is_file():
            raw = pl.read_csv(csv_path, try_parse_dates=True)
            self._distributions = raw.select(
                pl.col("ex_date").cast(pl.Date),
                pl.col("amount_per_share").cast(pl.Float64),
            ).sort("ex_date")
        else:
            raise FileNotFoundError(
                f"SSGA bundle has neither {_DISTRIBUTIONS_XLSX_FILENAME} nor "
                f"{_DISTRIBUTIONS_CSV_FILENAME} under {self._bundle_dir}"
            )
        return self._distributions

    def performance(self) -> pl.DataFrame:
        """Return the SSGA-published SPY performance summary.

        Columns: period (pl.String), annualized_nav_tr_pct (pl.Float64),
        annualized_market_price_tr_pct (pl.Float64).

        Reads spdr-product-data-us-en.xlsx if present (extracts SPY's row
        and the Annualized Total Returns block); otherwise falls back to
        performance.csv.

        Note: the XLSX path populates annualized_market_price_tr_pct with
        NaN because SSGA's product-data workbook reports only NAV-based
        annualized returns by default. The reconciliation uses
        annualized_nav_tr_pct exclusively, so this is acceptable. The CSV
        path requires both columns to be present per the legacy schema.
        """
        if self._performance is not None:
            return self._performance

        xlsx_path = self._bundle_dir / _PRODUCT_DATA_XLSX_FILENAME
        csv_path = self._bundle_dir / _PERFORMANCE_CSV_FILENAME
        if xlsx_path.is_file():
            self._performance = _read_performance_xlsx(xlsx_path)
        elif csv_path.is_file():
            raw = pl.read_csv(csv_path)
            self._performance = raw.select(
                pl.col("period").cast(pl.String).str.to_lowercase(),
                pl.col("annualized_nav_tr_pct").cast(pl.Float64),
                pl.col("annualized_market_price_tr_pct").cast(pl.Float64),
            )
        else:
            raise FileNotFoundError(
                f"SSGA bundle has neither {_PRODUCT_DATA_XLSX_FILENAME} nor "
                f"{_PERFORMANCE_CSV_FILENAME} under {self._bundle_dir}"
            )
        return self._performance

    def annualized_nav_tr_for_period(self, period: str) -> float:
        """Return SSGA's published annualized NAV TR for a labeled period.

        period is one of '1m', '3m', 'ytd', '1y', '3y', '5y', '10y', 'si'.
        Returned as a decimal (e.g., 0.1234 for 12.34%/yr), not percent.
        """
        normalized = period.lower()
        perf = self.performance().filter(pl.col("period") == normalized)
        if perf.height == 0:
            raise KeyError(
                f"period '{period}' not in SSGA performance snapshot; "
                f"available: {sorted(self.performance()['period'].to_list())}"
            )
        if perf.height > 1:
            raise ValueError(
                f"period '{period}' is duplicated in SSGA performance snapshot; "
                f"manifest may be stale"
            )
        return float(perf["annualized_nav_tr_pct"][0]) / 100.0


def reconciliation_delta_bps(
    engine_annualized_return: float, ssga_annualized_return: float
) -> float:
    """Return the engine-vs-SSGA annualized return delta in basis points.

    Positive = engine overstates relative to SSGA. The M1 kill-early gate
    asserts abs(delta_bps) <= 5 bps over the 2005-2024 window.
    """
    return (engine_annualized_return - ssga_annualized_return) * 10_000.0


def _read_distributions_xlsx(path: Path) -> pl.DataFrame:
    """Read SSGA's distributions XLSX and produce our canonical schema.

    Filters to TICKER='SPY'. Returns columns (ex_date, amount_per_share)
    sorted by ex_date.
    """
    import openpyxl  # type: ignore[import-untyped]

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if _DISTRIBUTIONS_XLSX_SHEET not in workbook.sheetnames:
        raise SSGAFormatError(
            f"SSGA distributions XLSX missing sheet '{_DISTRIBUTIONS_XLSX_SHEET}'; "
            f"found {workbook.sheetnames}"
        )
    sheet = workbook[_DISTRIBUTIONS_XLSX_SHEET]

    rows_iter = sheet.iter_rows(values_only=True)
    header = list(next(rows_iter))
    try:
        ticker_col = header.index(_DIST_TICKER_COL)
        ex_date_col = header.index(_DIST_EX_DATE_COL)
        amount_col = header.index(_DIST_AMOUNT_COL)
    except ValueError as e:
        raise SSGAFormatError(
            f"SSGA distributions XLSX header missing one of "
            f"({_DIST_TICKER_COL}, {_DIST_EX_DATE_COL}, {_DIST_AMOUNT_COL}); "
            f"got headers {header}"
        ) from e

    ex_dates: list[Any] = []
    amounts: list[float] = []
    for row in rows_iter:
        if row is None:
            continue
        ticker = row[ticker_col]
        if ticker != "SPY":
            continue
        ex_date = row[ex_date_col]
        amount = row[amount_col]
        if ex_date is None or amount is None:
            continue
        ex_dates.append(ex_date)
        amounts.append(float(amount))
    workbook.close()

    if not ex_dates:
        raise SSGAFormatError(
            f"SSGA distributions XLSX has no SPY rows; check TICKER column "
            f"content. File: {path}"
        )

    return (
        pl.DataFrame({"ex_date": ex_dates, "amount_per_share": amounts})
        .with_columns(pl.col("ex_date").cast(pl.Date))
        .sort("ex_date")
    )


def _read_performance_xlsx(path: Path) -> pl.DataFrame:
    """Read SSGA's product-data XLSX and produce our performance schema.

    Returns columns (period, annualized_nav_tr_pct, annualized_market_price_tr_pct).
    annualized_market_price_tr_pct is NaN because the workbook does not
    expose a Market Price annualized return; the reconciliation uses the
    NAV column exclusively.
    """
    import math

    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if _PRODUCT_DATA_XLSX_SHEET not in workbook.sheetnames:
        raise SSGAFormatError(
            f"SSGA product-data XLSX missing sheet '{_PRODUCT_DATA_XLSX_SHEET}'; "
            f"found {workbook.sheetnames}"
        )
    sheet = workbook[_PRODUCT_DATA_XLSX_SHEET]

    rows_iter = sheet.iter_rows(values_only=True)
    header_row_1 = list(next(rows_iter))
    header_row_2 = list(next(rows_iter))

    try:
        ticker_col = header_row_1.index(_PROD_TICKER_HEADER)
    except ValueError as e:
        raise SSGAFormatError(
            f"SSGA product-data XLSX header row 1 missing '{_PROD_TICKER_HEADER}'; "
            f"got {header_row_1}"
        ) from e

    try:
        ann_block_start = header_row_1.index(_PROD_ANN_RETURNS_HEADER)
    except ValueError as e:
        raise SSGAFormatError(
            f"SSGA product-data XLSX header row 1 missing "
            f"'{_PROD_ANN_RETURNS_HEADER}'; got {header_row_1}"
        ) from e

    period_cols: dict[str, int] = {}
    for offset, expected_label in enumerate(_PROD_ANN_PERIOD_LABELS):
        col = ann_block_start + offset
        actual_label = header_row_2[col] if col < len(header_row_2) else None
        if actual_label != expected_label:
            raise SSGAFormatError(
                f"SSGA product-data XLSX header row 2 column {col} expected "
                f"'{expected_label}' but got '{actual_label}'. The Annualized "
                f"Total Returns block layout has changed; update _PROD_ANN_PERIOD_LABELS."
            )
        period_cols[_SSGA_PERIOD_TO_TAG[expected_label]] = col

    spy_row: tuple[Any, ...] | None = None
    for row in rows_iter:
        if row is None:
            continue
        if len(row) > ticker_col and row[ticker_col] == "SPY":
            spy_row = row
            break
    workbook.close()

    if spy_row is None:
        raise SSGAFormatError(
            f"SSGA product-data XLSX has no SPY row in column {ticker_col}; "
            f"file may be missing the SPY entry or the workbook structure has changed."
        )

    periods: list[str] = []
    nav_pct: list[float] = []
    mkt_pct: list[float] = []
    for tag in ("1y", "3y", "5y", "10y", "si"):
        col = period_cols[tag]
        raw_value = spy_row[col]
        if raw_value is None:
            continue
        as_float = float(raw_value)
        # SSGA product-data returns are quoted in percent at one decimal
        # place (e.g., 9.95 for 9.95%/yr). They are NOT decimals.
        periods.append(tag)
        nav_pct.append(as_float)
        mkt_pct.append(math.nan)

    return pl.DataFrame(
        {
            "period": periods,
            "annualized_nav_tr_pct": nav_pct,
            "annualized_market_price_tr_pct": mkt_pct,
        }
    )
