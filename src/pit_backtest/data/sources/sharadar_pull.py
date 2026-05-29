"""Sharadar snapshot pull helper.

Per docs/methodology/dataset_versioning.md, every Sharadar pull produces
a bundle under data/snapshots/sharadar_<YYYY-MM-DD>/ containing the SEP,
ACTIONS, SF1, TICKERS, SP500 parquet files plus a manifest entry with
the SHA256 of each.

This script does two things:

1. `--refresh-hashes`: scan an existing bundle directory, compute SHA256
   + size + row_count for each parquet, and write/update the manifest
   entry for the bundle. This is the workflow when Sam has manually
   downloaded the parquets (via the Nasdaq Data Link web UI, the
   `nasdaq-data-link` Python SDK, or curl) and just needs the hashes
   committed.

2. `--download`: attempt to download via the Nasdaq Data Link bulk-export
   API. Requires SHARADAR_API_KEY in env. The bulk-export endpoint is
   asynchronous (request -> poll -> download URL), so the implementation
   is best-effort and Sam should verify the result against the Sharadar
   web UI before committing.

The script is intentionally a thin layer over the SHA256 + manifest
update so the manifest commitment can land via a tiny commit even when
the data is in flux.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import polars as pl

from pit_backtest.data.sources.manifest import (
    SnapshotFileEntry,
    compute_sha256,
)
from pit_backtest.utils.logging import configure_logging, get_logger


_log = get_logger("pit_backtest.data.sources.sharadar_pull")


# Sharadar table -> parquet filename. The script supports the M1+M3 table
# inventory from docs/methodology/dataset_versioning.md.
_TABLE_FILENAMES: dict[str, str] = {
    "SEP": "sep.parquet",
    "ACTIONS": "actions.parquet",
    "SF1": "sf1.parquet",
    "TICKERS": "tickers.parquet",
    "SP500": "sp500.parquet",
}


def refresh_hashes(
    bundle_dir: Path, manifest_path: Path, pull_date: date, notes: str = ""
) -> None:
    """Recompute SHA256 + size + row_count for every parquet in bundle_dir
    and write/update the manifest entry.

    The bundle name is derived from bundle_dir.name. The manifest is
    edited in place: existing entries for OTHER bundles are preserved;
    the entry for this bundle is fully rewritten.
    """
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle directory not found: {bundle_dir}")

    bundle_name = bundle_dir.name
    # Hash every data file (.parquet for Sharadar; .xlsx / .csv for SSGA-
    # native exports; .pdf provenance kept locally but skipped here).
    # See docs/vendor/nasdaq-data-link-pull.md for the supported shapes.
    hashable_suffixes = {".parquet", ".xlsx", ".csv"}
    data_files = sorted(
        p for p in bundle_dir.iterdir() if p.suffix.lower() in hashable_suffixes
    )
    if not data_files:
        raise FileNotFoundError(
            f"no .parquet/.xlsx/.csv files in {bundle_dir}; download data first or run "
            f"with --download"
        )

    entries: dict[str, SnapshotFileEntry] = {}
    for path in data_files:
        sha = compute_sha256(path)
        size = path.stat().st_size
        # row_count is parquet-only (cheap via Polars scan); XLSX/CSV are
        # variable-row-count vendor exports and the manifest schema allows
        # row_count=None for them.
        row_count: int | None
        if path.suffix.lower() == ".parquet":
            row_count = int(
                pl.scan_parquet(path).select(pl.len()).collect()[0, 0]
            )
        else:
            row_count = None
        entries[path.name] = SnapshotFileEntry(
            sha256=sha, size_bytes=size, row_count=row_count
        )
        _log.info(
            "computed_hash",
            extra={
                "file": path.name,
                "sha256_short": sha[:12],
                "size_mb": f"{size / 1024 / 1024:.2f}",
                "row_count": row_count if row_count is not None else "n/a",
            },
        )

    _rewrite_manifest_entry(
        manifest_path=manifest_path,
        bundle_name=bundle_name,
        source="sharadar",
        pull_date=pull_date,
        notes=notes,
        entries=entries,
    )
    _log.info(
        "manifest_updated",
        extra={
            "bundle": bundle_name,
            "files": len(entries),
            "manifest": str(manifest_path),
        },
    )


def _rewrite_manifest_entry(
    manifest_path: Path,
    bundle_name: str,
    source: str,
    pull_date: date,
    notes: str,
    entries: dict[str, SnapshotFileEntry],
) -> None:
    """Insert or replace the manifest entry for bundle_name.

    For M1 we use a line-oriented rewrite: read the file, find the
    `[snapshots.<bundle_name>]` section if present and drop it, append
    the new section at the end. TOML round-trip via tomllib + write is
    not supported (tomllib is read-only); tomli-w is the standard
    write-side library but adding a dep for a chore script is overkill.
    """
    existing_text = ""
    if manifest_path.is_file():
        existing_text = manifest_path.read_text(encoding="utf-8")

    new_text = _strip_bundle_section(existing_text, bundle_name).rstrip() + "\n\n"
    new_text += _format_bundle_section(
        bundle_name=bundle_name,
        source=source,
        pull_date=pull_date,
        notes=notes,
        entries=entries,
    )
    manifest_path.write_text(new_text, encoding="utf-8")


def _strip_bundle_section(text: str, bundle_name: str) -> str:
    """Remove [snapshots.<bundle_name>] and [snapshots.<bundle_name>.files]
    blocks from a TOML text.

    Block-aware: drops every line from `[snapshots.<bundle_name>` until
    the next top-level `[snapshots.` block or end of file. Conservative
    when the file has unrelated content; only the named bundle is removed.
    """
    keep_lines: list[str] = []
    section_prefix = f"[snapshots.{bundle_name}"
    in_target_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(section_prefix):
            in_target_section = True
            continue
        if in_target_section:
            if stripped.startswith("[") and not stripped.startswith(section_prefix):
                in_target_section = False
                keep_lines.append(line)
            # Otherwise: inside target section, skip the line.
            continue
        keep_lines.append(line)
    return "\n".join(keep_lines)


def _format_bundle_section(
    bundle_name: str,
    source: str,
    pull_date: date,
    notes: str,
    entries: dict[str, SnapshotFileEntry],
) -> str:
    """Render one TOML bundle block. Hand-rolled to avoid adding tomli-w
    as a dependency for a chore script.
    """
    lines = [
        f"[snapshots.{bundle_name}]",
        f'source = "{source}"',
        f"pull_date = {pull_date.isoformat()}",
    ]
    if notes:
        escaped = notes.replace('"', '\\"')
        lines.append(f'notes = "{escaped}"')
    lines.append("")
    lines.append(f"[snapshots.{bundle_name}.files]")
    for filename in sorted(entries.keys()):
        entry = entries[filename]
        rc = "" if entry.row_count is None else f", row_count = {entry.row_count}"
        lines.append(
            f'"{filename}" = {{ sha256 = "{entry.sha256}", '
            f"size_bytes = {entry.size_bytes}{rc} }}"
        )
    lines.append("")
    return "\n".join(lines)


def attempt_download(
    bundle_dir: Path,
    api_key: str,
    tables: Iterable[str] = ("SEP", "ACTIONS", "SF1", "TICKERS", "SP500"),
) -> None:
    """Best-effort download via Nasdaq Data Link bulk-export API.

    This is intentionally minimal: it issues the bulk-export request and
    reports the polling URL Sam should hit. The async download semantics
    of Nasdaq Data Link's export endpoint make a full client overkill for
    the M1 chore. Sam can use the nasdaq-data-link Python SDK directly
    for an interactive pull; this function exists to capture the URL
    pattern for posterity.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    _log.warning(
        "download_not_implemented",
        extra={
            "note": (
                "M1 ships --refresh-hashes only. Use the nasdaq-data-link Python "
                "SDK or the Nasdaq Data Link web UI to download parquets into "
                f"{bundle_dir}; then re-run with --refresh-hashes to commit the "
                "manifest entry."
            ),
            "api_key_fingerprint": _fingerprint(api_key),
            "tables_requested": ",".join(tables),
        },
    )


def _fingerprint(api_key: str) -> str:
    """Last 4 hex chars of SHA256 of the API key.

    Per docs/methodology/dataset_versioning.md, recorded to identify
    WHO pulled without exposing the key.
    """
    import hashlib

    return "fp_" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[-4:]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sharadar snapshot pull helper: hash + manifest, with "
        "best-effort download.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--bundle",
        type=str,
        required=True,
        help="Bundle name (e.g., sharadar_2026-05-28). Directory expected at "
        "data/snapshots/<bundle>/.",
    )
    parser.add_argument(
        "--snapshots-root",
        type=Path,
        default=Path("data/snapshots"),
        help="Root containing the manifest.toml and bundle subdirectories.",
    )
    parser.add_argument(
        "--pull-date",
        type=date.fromisoformat,
        default=None,
        help="Pull date for the manifest entry. Defaults to today.",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default="",
        help="Free-text note recorded in the manifest entry.",
    )
    parser.add_argument(
        "--refresh-hashes",
        action="store_true",
        help="Compute SHA256 + size + row_count for every .parquet in the "
        "bundle and write the manifest entry.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Best-effort Sharadar download via SHARADAR_API_KEY env var. "
        "M1 prints guidance only; actual download deferred until Sam needs it.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    bundle_dir = args.snapshots_root / args.bundle
    manifest_path = args.snapshots_root / "manifest.toml"
    pull_date = args.pull_date or datetime.now().date()

    if args.download:
        # The official SDK env var is NASDAQ_DATA_LINK_API_KEY; the
        # legacy SHARADAR_API_KEY is kept for backwards compat. See
        # docs/vendor/nasdaq-data-link-pull.md.
        api_key = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get(
            "SHARADAR_API_KEY"
        )
        if not api_key:
            _log.error(
                "missing_api_key",
                extra={"env_var": "NASDAQ_DATA_LINK_API_KEY or SHARADAR_API_KEY"},
            )
            print(
                "neither NASDAQ_DATA_LINK_API_KEY nor SHARADAR_API_KEY env var "
                "is set; cannot attempt download",
                file=sys.stderr,
            )
            return 2
        attempt_download(bundle_dir=bundle_dir, api_key=api_key)

    if args.refresh_hashes:
        try:
            refresh_hashes(
                bundle_dir=bundle_dir,
                manifest_path=manifest_path,
                pull_date=pull_date,
                notes=args.notes,
            )
        except FileNotFoundError as e:
            _log.error("refresh_failed", extra={"reason": str(e)})
            print(str(e), file=sys.stderr)
            return 2

    if not args.download and not args.refresh_hashes:
        print(
            "no action requested; pass --refresh-hashes or --download",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
