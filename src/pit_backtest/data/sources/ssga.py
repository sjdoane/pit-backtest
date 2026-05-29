"""SSGA SPY reference loader (M1 reconciliation reference).

Per docs/methodology/total_return_reconstruction.md, the M1 reconciliation
compares the engine's reconstructed SPY TR to SSGA's published SPY NAV TR.
This module loads the SSGA snapshot (Performance + Distributions CSVs)
under data/snapshots/spy_ssga_<YYYY-MM-DD>/ with the same SHA256
verification pattern as the Sharadar adapter.

SSGA CSV column conventions (vendor-determined, subject to adjustment
when first real pull lands):

distributions.csv:
    ex_date, record_date, payable_date, amount_per_share
    Per-distribution rows for SPY's full distribution history.

performance.csv:
    period, annualized_nav_tr_pct, annualized_market_price_tr_pct
    Period labels: "1m", "3m", "ytd", "1y", "3y", "5y", "10y", "si"
    annualized_*_tr_pct columns are floats in percent (e.g., 12.34 = 12.34%/yr).

The synthetic fixture in tests/data/test_ssga_loader.py uses this exact
shape. The real SSGA export may have additional columns or different
casing; adjust the column projections in `_DISTRIBUTIONS_COLS` and
`_PERFORMANCE_COLS` when Sam's first pull lands.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from pit_backtest.data.sources.manifest import load_manifest, verify_bundle


_DISTRIBUTIONS_FILENAME = "distributions.csv"
_PERFORMANCE_FILENAME = "performance.csv"


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
        """
        if self._distributions is None:
            path = self._bundle_dir / _DISTRIBUTIONS_FILENAME
            if not path.is_file():
                raise FileNotFoundError(
                    f"SSGA bundle missing {_DISTRIBUTIONS_FILENAME} at {path}"
                )
            raw = pl.read_csv(path, try_parse_dates=True)
            self._distributions = raw.select(
                pl.col("ex_date").cast(pl.Date),
                pl.col("amount_per_share").cast(pl.Float64),
            ).sort("ex_date")
        return self._distributions

    def performance(self) -> pl.DataFrame:
        """Return the SSGA-published SPY performance summary.

        Columns: period (pl.String), annualized_nav_tr_pct (pl.Float64),
        annualized_market_price_tr_pct (pl.Float64).

        Period labels are case-normalized to lowercase to make
        annualized_nav_tr_for_period lookups robust to vendor casing
        changes.
        """
        if self._performance is None:
            path = self._bundle_dir / _PERFORMANCE_FILENAME
            if not path.is_file():
                raise FileNotFoundError(
                    f"SSGA bundle missing {_PERFORMANCE_FILENAME} at {path}"
                )
            raw = pl.read_csv(path)
            self._performance = raw.select(
                pl.col("period").cast(pl.String).str.to_lowercase(),
                pl.col("annualized_nav_tr_pct").cast(pl.Float64),
                pl.col("annualized_market_price_tr_pct").cast(pl.Float64),
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
        # Convert percent to decimal (12.34 -> 0.1234).
        return float(perf["annualized_nav_tr_pct"][0]) / 100.0


def reconciliation_delta_bps(
    engine_annualized_return: float, ssga_annualized_return: float
) -> float:
    """Return the engine-vs-SSGA annualized return delta in basis points.

    Positive = engine overstates relative to SSGA. The M1 kill-early gate
    asserts abs(delta_bps) <= 5 bps over the 2005-2024 window.
    """
    return (engine_annualized_return - ssga_annualized_return) * 10_000.0
