"""Tests for the M5 figure pipeline (PR 4).

The loader + numeric validators run EVERYWHERE (no matplotlib); only the render
test requires matplotlib (the optional `figures` extra), so a CI without that
extra skips just the thin rendering check. PNG bytes are never asserted (not
reproducible across matplotlib / freetype versions); the artifact's numeric
content is the audited contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from scripts.figures.regenerate import load_artifact, render_all, validate_artifact

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMITTED_ARTIFACT = _REPO_ROOT / "scripts" / "figures" / "study_artifact"
_DATA_FILES = (
    "momentum_equity.json",
    "bootstrap_fan.json",
    "by_year.json",
    "scalars.json",
    "survivorship.json",
)
_HAS_MPL = importlib.util.find_spec("matplotlib") is not None


def _synthetic_artifact(tmp: Path) -> Path:
    """Write a tiny but schema-valid artifact (no bundle, no study run)."""
    art = tmp / "study_artifact"
    art.mkdir()
    (art / "momentum_equity.json").write_text(
        json.dumps(
            {
                "dt": ["2005-01-31", "2005-02-28", "2005-03-31"],
                "nav": [1_000_000.0, 1_010_000.0, 1_005_000.0],
            }
        )
    )
    (art / "bootstrap_fan.json").write_text(
        json.dumps(
            {
                "dt": ["2005-01-31", "2005-02-28", "2005-03-31"],
                "realized": [1.0, 1.01, 1.005],
                "p5": [1.0, 0.98, 0.97],
                "p25": [1.0, 0.99, 0.99],
                "p50": [1.0, 1.0, 1.0],
                "p75": [1.0, 1.01, 1.02],
                "p95": [1.0, 1.03, 1.05],
            }
        )
    )
    (art / "by_year.json").write_text(json.dumps({"2005": 0.05, "2006": -0.02}))
    (art / "survivorship.json").write_text(
        json.dumps(
            {
                "pit_date": "2010-01-04",
                "as_of_date": "2025-01-03",
                "years": 15.0,
                "pit_curve": {
                    "dt": ["2010-01-04", "2017-06-30", "2025-01-03"],
                    "nav": [1.0, 1.1, 1.25],
                },
                "current_curve": {
                    "dt": ["2010-01-04", "2017-06-30", "2025-01-03"],
                    "nav": [1.0, 1.5, 2.0],
                },
                "pit_cohort_mean_terminal_tr": 1.25,
                "current_cohort_mean_terminal_tr": 2.0,
                "pit_cohort_median_terminal_tr": 1.25,
                "current_cohort_median_terminal_tr": 2.0,
                "pit_cohort_cagr": 0.015,
                "current_cohort_cagr": 0.047,
                "cagr_delta_bps": 320.0,
                "pit_count": 2,
                "current_count": 2,
                "survivor_count": 1,
            }
        )
    )
    (art / "scalars.json").write_text(
        json.dumps(
            {
                "sharpe_per_period": 0.05,
                "psr": 0.6,
                "dsr": None,
                "block_length": 3.0,
                "n_rebalances": 3,
                "total_return": 0.005,
                "window_start": "2005-01-31",
                "window_end": "2005-03-31",
            }
        )
    )
    (art / "meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_name": "synthetic",
                "git_sha": "test",
                "n_bootstrap": 10,
                "seed": 1,
                "block_length": 3.0,
                "content_hash": "x",
            }
        )
    )
    return art


def test_load_and_validate_synthetic(tmp_path: Path) -> None:
    payloads = load_artifact(_synthetic_artifact(tmp_path))
    validate_artifact(payloads)  # does not raise
    scalars = payloads["scalars"]
    assert isinstance(scalars, dict)
    assert scalars["dsr"] is None  # a null DSR is a valid, handled value


def test_validate_rejects_non_monotone_fan(tmp_path: Path) -> None:
    art = _synthetic_artifact(tmp_path)
    bad = json.loads((art / "bootstrap_fan.json").read_text())
    bad["p95"][2] = 0.5  # p95 below p75 at month 2
    (art / "bootstrap_fan.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="monotone"):
        validate_artifact(load_artifact(art))


def test_validate_rejects_non_contiguous_years(tmp_path: Path) -> None:
    art = _synthetic_artifact(tmp_path)
    (art / "by_year.json").write_text(json.dumps({"2005": 0.05, "2008": -0.02}))
    with pytest.raises(ValueError, match="contiguous"):
        validate_artifact(load_artifact(art))


def test_validate_rejects_survivorship_terminal_mismatch(tmp_path: Path) -> None:
    """The figure-headline reconciliation invariant: a cohort curve whose
    terminal NAV does not equal the cohort mean terminal TR is rejected."""
    art = _synthetic_artifact(tmp_path)
    bad = json.loads((art / "survivorship.json").read_text())
    bad["pit_curve"]["nav"][-1] = 1.40  # != pit_cohort_mean_terminal_tr (1.25)
    (art / "survivorship.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="terminal NAV"):
        validate_artifact(load_artifact(art))


def test_validate_rejects_survivorship_nav_not_starting_at_one(
    tmp_path: Path,
) -> None:
    art = _synthetic_artifact(tmp_path)
    bad = json.loads((art / "survivorship.json").read_text())
    bad["current_curve"]["nav"][0] = 0.9
    (art / "survivorship.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="start at 1.0"):
        validate_artifact(load_artifact(art))


def test_load_missing_artifact_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_artifact(tmp_path / "does-not-exist")


def test_committed_artifact_loads_and_validates() -> None:
    """The committed real-bundle artifact loads, validates, and its content hash
    matches meta (so a corrupted or stale partial commit is caught)."""
    if not (_COMMITTED_ARTIFACT / "meta.json").is_file():
        pytest.skip("committed study artifact not present")
    payloads = load_artifact(_COMMITTED_ARTIFACT)
    validate_artifact(payloads)
    meta = payloads["meta"]
    assert isinstance(meta, dict)
    digest = hashlib.sha256()
    for name in _DATA_FILES:
        digest.update((_COMMITTED_ARTIFACT / name).read_bytes())
    assert digest.hexdigest() == meta["content_hash"]


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib (the figures extra) not installed")
def test_render_all_produces_pngs(tmp_path: Path) -> None:
    art = _synthetic_artifact(tmp_path)
    out = tmp_path / "figs"
    written = render_all(load_artifact(art), out)
    assert len(written) == 4
    for path in written:
        assert path.is_file() and path.stat().st_size > 0
    assert (out / "survivorship_two_panel.png").is_file()
    assert (out / "PROVENANCE.md").is_file()
