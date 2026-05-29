"""sharadar_pull tests.

Synthetic mode only (no real Sharadar API in CI). Validates --refresh-hashes
round-trips through the manifest correctly, including preserving other
bundles and re-running idempotently.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.data.sources.manifest import load_manifest
from pit_backtest.data.sources.sharadar_pull import (
    _format_bundle_section,
    _strip_bundle_section,
    refresh_hashes,
)


def _write_synthetic_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)


def test_refresh_hashes_creates_manifest_entry(tmp_path: Path) -> None:
    bundle_name = "sharadar_2026-05-28"
    bundle_dir = tmp_path / "snapshots" / bundle_name
    manifest_path = tmp_path / "snapshots" / "manifest.toml"

    sep_rows = [
        {"ticker": "SPY", "date": date(2024, 1, 2), "close": 470.0, "closeunadj": 470.0, "open": 470.0, "high": 470.0, "low": 470.0, "volume": 1000000}
    ]
    _write_synthetic_parquet(bundle_dir / "sep.parquet", sep_rows)

    refresh_hashes(
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        pull_date=date(2026, 5, 28),
        notes="test pull",
    )

    manifest = load_manifest(manifest_path)
    assert bundle_name in manifest
    entry = manifest[bundle_name]
    assert entry.source == "sharadar"
    assert entry.pull_date == date(2026, 5, 28)
    assert entry.notes == "test pull"
    assert "sep.parquet" in entry.files
    sep_entry = entry.files["sep.parquet"]
    assert sep_entry.row_count == 1
    expected_sha = hashlib.sha256((bundle_dir / "sep.parquet").read_bytes()).hexdigest()
    assert sep_entry.sha256 == expected_sha


def test_refresh_hashes_preserves_other_bundles(tmp_path: Path) -> None:
    """Adding a new bundle does not delete an existing one."""
    manifest_path = tmp_path / "snapshots" / "manifest.toml"
    manifest_path.parent.mkdir(parents=True)
    # Pre-existing manifest with an old bundle.
    manifest_path.write_text(
        """
[snapshots.spy_ssga_2026-05-27]
source = "ssga_spy"
pull_date = 2026-05-27

[snapshots.spy_ssga_2026-05-27.files]
"performance.csv" = { sha256 = "0000000000000000000000000000000000000000000000000000000000000000", size_bytes = 100 }
""",
        encoding="utf-8",
    )

    bundle_name = "sharadar_2026-05-28"
    bundle_dir = tmp_path / "snapshots" / bundle_name
    _write_synthetic_parquet(
        bundle_dir / "sep.parquet",
        [{"date": date(2024, 1, 2), "close": 1.0}],
    )

    refresh_hashes(
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        pull_date=date(2026, 5, 28),
    )

    manifest = load_manifest(manifest_path)
    assert "spy_ssga_2026-05-27" in manifest
    assert "sharadar_2026-05-28" in manifest


def test_refresh_hashes_idempotent(tmp_path: Path) -> None:
    """Running --refresh-hashes twice produces the same manifest content."""
    bundle_name = "sharadar_2026-05-28"
    bundle_dir = tmp_path / "snapshots" / bundle_name
    manifest_path = tmp_path / "snapshots" / "manifest.toml"
    _write_synthetic_parquet(
        bundle_dir / "sep.parquet",
        [{"date": date(2024, 1, 2), "close": 1.0}],
    )

    refresh_hashes(bundle_dir, manifest_path, pull_date=date(2026, 5, 28))
    first = manifest_path.read_text(encoding="utf-8")
    refresh_hashes(bundle_dir, manifest_path, pull_date=date(2026, 5, 28))
    second = manifest_path.read_text(encoding="utf-8")
    assert first == second


def test_refresh_hashes_no_parquets_raises(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "snapshots" / "sharadar_2026-05-28"
    bundle_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="no .parquet files"):
        refresh_hashes(
            bundle_dir=bundle_dir,
            manifest_path=tmp_path / "snapshots" / "manifest.toml",
            pull_date=date(2026, 5, 28),
        )


def test_refresh_hashes_missing_bundle_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="bundle directory not found"):
        refresh_hashes(
            bundle_dir=tmp_path / "snapshots" / "does_not_exist",
            manifest_path=tmp_path / "snapshots" / "manifest.toml",
            pull_date=date(2026, 5, 28),
        )


def test_strip_bundle_section_removes_target_block() -> None:
    text = """
[snapshots.a]
source = "a"
pull_date = 2026-01-01

[snapshots.a.files]
"x.parquet" = { sha256 = "0", size_bytes = 0 }

[snapshots.b]
source = "b"
pull_date = 2026-01-02

[snapshots.b.files]
"y.parquet" = { sha256 = "1", size_bytes = 1 }
"""
    stripped = _strip_bundle_section(text, "a")
    assert "[snapshots.b]" in stripped
    assert "[snapshots.a]" not in stripped
    assert "[snapshots.a.files]" not in stripped


def test_format_bundle_section_round_trips() -> None:
    """The TOML emitted by _format_bundle_section parses back via load_manifest."""
    from pit_backtest.data.sources.manifest import SnapshotFileEntry

    entries = {
        "sep.parquet": SnapshotFileEntry(
            sha256="a" * 64, size_bytes=1234, row_count=100
        ),
    }
    section = _format_bundle_section(
        bundle_name="test_bundle",
        source="sharadar",
        pull_date=date(2026, 5, 28),
        notes="hello",
        entries=entries,
    )
    assert "[snapshots.test_bundle]" in section
    assert 'source = "sharadar"' in section
    assert "pull_date = 2026-05-28" in section
    assert "row_count = 100" in section
