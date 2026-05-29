"""SPY reconciliation harness (M1 kill-early gate).

Wires the SharadarDataSource + SSGASpyReference + reconstruct_total_return
into a single callable that produces the engine vs SSGA annualized-TR
delta in basis points. The M1 acceptance criterion is |delta| <= 5 bps
over 2005-2024.

The runner is intentionally thin: it composes the existing primitives.
The integration test in tests/integration/test_spy_reconciliation.py
exercises both synthetic-fixture and real-snapshot modes (the latter
gated on snapshot availability).

Per docs/methodology/total_return_reconstruction.md:
- engine TR uses Sharadar SEP closeunadj + ACTIONS dividends
- expense-ratio drag explicitly subtracted (default 0.0945% for SPY post-2003-11)
- reference is SSGA-published NAV TR over the same window
- delta in bps = (engine_ann - ssga_ann) * 10_000
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import attrs

from pit_backtest.data.adjustments import annualized_return, reconstruct_total_return
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference, reconciliation_delta_bps
from pit_backtest.utils.logging import get_logger
from pit_backtest.utils.timezones import to_nyse_close


# SPY expense ratio post-2003-11 (current). Pre-2003-11 was 0.12%; M1's
# 2005-2024 window is entirely after the reduction. Per
# docs/methodology/total_return_reconstruction.md.
SPY_EXPENSE_RATIO_POST_2003 = Decimal("0.000945")

# SSGA period label that best corresponds to the M1 window. SSGA's
# canonical published horizons are 1m, 3m, ytd, 1y, 3y, 5y, 10y, si.
# 10y is the longest non-since-inception window and is the cleanest
# external check. SI (since 1993-01-22) is the alternative; both are
# reconcilable. Default to 10y for the standard M1 reconciliation.
DEFAULT_SSGA_PERIOD = "10y"


_log = get_logger(__name__)


@attrs.frozen(slots=True)
class ReconciliationReport:
    """Engine vs SSGA annualized-TR reconciliation result."""

    engine_annualized_return: float
    ssga_annualized_return: float
    delta_bps: float
    window_start_dt: date
    window_end_dt: date
    ssga_period_label: str
    sharadar_bundle: str
    ssga_bundle: str
    n_trading_days: int

    def passes_kill_gate(self, tolerance_bps: float = 5.0) -> bool:
        """True if abs(delta_bps) <= tolerance_bps. Default 5 bps is the
        M1 kill-early gate from ADR 0002 acceptance criterion 1.
        """
        return abs(self.delta_bps) <= tolerance_bps

    def render_evidence_line(self) -> str:
        """Format the result for the PR description line documented in
        docs/methodology/dataset_versioning.md.
        """
        verdict = "PASS" if self.passes_kill_gate() else "FAIL"
        return (
            f"M1 SPY reconciliation: {verdict} "
            f"(delta = {self.delta_bps:+.2f} bps annualized, "
            f"window = {self.window_start_dt}..{self.window_end_dt}, "
            f"ssga_period = {self.ssga_period_label}, "
            f"sharadar_bundle = {self.sharadar_bundle}, "
            f"ssga_bundle = {self.ssga_bundle}, "
            f"n_trading_days = {self.n_trading_days})"
        )


def reconcile_spy(
    sharadar: SharadarDataSource,
    ssga: SSGASpyReference,
    start_dt: date | datetime,
    end_dt: date | datetime,
    ssga_period_label: str = DEFAULT_SSGA_PERIOD,
    spy_ticker: str = "SPY",
    expense_ratio_annual: Decimal = SPY_EXPENSE_RATIO_POST_2003,
) -> ReconciliationReport:
    """Compute the engine vs SSGA annualized-TR delta for SPY.

    The engine TR is reconstructed from SharadarDataSource's SEP closeunadj
    and ACTIONS dividends with the explicit expense-ratio drag. The SSGA
    reference is read from the snapshot at the labeled period (default
    '10y'; configurable for other reconciliation windows).
    """
    start = start_dt.date() if isinstance(start_dt, datetime) else start_dt
    end = end_dt.date() if isinstance(end_dt, datetime) else end_dt

    _log.info(
        "spy_reconciliation_begin",
        extra={
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "ssga_period": ssga_period_label,
            "sharadar_bundle": sharadar.bundle_name,
            "ssga_bundle": ssga.bundle_name,
            "expense_ratio_annual": str(expense_ratio_annual),
        },
    )

    prices = sharadar.read_sep_prices(
        ticker=spy_ticker, start_dt=start, end_dt=end
    )
    # Engine uses closeunadj (raw) + dividends; back-adjusted close is
    # double-applying per docs/methodology/total_return_reconstruction.md.
    prices_for_tr = prices.select(
        prices["dt"], prices["closeunadj"].alias("close")
    )
    dividends = sharadar.read_actions_dividends(
        ticker=spy_ticker, start_dt=start, end_dt=end
    )

    tr_series = reconstruct_total_return(
        prices_for_tr,
        dividends,
        start_dt=start,
        end_dt=end,
        expense_ratio_annual=expense_ratio_annual,
    )
    engine_ann = annualized_return(tr_series)
    ssga_ann = ssga.annualized_nav_tr_for_period(ssga_period_label)
    delta_bps = reconciliation_delta_bps(engine_ann, ssga_ann)

    report = ReconciliationReport(
        engine_annualized_return=engine_ann,
        ssga_annualized_return=ssga_ann,
        delta_bps=delta_bps,
        window_start_dt=start,
        window_end_dt=end,
        ssga_period_label=ssga_period_label,
        sharadar_bundle=sharadar.bundle_name,
        ssga_bundle=ssga.bundle_name,
        n_trading_days=tr_series.height,
    )
    _log.info(
        "spy_reconciliation_complete",
        extra={
            "engine_ann_pct": f"{engine_ann * 100:.4f}",
            "ssga_ann_pct": f"{ssga_ann * 100:.4f}",
            "delta_bps": f"{delta_bps:+.2f}",
            "verdict": "PASS" if report.passes_kill_gate() else "FAIL",
        },
    )
    return report


def discover_latest_bundle(
    snapshots_root: Path, prefix: str
) -> str | None:
    """Find the most recent snapshot bundle matching prefix_<YYYY-MM-DD>.

    Used by the integration test and the CLI to default to "the latest
    snapshot Sam pulled" without hardcoding a date. Returns None if no
    matching bundle exists.
    """
    if not snapshots_root.is_dir():
        return None
    candidates = sorted(
        p.name
        for p in snapshots_root.iterdir()
        if p.is_dir() and p.name.startswith(prefix + "_")
    )
    return candidates[-1] if candidates else None
