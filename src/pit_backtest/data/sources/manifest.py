"""Snapshot manifest loader and SHA256 verifier.

Loads data/snapshots/manifest.toml; verifies each file's SHA256 against
the manifest entry; raises SnapshotMismatchError on any drift.

Per docs/methodology/dataset_versioning.md, this is the data-layer
instance of the determinism invariant.
"""

from __future__ import annotations

import hashlib
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import attrs

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


# Read parquet/CSV files in 1 MiB chunks. SHA256 is streaming so memory
# usage is constant; the chunk size affects only the number of read syscalls.
_HASH_READ_CHUNK_BYTES = 1024 * 1024


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


class ManifestParseError(ValueError):
    """Raised when manifest.toml does not match the expected schema."""


def load_manifest(manifest_path: Path) -> Mapping[str, SnapshotBundleEntry]:
    """Load and parse data/snapshots/manifest.toml.

    Returns an ordered mapping of bundle_name -> SnapshotBundleEntry, sorted
    by bundle_name for determinism (per the determinism invariant's
    sorted-output-frames requirement, applied here at the manifest API).
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")

    with manifest_path.open("rb") as fh:
        raw = tomllib.load(fh)

    snapshots_raw = raw.get("snapshots", {})
    if not isinstance(snapshots_raw, dict):
        raise ManifestParseError(
            f"manifest [snapshots] must be a table; got {type(snapshots_raw).__name__}"
        )

    bundles: OrderedDict[str, SnapshotBundleEntry] = OrderedDict()
    for name in sorted(snapshots_raw.keys()):
        bundles[name] = _parse_bundle_entry(name, snapshots_raw[name])

    return bundles


def verify_bundle(
    bundle_name: str,
    snapshots_root: Path,
    manifest: Mapping[str, SnapshotBundleEntry],
) -> None:
    """Verify every file in bundle_name matches its manifest SHA256.

    snapshots_root is the directory that contains the bundle subdirectory
    (i.e., data/snapshots/, not data/snapshots/<bundle>/).

    Raises SnapshotMismatchError on any mismatch, with the offending file
    and the expected vs actual hash in the message. Iterates files in
    sorted order so the first failure is deterministic.
    """
    if bundle_name not in manifest:
        raise SnapshotMismatchError(
            f"bundle '{bundle_name}' not in manifest; available: "
            f"{sorted(manifest.keys())}"
        )

    bundle = manifest[bundle_name]
    bundle_dir = snapshots_root / bundle_name

    if not bundle_dir.is_dir():
        raise SnapshotMismatchError(
            f"bundle directory not found: {bundle_dir}"
        )

    for filename in sorted(bundle.files.keys()):
        entry = bundle.files[filename]
        file_path = bundle_dir / filename
        if not file_path.is_file():
            raise SnapshotMismatchError(
                f"bundle '{bundle_name}' missing file: {file_path}"
            )

        actual_sha = compute_sha256(file_path)
        if actual_sha != entry.sha256:
            raise SnapshotMismatchError(
                f"SHA256 mismatch for {bundle_name}/{filename}:\n"
                f"  expected: {entry.sha256}\n"
                f"  actual:   {actual_sha}\n"
                f"vendor data has shifted since the manifest was last updated. "
                f"Refresh the snapshot per docs/methodology/dataset_versioning.md."
            )

        actual_size = file_path.stat().st_size
        if actual_size != entry.size_bytes:
            raise SnapshotMismatchError(
                f"size mismatch for {bundle_name}/{filename}: "
                f"expected {entry.size_bytes} bytes, actual {actual_size}. "
                f"SHA256 matched but size differs, which should be impossible "
                f"for a deterministic hash; check for manifest corruption."
            )


def compute_sha256(file_path: Path) -> str:
    """Stream-compute the SHA256 of a file.

    Returns the lowercase hex digest. Reads in fixed-size chunks so memory
    usage is constant regardless of file size; used both at manifest-update
    time and at verify time.
    """
    h = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_READ_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_bundle_entry(name: str, raw: Any) -> SnapshotBundleEntry:
    if not isinstance(raw, dict):
        raise ManifestParseError(
            f"bundle '{name}' must be a table; got {type(raw).__name__}"
        )

    try:
        source = raw["source"]
        pull_date = raw["pull_date"]
    except KeyError as missing:
        raise ManifestParseError(
            f"bundle '{name}' missing required field {missing}"
        ) from missing

    if not isinstance(source, str):
        raise ManifestParseError(
            f"bundle '{name}' source must be a string; got {type(source).__name__}"
        )
    if not isinstance(pull_date, date):
        raise ManifestParseError(
            f"bundle '{name}' pull_date must be a TOML date; "
            f"got {type(pull_date).__name__}"
        )

    files_raw = raw.get("files", {})
    if not isinstance(files_raw, dict):
        raise ManifestParseError(
            f"bundle '{name}' files must be a table; got {type(files_raw).__name__}"
        )

    files: OrderedDict[str, SnapshotFileEntry] = OrderedDict()
    for filename in sorted(files_raw.keys()):
        files[filename] = _parse_file_entry(name, filename, files_raw[filename])

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise ManifestParseError(
            f"bundle '{name}' notes must be a string; got {type(notes).__name__}"
        )

    return SnapshotBundleEntry(
        source=source,
        pull_date=pull_date,
        files=files,
        notes=notes,
    )


def _parse_file_entry(
    bundle_name: str, filename: str, raw: Any
) -> SnapshotFileEntry:
    if not isinstance(raw, dict):
        raise ManifestParseError(
            f"file '{bundle_name}/{filename}' must be a table; "
            f"got {type(raw).__name__}"
        )

    try:
        sha256 = raw["sha256"]
        size_bytes = raw["size_bytes"]
    except KeyError as missing:
        raise ManifestParseError(
            f"file '{bundle_name}/{filename}' missing required field {missing}"
        ) from missing

    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ManifestParseError(
            f"file '{bundle_name}/{filename}' sha256 must be a 64-hex string; "
            f"got {sha256!r}"
        )
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise ManifestParseError(
            f"file '{bundle_name}/{filename}' size_bytes must be a non-negative integer; "
            f"got {size_bytes!r}"
        )

    row_count = raw.get("row_count")
    if row_count is not None and (not isinstance(row_count, int) or row_count < 0):
        raise ManifestParseError(
            f"file '{bundle_name}/{filename}' row_count must be a non-negative integer or absent; "
            f"got {row_count!r}"
        )

    return SnapshotFileEntry(
        sha256=sha256,
        size_bytes=size_bytes,
        row_count=row_count,
    )
