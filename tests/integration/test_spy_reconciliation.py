"""SPY reconciliation tests.

Two modes:
1. Synthetic-fixture mode (CI-enabled): builds matching Sharadar +
   SSGA bundles with hand-computed values, runs reconcile_spy, asserts
   the delta is near zero (only float rounding). This exercises the
   full wiring without requiring real vendor data.
2. Real-snapshot mode (CI-skipped): runs against the actual
   data/snapshots/sharadar_<YYYY-MM-DD>/ and data/snapshots/spy_ssga_<YYYY-MM-DD>/
   bundles, asserts |delta| <= 5 bps over the M1 window per ADR 0002
   acceptance criterion 1. The kill-early gate; failure ends the
   project per docs/ROADMAP.md.

Plus a one-quarter preflight per docs/methodology/total_return_reconstruction.md.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    SPY_EXPENSE_RATIO_POST_2003,
    discover_latest_bundle,
    reconcile_spy,
)


# Repo root resolved relative to this test file's location.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"


# ----- Synthetic-fixture mode -----


def _build_synthetic_sharadar(
    bundle_dir: Path,
    rows: list[dict[str, object]],
    actions: list[dict[str, object]],
) -> tuple[str, int, str, int]:
    """Write a synthetic Sharadar SEP + ACTIONS bundle. Returns
    (sep_sha, sep_size, actions_sha, actions_size).
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    sep_df = pl.DataFrame(rows)
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)
    actions_df = pl.DataFrame(actions)
    actions_path = bundle_dir / "actions.parquet"
    actions_df.write_parquet(actions_path)
    return (
        hashlib.sha256(sep_path.read_bytes()).hexdigest(),
        sep_path.stat().st_size,
        hashlib.sha256(actions_path.read_bytes()).hexdigest(),
        actions_path.stat().st_size,
    )


def _build_synthetic_ssga(
    bundle_dir: Path, performance_csv: str, distributions_csv: str
) -> tuple[str, int, str, int]:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    dist_path = bundle_dir / "distributions.csv"
    dist_path.write_bytes(distributions_csv.encode("utf-8"))
    perf_path = bundle_dir / "performance.csv"
    perf_path.write_bytes(performance_csv.encode("utf-8"))
    return (
        hashlib.sha256(dist_path.read_bytes()).hexdigest(),
        dist_path.stat().st_size,
        hashlib.sha256(perf_path.read_bytes()).hexdigest(),
        perf_path.stat().st_size,
    )


def test_synthetic_reconciliation_matches_known_annualized_tr(tmp_path: Path) -> None:
    """A 252-trading-day window with a hand-chosen price path produces a
    known annualized TR. The synthetic SSGA performance.csv reports the
    same TR; reconcile_spy should report a delta of zero (modulo float
    rounding).

    Price path: 252 trading days, daily multiplier exactly 1.0005 (no
    dividends, no expense drag). Annualized return = 1.0005**252 - 1
    = ~0.1340 = 13.40%.

    Engine and SSGA both report this; delta should be < 0.01 bps.
    """
    snapshots_root = tmp_path / "snapshots"

    # 253 trading days starting 2024-01-02 (using calendar days for
    # simplicity; the test doesn't care about NYSE holidays since
    # reconstruct_total_return treats the input dt sequence as-is).
    n = 253
    base = date(2024, 1, 2)
    sep_rows: list[dict[str, object]] = []
    daily_mult = 1.0005
    px = 100.0
    for i in range(n):
        from datetime import timedelta
        d = base + timedelta(days=i)
        sep_rows.append({
            "ticker": "SPY",
            "date": d,
            "open": px,
            "high": px,
            "low": px,
            "close": px,
            "closeunadj": px,
            "volume": 1_000_000,
        })
        px = px * daily_mult

    # No dividends, no expense drag for this fixture.
    actions_rows: list[dict[str, object]] = [
        {"ticker": "SPY", "date": date(2099, 1, 1), "action": "split", "value": 1.0}
    ]

    sharadar_dir = snapshots_root / "sharadar_2026-05-28"
    sep_sha, sep_size, act_sha, act_size = _build_synthetic_sharadar(
        sharadar_dir, sep_rows, actions_rows
    )

    expected_ann = (daily_mult ** 252) - 1.0  # ~0.1340
    perf_csv = f"""period,annualized_nav_tr_pct,annualized_market_price_tr_pct
1y,{expected_ann * 100:.10f},{expected_ann * 100:.10f}
"""
    dist_csv = """ex_date,record_date,payable_date,amount_per_share
"""
    ssga_dir = snapshots_root / "spy_ssga_2026-05-28"
    dist_sha, dist_size, perf_sha, perf_size = _build_synthetic_ssga(
        ssga_dir, perf_csv, dist_csv
    )

    manifest = f"""
[snapshots.sharadar_2026-05-28]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.sharadar_2026-05-28.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_size}, row_count = {n} }}
"actions.parquet" = {{ sha256 = "{act_sha}", size_bytes = {act_size}, row_count = {len(actions_rows)} }}

[snapshots.spy_ssga_2026-05-28]
source = "ssga_spy"
pull_date = 2026-05-28

[snapshots.spy_ssga_2026-05-28.files]
"distributions.csv" = {{ sha256 = "{dist_sha}", size_bytes = {dist_size} }}
"performance.csv" = {{ sha256 = "{perf_sha}", size_bytes = {perf_size} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    sharadar = SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)

    report = reconcile_spy(
        sharadar=sharadar,
        ssga=ssga,
        start_dt=date(2024, 1, 2),
        end_dt=base + __import__("datetime").timedelta(days=n - 1),
        ssga_period_label="1y",
        expense_ratio_annual=Decimal("0"),  # synthetic, no fee drag
    )

    # Engine should reproduce the exact compounded annualized return.
    # SSGA reports the same value. Delta should be < 1 bp.
    assert abs(report.delta_bps) < 1.0, (
        f"synthetic reconciliation drift: engine={report.engine_annualized_return:.6f}, "
        f"ssga={report.ssga_annualized_return:.6f}, delta={report.delta_bps:+.2f} bps"
    )
    assert report.passes_kill_gate()
    assert report.n_trading_days == n


def test_reconciliation_report_evidence_line_format(tmp_path: Path) -> None:
    """ReconciliationReport.render_evidence_line produces the exact format
    documented in docs/methodology/dataset_versioning.md.
    """
    from pit_backtest.engine.spy_reconciliation import ReconciliationReport

    report = ReconciliationReport(
        engine_annualized_return=0.1015,
        ssga_annualized_return=0.1010,
        delta_bps=5.0,
        window_start_dt=date(2005, 1, 3),
        window_end_dt=date(2024, 12, 31),
        ssga_period_label="10y",
        sharadar_bundle="sharadar_2026-05-28",
        ssga_bundle="spy_ssga_2026-05-28",
        n_trading_days=5031,
    )
    line = report.render_evidence_line()
    assert line.startswith("M1 SPY reconciliation: PASS")
    assert "delta = +5.00 bps" in line
    assert "ssga_period = 10y" in line
    assert "n_trading_days = 5031" in line


def test_kill_gate_fails_above_tolerance() -> None:
    from pit_backtest.engine.spy_reconciliation import ReconciliationReport

    report = ReconciliationReport(
        engine_annualized_return=0.10,
        ssga_annualized_return=0.099,  # 10 bps below engine
        delta_bps=10.0,
        window_start_dt=date(2005, 1, 3),
        window_end_dt=date(2024, 12, 31),
        ssga_period_label="10y",
        sharadar_bundle="x",
        ssga_bundle="y",
        n_trading_days=5031,
    )
    assert not report.passes_kill_gate(tolerance_bps=5.0)
    assert report.passes_kill_gate(tolerance_bps=15.0)


# ----- Real-snapshot mode -----


def _real_sharadar_bundle() -> str | None:
    return discover_latest_bundle(_SNAPSHOTS_ROOT, "sharadar")


def _real_ssga_bundle() -> str | None:
    return discover_latest_bundle(_SNAPSHOTS_ROOT, "spy_ssga")


@pytest.mark.snapshot
@pytest.mark.kill_gate
def test_spy_reconciliation_full_window_2005_2024() -> None:
    """M1 acceptance criterion 1: SPY buy-and-hold 2005-2024 within 5 bps
    annualized of SSGA-published NAV TR.

    Reconciliation window aligned to SSGA's 10y published period when
    that brackets a stable comparison; for M1 the 20-year window with
    SSGA's "10y" or "si" period is used per the methodology doc
    (precise period chosen at first real pull).

    Gated on snapshot availability; skipped in CI per
    docs/methodology/dataset_versioning.md (CI does not carry vendor data).
    """
    sharadar_bundle = _real_sharadar_bundle()
    ssga_bundle = _real_ssga_bundle()
    if sharadar_bundle is None or ssga_bundle is None:
        pytest.skip(
            "no sharadar/spy_ssga snapshots in data/snapshots/; "
            "pull per docs/methodology/dataset_versioning.md to run this gate"
        )

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)

    report = reconcile_spy(
        sharadar=sharadar,
        ssga=ssga,
        start_dt=date(2005, 1, 3),  # first NYSE trading day of 2005
        end_dt=date(2024, 12, 31),
        ssga_period_label="10y",  # adjust per the period that brackets the window
        expense_ratio_annual=SPY_EXPENSE_RATIO_POST_2003,
    )

    print(report.render_evidence_line())
    assert report.passes_kill_gate(tolerance_bps=5.0), (
        f"M1 kill-early gate failed: {report.render_evidence_line()}"
    )


@pytest.mark.snapshot
def test_spy_reconciliation_one_quarter_preflight() -> None:
    """Pre-flight sanity check per docs/methodology/total_return_reconstruction.md.

    Runs the reconciliation over a single quarter (2024 Q1) before the
    full 20-year window. If the quarter is more than 20 bps off, debug
    before running the full window.

    Gated on snapshot availability; skipped in CI.
    """
    sharadar_bundle = _real_sharadar_bundle()
    ssga_bundle = _real_ssga_bundle()
    if sharadar_bundle is None or ssga_bundle is None:
        pytest.skip(
            "no sharadar/spy_ssga snapshots in data/snapshots/; "
            "pull per docs/methodology/dataset_versioning.md to run preflight"
        )

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)

    # 2024 Q1 has 61 trading days; SSGA's "3m" or "ytd" period brackets
    # this. The synthetic preflight harness uses the quarter as a quick
    # signal: a drift large here means a fundamental wiring issue and
    # the 20-year run will not pass.
    report = reconcile_spy(
        sharadar=sharadar,
        ssga=ssga,
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 3, 28),
        ssga_period_label="3m",
        expense_ratio_annual=SPY_EXPENSE_RATIO_POST_2003,
    )
    print(report.render_evidence_line())
    assert report.passes_kill_gate(tolerance_bps=20.0), (
        f"preflight quarterly reconciliation off by more than 20 bps: "
        f"{report.render_evidence_line()}"
    )
