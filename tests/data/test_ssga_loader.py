"""SSGA SPY loader tests.

Synthetic fixture matching the vendor schema documented in
src/pit_backtest/data/sources/ssga.py. No real SSGA data required.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from pit_backtest.data.sources.manifest import SnapshotMismatchError
from pit_backtest.data.sources.ssga import (
    SSGASpyReference,
    reconciliation_delta_bps,
)


_DISTRIBUTIONS_CSV = """ex_date,record_date,payable_date,amount_per_share
2023-12-15,2023-12-18,2024-01-31,1.5800
2024-03-15,2024-03-18,2024-04-30,1.7715
"""

_PERFORMANCE_CSV = """period,annualized_nav_tr_pct,annualized_market_price_tr_pct
1m,1.20,1.21
3m,5.40,5.41
ytd,8.50,8.49
1y,15.20,15.22
3y,10.10,10.11
5y,12.30,12.32
10y,11.50,11.49
si,9.95,9.94
"""


def _write_synthetic_bundle(tmp_path: Path, bundle_name: str = "spy_ssga_2026-05-28") -> Path:
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    dist_path = bundle_dir / "distributions.csv"
    dist_path.write_bytes(_DISTRIBUTIONS_CSV.encode("utf-8"))
    perf_path = bundle_dir / "performance.csv"
    perf_path.write_bytes(_PERFORMANCE_CSV.encode("utf-8"))

    dist_sha = hashlib.sha256(dist_path.read_bytes()).hexdigest()
    perf_sha = hashlib.sha256(perf_path.read_bytes()).hexdigest()
    dist_size = dist_path.stat().st_size
    perf_size = perf_path.stat().st_size

    manifest_content = f"""
[snapshots.{bundle_name}]
source = "ssga_spy"
pull_date = 2026-05-28

[snapshots.{bundle_name}.files]
"distributions.csv" = {{ sha256 = "{dist_sha}", size_bytes = {dist_size} }}
"performance.csv" = {{ sha256 = "{perf_sha}", size_bytes = {perf_size} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest_content, encoding="utf-8")
    return snapshots_root


def test_construction_verifies_manifest(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    assert ssga.bundle_name == "spy_ssga_2026-05-28"


def test_tampered_file_fails_construction(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    (snapshots_root / "spy_ssga_2026-05-28" / "distributions.csv").write_bytes(b"tampered")
    with pytest.raises(SnapshotMismatchError, match="SHA256 mismatch"):
        SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)


def test_dividends_returns_sorted_frame(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    divs = ssga.dividends()
    assert divs.columns == ["ex_date", "amount_per_share"]
    assert divs["ex_date"].to_list() == [date(2023, 12, 15), date(2024, 3, 15)]
    assert divs["amount_per_share"][1] == pytest.approx(1.7715)


def test_performance_normalizes_period_casing(tmp_path: Path) -> None:
    """Period labels normalize to lowercase so lookups are case-insensitive."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    perf = ssga.performance()
    assert "10y" in perf["period"].to_list()


def test_annualized_nav_tr_lookup(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    # 10y is 11.50% in the fixture; returned as 0.1150 decimal.
    assert ssga.annualized_nav_tr_for_period("10y") == pytest.approx(0.1150)
    # Case-insensitive
    assert ssga.annualized_nav_tr_for_period("10Y") == pytest.approx(0.1150)


def test_annualized_nav_tr_unknown_period_raises(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    with pytest.raises(KeyError, match="period '20y' not in SSGA"):
        ssga.annualized_nav_tr_for_period("20y")


def test_dividends_lazy_loaded_and_cached(tmp_path: Path) -> None:
    """Repeated dividends() calls return the same frame instance (no re-read)."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    ssga = SSGASpyReference("spy_ssga_2026-05-28", snapshots_root)
    d1 = ssga.dividends()
    d2 = ssga.dividends()
    assert d1 is d2


def test_reconciliation_delta_bps_positive_when_engine_overstates() -> None:
    """Engine at 10.15%, SSGA at 10.10% = +5 bps overstate."""
    delta = reconciliation_delta_bps(0.1015, 0.1010)
    assert delta == pytest.approx(5.0, abs=1e-9)


def test_reconciliation_delta_bps_negative_when_engine_understates() -> None:
    delta = reconciliation_delta_bps(0.0995, 0.1010)
    assert delta == pytest.approx(-15.0, abs=1e-9)


def test_reconciliation_delta_bps_zero_on_match() -> None:
    delta = reconciliation_delta_bps(0.10, 0.10)
    assert delta == pytest.approx(0.0, abs=1e-12)
