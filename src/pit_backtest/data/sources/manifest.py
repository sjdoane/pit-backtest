"""Snapshot manifest loader and SHA256 verifier.

Loads data/snapshots/manifest.toml; verifies each file's SHA256 against
the manifest entry; raises SnapshotMismatchError on any drift.

Per docs/methodology/dataset_versioning.md, this is the data-layer
instance of the determinism invariant.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Mapping

import attrs


@attrs.frozen(slots=True)
class SnapshotFileEntry:
    """One file's manifest entry."""

    sha256: str
    size_bytes: int
    row_count: int | None  # None for CSV files where row count is not pinned


@attrs.frozen(slots=True)
class SnapshotBundleEntry:
    """One snapshot bundle's manifest entry."""

    source: str
    pull_date: date
    files: Mapping[str, SnapshotFileEntry]
    notes: str = ""


class SnapshotMismatchError(ValueError):
    """Raised when a snapshot file's SHA256 does not match the manifest."""


def load_manifest(manifest_path: Path) -> Mapping[str, SnapshotBundleEntry]:
    """Load and parse data/snapshots/manifest.toml.

    Returns a mapping of bundle_name -> SnapshotBundleEntry. The mapping
    is sorted by bundle_name for determinism.
    """
    raise NotImplementedError("M1 deliverable")


def verify_bundle(
    bundle_name: str, snapshots_root: Path, manifest: Mapping[str, SnapshotBundleEntry]
) -> None:
    """Verify every file in bundle_name matches its manifest SHA256.

    Raises SnapshotMismatchError on any mismatch, with the offending file
    and the expected vs actual hash in the message.
    """
    raise NotImplementedError("M1 deliverable")
