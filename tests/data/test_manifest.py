"""Manifest loader and SHA256 verifier tests.

Schema documented in docs/methodology/dataset_versioning.md.
Synthetic fixture; no Sharadar data required.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from pit_backtest.data.sources.manifest import (
    ManifestParseError,
    SnapshotBundleEntry,
    SnapshotFileEntry,
    SnapshotMismatchError,
    compute_sha256,
    load_manifest,
    verify_bundle,
)


def _write_manifest(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_file(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_compute_sha256_matches_hashlib(tmp_path: Path) -> None:
    """Streaming compute_sha256 produces the same digest as hashlib on the
    full bytes (sanity check on the chunked reader).
    """
    content = b"a" * (3 * 1024 * 1024 + 17)  # >3 chunks plus a partial
    file_path = tmp_path / "blob.bin"
    file_path.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    actual = compute_sha256(file_path)
    assert actual == expected


def test_load_manifest_parses_valid_bundle(tmp_path: Path) -> None:
    """A minimal valid manifest parses into SnapshotBundleEntry."""
    manifest_path = tmp_path / "manifest.toml"
    _write_manifest(
        manifest_path,
        """
[snapshots.sharadar_2026-05-28]
source = "sharadar"
pull_date = 2026-05-28
notes = "Initial pull."

[snapshots.sharadar_2026-05-28.files]
"sep.parquet" = { sha256 = "abc123def456789012345678901234567890123456789012345678901234abcd", size_bytes = 100, row_count = 50 }
""",
    )

    manifest = load_manifest(manifest_path)
    assert list(manifest.keys()) == ["sharadar_2026-05-28"]
    bundle = manifest["sharadar_2026-05-28"]
    assert isinstance(bundle, SnapshotBundleEntry)
    assert bundle.source == "sharadar"
    assert bundle.pull_date == date(2026, 5, 28)
    assert bundle.notes == "Initial pull."
    assert list(bundle.files.keys()) == ["sep.parquet"]

    sep_entry = bundle.files["sep.parquet"]
    assert isinstance(sep_entry, SnapshotFileEntry)
    assert sep_entry.sha256 == "abc123def456789012345678901234567890123456789012345678901234abcd"
    assert sep_entry.size_bytes == 100
    assert sep_entry.row_count == 50


def test_load_manifest_returns_sorted_bundles(tmp_path: Path) -> None:
    """Bundles are returned in sorted order regardless of TOML declaration
    order (determinism invariant requirement 3).
    """
    manifest_path = tmp_path / "manifest.toml"
    _write_manifest(
        manifest_path,
        """
[snapshots.zsharadar_2026-06-01]
source = "sharadar"
pull_date = 2026-06-01

[snapshots.zsharadar_2026-06-01.files]
"a.parquet" = { sha256 = "0000000000000000000000000000000000000000000000000000000000000000", size_bytes = 0 }

[snapshots.asharadar_2026-05-28]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.asharadar_2026-05-28.files]
"a.parquet" = { sha256 = "0000000000000000000000000000000000000000000000000000000000000000", size_bytes = 0 }
""",
    )

    manifest = load_manifest(manifest_path)
    assert list(manifest.keys()) == ["asharadar_2026-05-28", "zsharadar_2026-06-01"]


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="manifest not found"):
        load_manifest(tmp_path / "does_not_exist.toml")


def test_load_manifest_missing_required_field_raises(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.toml"
    _write_manifest(
        manifest_path,
        """
[snapshots.x]
pull_date = 2026-05-28

[snapshots.x.files]
"a.parquet" = { sha256 = "0000000000000000000000000000000000000000000000000000000000000000", size_bytes = 0 }
""",
    )
    with pytest.raises(ManifestParseError, match="missing required field"):
        load_manifest(manifest_path)


def test_load_manifest_rejects_short_sha(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.toml"
    _write_manifest(
        manifest_path,
        """
[snapshots.x]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.x.files]
"a.parquet" = { sha256 = "tooshort", size_bytes = 0 }
""",
    )
    with pytest.raises(ManifestParseError, match="sha256 must be a 64-hex"):
        load_manifest(manifest_path)


def test_verify_bundle_passes_on_match(tmp_path: Path) -> None:
    """A bundle whose files have the manifest SHA256 verifies cleanly."""
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_2026-05-28"
    bundle_dir.mkdir(parents=True)

    content = b"some_sep_parquet_bytes_here"
    sha = _write_file(bundle_dir / "sep.parquet", content)

    manifest = {
        "sharadar_2026-05-28": SnapshotBundleEntry(
            source="sharadar",
            pull_date=date(2026, 5, 28),
            files={
                "sep.parquet": SnapshotFileEntry(
                    sha256=sha, size_bytes=len(content), row_count=None
                )
            },
        )
    }

    # Should not raise.
    verify_bundle("sharadar_2026-05-28", snapshots_root, manifest)


def test_verify_bundle_raises_on_sha_mismatch(tmp_path: Path) -> None:
    """If file content drifts from the manifest, verify raises with
    expected-vs-actual in the message.
    """
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_2026-05-28"
    bundle_dir.mkdir(parents=True)

    (bundle_dir / "sep.parquet").write_bytes(b"actual_content")

    stale_sha = "0" * 64
    manifest = {
        "sharadar_2026-05-28": SnapshotBundleEntry(
            source="sharadar",
            pull_date=date(2026, 5, 28),
            files={
                "sep.parquet": SnapshotFileEntry(
                    sha256=stale_sha, size_bytes=14, row_count=None
                )
            },
        )
    }

    with pytest.raises(SnapshotMismatchError, match="SHA256 mismatch"):
        verify_bundle("sharadar_2026-05-28", snapshots_root, manifest)


def test_verify_bundle_raises_when_file_missing(tmp_path: Path) -> None:
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_2026-05-28"
    bundle_dir.mkdir(parents=True)
    # Note: no file written.

    manifest = {
        "sharadar_2026-05-28": SnapshotBundleEntry(
            source="sharadar",
            pull_date=date(2026, 5, 28),
            files={
                "sep.parquet": SnapshotFileEntry(
                    sha256="0" * 64, size_bytes=0, row_count=None
                )
            },
        )
    }

    with pytest.raises(SnapshotMismatchError, match="missing file"):
        verify_bundle("sharadar_2026-05-28", snapshots_root, manifest)


def test_verify_bundle_raises_when_bundle_dir_missing(tmp_path: Path) -> None:
    snapshots_root = tmp_path / "snapshots"
    snapshots_root.mkdir()
    # No bundle directory created.

    manifest = {
        "sharadar_2026-05-28": SnapshotBundleEntry(
            source="sharadar",
            pull_date=date(2026, 5, 28),
            files={},
        )
    }

    with pytest.raises(SnapshotMismatchError, match="bundle directory not found"):
        verify_bundle("sharadar_2026-05-28", snapshots_root, manifest)


def test_verify_bundle_raises_when_bundle_not_in_manifest(tmp_path: Path) -> None:
    manifest: dict[str, SnapshotBundleEntry] = {}
    with pytest.raises(SnapshotMismatchError, match="not in manifest"):
        verify_bundle("absent_bundle", tmp_path, manifest)
