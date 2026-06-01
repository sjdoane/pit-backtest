"""Real-bundle acceptance tests for the snapshot SP500 universe (ADR 0017).

These tests run only when the full survivorship-bias-free S&P 500 bundle is
present on disk at `data/snapshots/sharadar_2026-05-31/`; they are skipped
otherwise (the bundle is gitignored and ~107 MB, so CI without it skips).
They verify the M5 acceptance criteria against REAL Sharadar data (project
rule 6: tests on synthetic fixtures are necessary but not sufficient):

1. The bundle loads contract-clean (all seven data-quality contracts pass
   at `SharadarDataSource.__init__`).
2. `members_at` returns sane S&P 500 sizes (about 500) across 2005-2024.
3. The consumer divergence (ADR 0017 decision 7) is bounded: the fraction
   of certified members with no tradeable ticker at a monthly rebalance is
   small and pervasive (delisting between the quarterly snapshot and the
   rebalance), not a structural defect.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from pit_backtest.data.resolver import TickerNotFoundError
from pit_backtest.data.sources.sharadar import SharadarDataSource

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"
_BUNDLE_NAME = "sharadar_2026-05-31"
_BUNDLE_DIR = _SNAPSHOTS_ROOT / _BUNDLE_NAME

pytestmark = pytest.mark.skipif(
    not _BUNDLE_DIR.is_dir(),
    reason=(
        f"real Sharadar bundle not present at {_BUNDLE_DIR}; "
        "this gated acceptance test runs only with the local data bundle"
    ),
)


def _source() -> SharadarDataSource:
    return SharadarDataSource(_BUNDLE_NAME, _SNAPSHOTS_ROOT)


def test_real_bundle_loads_contract_clean() -> None:
    """Construction runs all seven data-quality contracts; a clean bundle
    raises nothing. This is the headline ADR 0017 acceptance criterion."""
    source = _source()
    assert source.bundle_name == _BUNDLE_NAME


def test_real_bundle_members_at_band_across_2005_to_2024() -> None:
    """Year-end membership is a sane S&P 500 size (about 500). The band is
    [500, 505]; the observed counts on this bundle are 500/500/504/505/503
    for 2005/2010/2015/2020/2024 (the S&P can legitimately reach ~506 during
    multi-class additions; that headroom is documented, not asserted)."""
    source = _source()
    for year in (2005, 2010, 2015, 2020, 2024):
        members = source.members_at("sp500", _dt.datetime(year, 12, 31, 16, 0))
        assert 500 <= len(members) <= 505, (
            f"{year}-12-31 membership {len(members)} outside [500, 505]"
        )
        # AssetIds are unique and sorted (the Universe.members_at contract).
        assert len(set(members)) == len(members)
        assert members == sorted(members, key=int)


def test_real_bundle_consumer_divergence_is_bounded() -> None:
    """ADR 0017 decision 7 / Critical 1: a consumer that resolves each
    member to its tradeable ticker at the rebalance date omits members with
    no price there (delisted between the quarterly snapshot and the monthly
    rebalance). Prove that omission is small and pervasive, not structural:
    over the 240 month-ends 2005-2024 the drop rate is about 0.18% and the
    per-month maximum is single digits."""
    source = _source()
    resolver = source._resolver

    total_obs = 0
    total_dropped = 0
    max_dropped_in_a_month = 0
    months_with_a_drop = 0
    for year in range(2005, 2025):
        for month in range(1, 13):
            if month == 12:
                asof = _dt.date(year, 12, 31)
            else:
                asof = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
            asof_dt = _dt.datetime.combine(asof, _dt.time(16, 0))
            members = source.members_at("sp500", asof_dt)
            dropped = 0
            for asset_id in members:
                try:
                    resolver.get_ticker(asset_id, asof_dt)
                except TickerNotFoundError:
                    dropped += 1
            total_obs += len(members)
            total_dropped += dropped
            max_dropped_in_a_month = max(max_dropped_in_a_month, dropped)
            if dropped:
                months_with_a_drop += 1

    assert total_obs > 100_000  # sanity: ~500 members x 240 months
    drop_rate = total_dropped / total_obs
    # Bounded well under 1%: the divergence is delisting-lag, not structural.
    assert drop_rate < 0.005, f"consumer drop rate {drop_rate:.4%} too high"
    # Pervasive but tiny per month (no single month loses a large slice).
    assert max_dropped_in_a_month <= 12
