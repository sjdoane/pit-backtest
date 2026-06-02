"""Real-bundle gated test for the M5 PR 3b momentum study.

Gated on the survivorship-bias-free Sharadar S&P 500 bundle being present (like
tests/data/test_real_sp500_bundle.py), so CI without the bundle skips it (the
only CI workflow is perf-budget). A full 2005-2024 study run is ~11-15 min, so
the default gated test runs a short (2015-2016) window that exercises the whole
composition (contiguous level reference + CPCV degeneracy + the monthly
block-bootstrap fan + the honest DSR conclusion) in a few minutes; the full
window is an env-flagged opt-in. A separate FAST assertion pins the
survivorship-universe size over the full calendar with no backtest.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from examples.sp500_momentum_study import (
    MomentumStudyRecipe,
    build_ever_member_union,
    compute_momentum_study_report,
    momentum_rebalance_dates,
)
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"
_BUNDLE_NAME = "sharadar_2026-05-31"
_BUNDLE_DIR = _SNAPSHOTS_ROOT / _BUNDLE_NAME

pytestmark = pytest.mark.skipif(
    not _BUNDLE_DIR.is_dir(),
    reason=f"real Sharadar bundle not present at {_BUNDLE_DIR}; gated study test",
)


def _source_and_universe() -> tuple[SharadarDataSource, SharadarSP500Universe]:
    source = SharadarDataSource(_BUNDLE_NAME, _SNAPSHOTS_ROOT)
    return source, SharadarSP500Universe(source)


def _recipe_for(
    universe: SharadarSP500Universe, start: date, end: date
) -> MomentumStudyRecipe:
    rebalance_dates = momentum_rebalance_dates(start, end)
    union = build_ever_member_union(universe, rebalance_dates)
    return MomentumStudyRecipe(
        snapshots_root=str(_SNAPSHOTS_ROOT),
        bundle_name=_BUNDLE_NAME,
        start_dt=rebalance_dates[0],
        end_dt=end,
        initial_capital=1_000_000.0,
        rebalance_dates=rebalance_dates,
        union_tickers=tuple(int(a) for a in union),
    )


def test_ever_member_union_size_full_calendar() -> None:
    """FAST (no backtest): the survivorship-safe ever-member union over the full
    240-rebalance 2005-2024 calendar is ~930 (the probed size), pinning that the
    BarLoop's universe is the union of all in-window members, not the ~500 final
    roster.
    """
    _source, universe = _source_and_universe()
    rebalance_dates = momentum_rebalance_dates(date(2005, 1, 4), date(2024, 12, 31))
    assert len(rebalance_dates) == 240
    union = build_ever_member_union(universe, rebalance_dates)
    assert 900 <= len(union) <= 960  # anchored at the probed 930


def test_momentum_study_short_window() -> None:
    """The whole composition wires against the real bundle over a short window:
    the report renders, the DSR degenerates to PSR (naive=1), the 5 CPCV paths
    COINCIDE (the deterministic-factor degeneracy), the bootstrap block length is
    data-chosen, and the universe coverage is the S&P 500 size. ~3 minutes.
    """
    _source, universe = _source_and_universe()
    recipe = _recipe_for(universe, date(2015, 1, 2), date(2016, 12, 30))
    assert len(recipe.rebalance_dates) >= 12  # enough for CPCV N=6 (>= 2/group)

    report = compute_momentum_study_report(
        recipe, universe, n_bootstrap=200, seed=20260601
    )

    # Composition wired + the honest conclusion present.
    assert report.markdown.strip()
    assert "Honest DSR conclusion" in report.markdown
    assert "stationary block bootstrap" in report.markdown
    # naive=1 single-strategy: DSR is the PSR-against-zero (no deflation).
    assert report.result.dsr is not None
    assert report.result.dsr == report.result.psr
    # CPCV degeneracy: the 5 reconstructed paths coincide for a deterministic
    # factor (ADR 0016 dec 4), so the per-path Sharpe dispersion is ~0.
    assert report.cpcv_path_count == 5
    assert report.cpcv_sr_max - report.cpcv_sr_min < 1e-9
    # Bootstrap block length is a real (> 1.0) data-chosen value.
    assert report.block_length > 1.0
    assert report.n_bootstrap == 200
    # Universe coverage is the S&P 500 (members ~500-505; union over the short
    # window strictly larger than any single-rebalance member count).
    assert 495 <= report.member_count_min <= report.member_count_max <= 515
    assert report.union_size >= report.member_count_max


@pytest.mark.skipif(
    not os.environ.get("PIT_RUN_FULL_M5_STUDY"),
    reason="full 2005-2024 study is ~11-15 min; set PIT_RUN_FULL_M5_STUDY=1 to run",
)
def test_momentum_study_full_window_opt_in() -> None:
    """The full 2005-2024 study, opt-in via PIT_RUN_FULL_M5_STUDY=1 (it never
    runs by default even with the bundle present). Asserts the full study runs
    without error and renders the honest conclusion.
    """
    _source, universe = _source_and_universe()
    recipe = _recipe_for(universe, date(2005, 1, 4), date(2024, 12, 31))
    assert len(recipe.rebalance_dates) == 240

    report = compute_momentum_study_report(
        recipe, universe, n_bootstrap=1000, seed=20260601
    )
    assert report.markdown.strip()
    assert report.result.dsr is not None
    assert report.cpcv_path_count == 5
    assert report.cpcv_sr_max - report.cpcv_sr_min < 1e-9
    assert 900 <= report.union_size <= 960
