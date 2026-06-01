"""Runner.run_cpcv body tests (M5 PR 2c, ADR 0016).

The headline result is the CPCV degeneracy for a deterministic factor (ADR
0016 dec 4): the phi(N, k) reconstructed paths COINCIDE because momentum has
no fitted parameter whose retraining could vary them. These tests run the real
momentum backtest (the PitView wiring from PR 2b makes it runnable) over the
synthetic bundle in `_cpcv_momentum_factory`, plus a unit test of the stitch
helper and the Critical-2 registry-isolation test.

The contiguous full-period level reference and the commission seam-cost
artifact (ADR 0016 dec 2) are PR 3 deliverables against the real cost-bearing
bundle: the demos here wire the zero-cost CloseFillMatchingEngine, so there is
no commission seam to assert and no honest contiguous-reference comparison to
make in 2c. See the note on H.5 below.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.engine.runner import Runner, _stitch_path
from pit_backtest.validation.cv import CPCVSplitter, contiguous_folds
from pit_backtest.validation.trial_registry import TrialRegistry
from tests.engine._cpcv_momentum_factory import (
    BUNDLE_NAME,
    MomentumWindowFactory,
    build_observations,
    momentum_rebalance_dates,
    write_momentum_bundle,
)


def _count_trials(db_path: Path, fingerprint: str, family: str) -> int:
    """Direct row count for a (fingerprint, family) partition.

    The isolation test counts rows rather than relying only on the
    (n_effective, v_sr) tuple, because n_effective is registry-instance state
    (the construction-time naive count), not the row count: an unchanged tuple
    is necessary but not sufficient proof of non-pollution.
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM trials "
            "WHERE dataset_fingerprint = ? AND strategy_family = ?",
            (fingerprint, family),
        ).fetchone()
        return int(row[0])
    finally:
        con.close()


def _momentum_setup(
    tmp_path: Path,
) -> tuple[pl.DataFrame, pl.Series, MomentumWindowFactory]:
    root = write_momentum_bundle(tmp_path)
    rebals = momentum_rebalance_dates()
    observations, label_horizons = build_observations(rebals)
    factory = MomentumWindowFactory(
        snapshots_root=str(root), rebalance_dates=rebals
    )
    return observations, label_horizons, factory


# ----- fixture invariant: every group segment is non-flat -----


def test_fixture_every_group_segment_is_non_flat(tmp_path: Path) -> None:
    """The headline degeneracy test depends on every one of the N contiguous
    group windows firing at least one rebalance and producing a non-flat NAV
    segment (a held position whose price moves). If a future fixture change
    flattens a group, this fails loudly with the offending group index instead
    of the headline test hitting an opaque empty-distribution error.
    """
    observations, _label_horizons, factory = _momentum_setup(tmp_path)
    n_obs = observations.height
    dt_values = observations["dt"].to_list()
    for g, (gs, ge) in enumerate(contiguous_folds(n_obs, 6)):
        group_start, group_end = dt_values[gs], dt_values[ge - 1]
        result = factory(group_start, group_end).run(
            start_dt=group_start, end_dt=group_end
        )
        assert result.n_rebalances >= 1, f"group {g} fired no rebalance"
        std = result.equity_curve["nav"].pct_change().drop_nulls().std()
        assert std is not None and std > 0.0, f"group {g} segment is flat"


# ----- H.1 headline: deterministic-factor paths coincide -----


def test_deterministic_factor_paths_coincide(tmp_path: Path) -> None:
    """The phi=5 reconstructed paths are byte-identical for the deterministic
    momentum factor (ADR 0016 dec 4). Asserting p10 == median == p90 (sorted
    ranks 0, 2, 4 of the 5 paths) proves all five coincide: a sorted list whose
    extremes are equal is constant throughout.
    """
    observations, label_horizons, factory = _momentum_setup(tmp_path)
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    registry = TrialRegistry(tmp_path / "trials.db", naive_effective_n=1)
    runner = Runner()
    with pytest.warns(UserWarning, match="below stability threshold"):
        dist = runner.run_cpcv(
            splitter,
            observations,
            label_horizons,
            factory,
            registry=registry,
            strategy_family="momentum_jt1993",
            universe_id="sp500",
        )
    assert dist.path_count == 5
    r10, r50, r90 = dist.p10(), dist.median(), dist.p90()
    assert r10 == r50 == r90
    assert r10.sr_hat == r50.sr_hat == r90.sr_hat
    assert r10.psr == r50.psr == r90.psr
    assert r10.dsr == r50.dsr == r90.dsr


# ----- H.2 cell-partition cross-check + n_groups accessor -----


def test_cell_partition_cross_check_and_n_groups(tmp_path: Path) -> None:
    """The body's internal cell-partition cross-check (each path tiles all N
    groups exactly once) must pass, and the new public n_groups accessor must
    agree with expected_path_count / path_assignments shapes.
    """
    observations, label_horizons, factory = _momentum_setup(tmp_path)
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    assert splitter.n_groups == 6
    assert splitter.expected_path_count() == 5
    path_map = splitter.path_assignments()
    assert len(path_map) == 5
    assert all(len(path) == 6 for path in path_map)

    registry = TrialRegistry(tmp_path / "trials.db", naive_effective_n=1)
    runner = Runner()
    with pytest.warns(UserWarning):
        dist = runner.run_cpcv(
            splitter,
            observations,
            label_horizons,
            factory,
            registry=registry,
            strategy_family="momentum_jt1993",
            universe_id="sp500",
        )
    # The run completing without raising means the body's cell-partition
    # AssertionError gate did not fire.
    assert dist.path_count == 5


# ----- H.3 NaN/flat gate excludes a flat path -----


def test_nan_gate_excludes_flat_path(tmp_path: Path) -> None:
    """A flat factory (empty rebalance set -> no trades -> constant all-cash
    NAV) makes every stitched path flat, so the `_is_flat` pre-check skips all
    five and BacktestPathDistribution raises the empty-paths error. Asserting
    THAT error (not the adapter's flat-curve error) proves the skip branch ran.
    """
    root = write_momentum_bundle(tmp_path)
    observations, label_horizons = build_observations(momentum_rebalance_dates())
    flat_factory = MomentumWindowFactory(
        snapshots_root=str(root), rebalance_dates=()
    )
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    registry = TrialRegistry(tmp_path / "trials.db", naive_effective_n=1)
    runner = Runner()
    with pytest.raises(ValueError, match="at least one path"):
        runner.run_cpcv(
            splitter,
            observations,
            label_horizons,
            flat_factory,
            registry=registry,
            strategy_family="momentum_jt1993",
            universe_id="sp500",
        )


# ----- H.4 stitch helper: running-level carry + injected 0% seam -----


def test_stitch_path_running_level_carry_and_seam() -> None:
    """The stitch carries the running NAV level across the seam so per-group
    growth factors compound, preserves within-group returns, and injects a 0%
    return at the seam (prior group's last NAV == next group's rescaled first).
    Factor 2.0 keeps the arithmetic exact (no float ULP noise).
    """
    seg0 = pl.DataFrame(
        {"dt": [date(2011, 1, 3), date(2011, 1, 4)], "nav": [100.0, 200.0]}
    )
    seg1 = pl.DataFrame(
        {"dt": [date(2011, 2, 1), date(2011, 2, 2)], "nav": [100.0, 150.0]}
    )
    stitched = _stitch_path((0, 1), {0: seg0, 1: seg1}, 100.0)
    # group 0 grows 100 -> 200 (x2); the running level 200 rescales group 1's
    # [100, 150] by 200/100 = 2.0 -> [200, 300]. The seam (rows 1, 2) is the
    # injected 0% return: 200 == 200.
    assert stitched["nav"].to_list() == [100.0, 200.0, 200.0, 300.0]
    assert stitched["nav"][1] == stitched["nav"][2]  # the 0% seam return
    assert stitched["dt"].is_sorted(descending=False)
    assert stitched["dt"].n_unique() == stitched.height  # strictly ascending
    assert stitched.schema["dt"] == pl.Date


# ----- H.5 below-contiguous level reference: DEFERRED to PR 3 -----
# The CPCV per-path level vs a single contiguous full-period backtest is an
# ADR 0016 dec 2 / spec PR-3 deliverable: it needs the real cost-bearing bundle
# (the synthetic fixture wires the zero-cost CloseFillMatchingEngine, so the
# only stitched-vs-contiguous difference here is the omitted inter-group
# gap-day bars, not a commission seam bias). A 2c smoke test asserting
# CPCV <= contiguous would pass for that wrong reason, so it is deliberately
# NOT included here.


# ----- H.6 distribution path_count = 5 and warns -----


def test_returns_distribution_with_path_count_5_and_warns(tmp_path: Path) -> None:
    observations, label_horizons, factory = _momentum_setup(tmp_path)
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    registry = TrialRegistry(tmp_path / "trials.db", naive_effective_n=1)
    runner = Runner()
    with pytest.warns(UserWarning, match="below stability threshold"):
        dist = runner.run_cpcv(
            splitter,
            observations,
            label_horizons,
            factory,
            registry=registry,
            strategy_family="momentum_jt1993",
            universe_id="sp500",
        )
    assert dist.path_count == 5 == splitter.expected_path_count()


# ----- H.7 embargo invariance (purge/embargo inert for a deterministic factor) -----


def test_embargo_invariance(tmp_path: Path) -> None:
    """The body never reads the splits' purge/embargo indices (they touch only
    train folds, which a deterministic factor never runs), so the stitched
    curves and their analytics are invariant to embargo_pct.
    """
    observations, label_horizons, factory = _momentum_setup(tmp_path)
    runner = Runner()

    def _run(embargo_pct: float, family: str, db_name: str):  # type: ignore[no-untyped-def]
        splitter = CPCVSplitter(n_groups=6, k_test=2, embargo_pct=embargo_pct)
        registry = TrialRegistry(tmp_path / db_name, naive_effective_n=1)
        with pytest.warns(UserWarning):
            return runner.run_cpcv(
                splitter,
                observations,
                label_horizons,
                factory,
                registry=registry,
                strategy_family=family,
                universe_id="sp500",
            )

    dist_e0 = _run(0.0, "mom_e0", "e0.db")
    dist_e20 = _run(0.20, "mom_e20", "e20.db")
    assert dist_e0.median() == dist_e20.median()
    assert dist_e0.median().sr_hat == dist_e20.median().sr_hat


# ----- H.8 Critical 2: run_cpcv does not pollute the study DSR family -----


def test_run_cpcv_does_not_pollute_study_family(tmp_path: Path) -> None:
    """The phi-identical path trials would collapse the study family's v_sr to
    ~0 and INFLATE its DSR if they co-mingled. run_cpcv isolates them into a
    `::cpcv_paths` sub-family, so a study registry seeded at naive=2 (the
    genuine multiple-testing case) is provably untouched: same row count, same
    (n_effective, v_sr).
    """
    observations, label_horizons, factory = _momentum_setup(tmp_path)
    db = tmp_path / "study.db"
    registry = TrialRegistry(db, naive_effective_n=2)
    fingerprint = BUNDLE_NAME
    family = "momentum_jt1993"
    # Pre-seed two real study trials so the family has a defined (2, v_sr).
    registry.record(
        dataset_fingerprint=fingerprint, strategy_family=family,
        sr_hat=0.10, t_observations=24, gamma_3=0.0, gamma_4=3.0, metadata={},
    )
    registry.record(
        dataset_fingerprint=fingerprint, strategy_family=family,
        sr_hat=0.20, t_observations=24, gamma_3=0.0, gamma_4=3.0, metadata={},
    )
    before = registry.effective_n_and_sr_variance(fingerprint, family)
    assert _count_trials(db, fingerprint, family) == 2

    splitter = CPCVSplitter(n_groups=6, k_test=2)
    runner = Runner()
    with pytest.warns(UserWarning):
        runner.run_cpcv(
            splitter,
            observations,
            label_horizons,
            factory,
            registry=registry,
            strategy_family=family,
            universe_id="sp500",
        )

    # Study family untouched: row count unchanged AND (n_effective, v_sr) equal.
    assert _count_trials(db, fingerprint, family) == 2
    assert registry.effective_n_and_sr_variance(fingerprint, family) == before
    # The five path trials landed in the disjoint namespaced sub-family.
    assert _count_trials(db, fingerprint, f"{family}::cpcv_paths") == 5
