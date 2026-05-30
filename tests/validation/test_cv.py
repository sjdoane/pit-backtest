"""Tests for validation.cv splitter bodies (M4 PR 3b against ADR 0015).

Acceptance fixtures are hand-pinnable. The canonical PurgedKFold fixture
is T=20 obs, k=5, embargo_pct=0.05 (embargo_count = floor(20 * 0.05) = 1):
  dt = 2024-01-01..20; label_horizons[i] = dt[i] (zero-day horizon).
  Folds (remainder-front, here exactly even): (0,4),(4,8),(8,12),(12,16),(16,20).
  Fold 0 (test 0..3, t_end=3): purged={}, embargo={4}, train={5..19}.
  Fold 4 (test 16..19, t_end=19): embargo window (19, 20] is out of range,
    so embargo={}, train={0..15}.

The CPCV acceptance fixture is N=6, k=2 -> phi = (2/6) * C(6,2) = 5 paths
across C(6,2) = 15 combinations (ADR 0002 decision 2).
"""

from __future__ import annotations

import itertools
from datetime import date, datetime

import polars as pl
import pytest

from pit_backtest.validation.cv import (
    CPCVSplitter,
    PurgedKFoldSplitter,
    Split,
    WalkForwardSplitter,
    _contiguous_folds,
)


def _obs(n: int) -> pl.DataFrame:
    """n daily observations 2024-01-01.. with a sorted dt column."""
    return pl.DataFrame({"dt": [date(2024, 1, i + 1) for i in range(n)]})


def _zero_horizon(n: int) -> pl.Series:
    """label_horizons[i] = dt[i] (label realized same bar; no overlap)."""
    return pl.Series("h", [date(2024, 1, i + 1) for i in range(n)])


def _k_day_horizon(n: int, k: int) -> pl.Series:
    """label_horizons[i] = dt[min(i + k, n - 1)] (k-bar forward label)."""
    return pl.Series(
        "h", [date(2024, 1, min(i + k, n - 1) + 1) for i in range(n)]
    )


# ----- _contiguous_folds convention (Plan-reviewer M2) -----


def test_contiguous_folds_even_division() -> None:
    assert _contiguous_folds(20, 5) == ((0, 4), (4, 8), (8, 12), (12, 16), (16, 20))


def test_contiguous_folds_remainder_front_matches_numpy_array_split() -> None:
    """n=11, k=3: numpy.array_split puts the larger chunks first."""
    assert _contiguous_folds(11, 3) == ((0, 4), (4, 8), (8, 11))


# ----- PurgedKFoldSplitter acceptance -----


def test_purged_kfold_acceptance_fold_0_embargo_one() -> None:
    splitter = PurgedKFoldSplitter(k=5, embargo_pct=0.05)
    splits = list(splitter.split(_obs(20), _zero_horizon(20)))
    assert len(splits) == 5
    fold0 = splits[0]
    assert fold0.test_indices == (0, 1, 2, 3)
    assert fold0.purged_indices == ()
    assert fold0.embargo_indices == (4,)
    assert fold0.train_indices == tuple(range(5, 20))
    assert fold0.test_groups == (0,)


def test_purged_kfold_acceptance_last_fold_embargo_out_of_range() -> None:
    splitter = PurgedKFoldSplitter(k=5, embargo_pct=0.05)
    splits = list(splitter.split(_obs(20), _zero_horizon(20)))
    fold4 = splits[4]
    assert fold4.test_indices == (16, 17, 18, 19)
    assert fold4.embargo_indices == ()  # (19, 20] is out of range
    assert fold4.purged_indices == ()
    assert fold4.train_indices == tuple(range(0, 16))
    assert fold4.test_groups == (4,)


def test_purged_kfold_purges_overlapping_forward_horizon() -> None:
    """T=20, k=5, embargo_pct=0 (isolate purge), 3-bar forward horizon.
    Fold 1 (test 4..7, t_start index 4 -> dt 2024-01-05): obs 1, 2, 3 have
    labels ending at dt[4], dt[5], dt[6] (>= 2024-01-05) so they are purged;
    obs 0's label ends at dt[3]=2024-01-04 < 2024-01-05 so it survives.
    """
    splitter = PurgedKFoldSplitter(k=5, embargo_pct=0.0)
    splits = list(splitter.split(_obs(20), _k_day_horizon(20, 3)))
    fold1 = splits[1]
    assert fold1.test_indices == (4, 5, 6, 7)
    assert fold1.purged_indices == (1, 2, 3)
    assert fold1.embargo_indices == ()
    assert 0 in fold1.train_indices
    assert 1 not in fold1.train_indices


def test_purged_kfold_partition_is_exhaustive_and_disjoint() -> None:
    """train + test + purged + embargo cover range(n) with no overlap."""
    splitter = PurgedKFoldSplitter(k=4, embargo_pct=0.1)
    for split in splitter.split(_obs(20), _k_day_horizon(20, 2)):
        groups = [
            set(split.train_indices),
            set(split.test_indices),
            set(split.purged_indices),
            set(split.embargo_indices),
        ]
        union: set[int] = set()
        for g in groups:
            assert union.isdisjoint(g)
            union |= g
        assert union == set(range(20))


def test_purged_kfold_non_divisible_folds_remainder_front() -> None:
    """T=11, k=3: fold sizes are 4, 4, 3 (remainder-front)."""
    splitter = PurgedKFoldSplitter(k=3, embargo_pct=0.0)
    splits = list(splitter.split(_obs(11), _zero_horizon(11)))
    assert splits[0].test_indices == (0, 1, 2, 3)
    assert splits[1].test_indices == (4, 5, 6, 7)
    assert splits[2].test_indices == (8, 9, 10)


def test_purged_kfold_weekend_gap_uses_calendar_dates() -> None:
    """Per Plan-reviewer M3: the purge is date-based, not index-based, so a
    calendar gap between observations is respected. Obs at indices [0,1,2]
    on 2024-01-01/02/03; index 3 jumps to 2024-01-31 (a gap). With a label
    horizon that ends 2024-01-05 for index 0, a test fold starting at index 3
    (2024-01-31) does NOT purge index 0 because its label (ending 01-05) does
    not reach the test window starting 01-31.
    """
    obs = pl.DataFrame(
        {
            "dt": [
                date(2024, 1, 1),
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 31),
                date(2024, 2, 1),
                date(2024, 2, 2),
            ]
        }
    )
    # index 0 label ends 01-05; others zero-horizon.
    horizons = pl.Series(
        "h",
        [
            date(2024, 1, 5),
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 31),
            date(2024, 2, 1),
            date(2024, 2, 2),
        ],
    )
    splitter = PurgedKFoldSplitter(k=2, embargo_pct=0.0)
    splits = list(splitter.split(obs, horizons))
    # Fold 1 tests indices 3,4,5 (dt 01-31..02-02). Index 0's label ends
    # 01-05, well before 01-31, so index 0 is NOT purged.
    fold1 = splits[1]
    assert fold1.test_indices == (3, 4, 5)
    assert 0 not in fold1.purged_indices
    assert 0 in fold1.train_indices


# ----- PurgedKFoldSplitter domain violations -----


def test_purged_kfold_init_raises_on_k_below_two() -> None:
    with pytest.raises(ValueError, match="k >= 2"):
        PurgedKFoldSplitter(k=1)


def test_purged_kfold_init_raises_on_embargo_out_of_range() -> None:
    with pytest.raises(ValueError, match="embargo_pct"):
        PurgedKFoldSplitter(k=5, embargo_pct=-0.1)
    with pytest.raises(ValueError, match="embargo_pct"):
        PurgedKFoldSplitter(k=5, embargo_pct=1.0)


def test_purged_kfold_split_raises_when_height_below_k() -> None:
    splitter = PurgedKFoldSplitter(k=10)
    with pytest.raises(ValueError, match="height"):
        list(splitter.split(_obs(5), _zero_horizon(5)))


def test_purged_kfold_split_raises_on_missing_dt_column() -> None:
    splitter = PurgedKFoldSplitter(k=2)
    bad = pl.DataFrame({"value": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError, match="'dt' column"):
        list(splitter.split(bad, pl.Series("h", [date(2024, 1, 1)] * 3)))


def test_purged_kfold_split_raises_on_unsorted_dt() -> None:
    splitter = PurgedKFoldSplitter(k=2)
    unsorted = pl.DataFrame(
        {"dt": [date(2024, 1, 3), date(2024, 1, 1), date(2024, 1, 2)]}
    )
    with pytest.raises(ValueError, match="sorted"):
        list(splitter.split(unsorted, _zero_horizon(3)))


def test_purged_kfold_split_raises_on_horizon_length_mismatch() -> None:
    splitter = PurgedKFoldSplitter(k=2)
    with pytest.raises(ValueError, match="length"):
        list(splitter.split(_obs(10), _zero_horizon(8)))


def test_purged_kfold_split_raises_on_null_horizon() -> None:
    splitter = PurgedKFoldSplitter(k=2)
    horizons = pl.Series(
        "h", [date(2024, 1, 1), None, date(2024, 1, 3)], dtype=pl.Date
    )
    with pytest.raises(ValueError, match="non-null"):
        list(splitter.split(_obs(3), horizons))


def test_purged_kfold_split_raises_on_dtype_mismatch() -> None:
    """dt is Date, label_horizons is Datetime -> raises (avoids the
    date-vs-datetime comparison trap).
    """
    splitter = PurgedKFoldSplitter(k=2)
    horizons = pl.Series(
        "h", [datetime(2024, 1, i + 1) for i in range(3)], dtype=pl.Datetime
    )
    with pytest.raises(ValueError, match="dtype"):
        list(splitter.split(_obs(3), horizons))


# ----- WalkForwardSplitter -----


def test_walk_forward_single_split() -> None:
    splitter = WalkForwardSplitter(
        train_end=datetime(2024, 1, 11), test_start=datetime(2024, 1, 11)
    )
    splits = list(splitter.split(_obs(20), _zero_horizon(20)))
    assert len(splits) == 1
    split = splits[0]
    # dt[i] = 2024-01-(i+1); train < 01-11 -> indices 0..9; test >= 01-11 -> 10..19.
    assert split.train_indices == tuple(range(0, 10))
    assert split.test_indices == tuple(range(10, 20))
    assert split.purged_indices == ()
    assert split.embargo_indices == ()
    assert split.test_groups == ()


def test_walk_forward_init_raises_when_train_end_after_test_start() -> None:
    with pytest.raises(ValueError, match="train_end <= test_start"):
        WalkForwardSplitter(
            train_end=datetime(2024, 3, 1), test_start=datetime(2024, 2, 1)
        )


def test_walk_forward_validates_horizon_length_only() -> None:
    """Per Plan-reviewer M1: length parity is enforced; values are not read."""
    splitter = WalkForwardSplitter(
        train_end=datetime(2024, 1, 11), test_start=datetime(2024, 1, 11)
    )
    with pytest.raises(ValueError, match="length"):
        list(splitter.split(_obs(20), _zero_horizon(8)))


def test_walk_forward_ignores_horizon_values_including_nulls() -> None:
    """A null-laden horizon series of the right LENGTH is accepted (values
    are never read for the single-window baseline).
    """
    splitter = WalkForwardSplitter(
        train_end=datetime(2024, 1, 6), test_start=datetime(2024, 1, 6)
    )
    null_horizons = pl.Series("h", [None] * 10, dtype=pl.Date)
    splits = list(splitter.split(_obs(10), null_horizons))
    assert len(splits) == 1
    assert splits[0].train_indices == (0, 1, 2, 3, 4)
    assert splits[0].test_indices == (5, 6, 7, 8, 9)


# ----- CPCVSplitter -----


def test_cpcv_n6_k2_yields_5_paths_and_15_combinations() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    assert splitter.expected_path_count() == 5
    assert len(splitter.path_assignments()) == 5
    splits = list(splitter.split(_obs(18), _zero_horizon(18)))
    assert len(splits) == 15  # C(6, 2)


def test_cpcv_path_assignments_each_path_spans_all_groups() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    assignments = splitter.path_assignments()
    for path in assignments:
        assert len(path) == 6  # one combination index per group


def test_cpcv_path_assignments_each_combination_appears_k_times() -> None:
    """Plan-reviewer C2 invariant: each of the C(N,k) combinations appears
    exactly k_test times across the whole path-assignment map (it tests
    k_test groups, contributing one cell to k_test paths).
    """
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    assignments = splitter.path_assignments()
    counts: dict[int, int] = {}
    for path in assignments:
        for combo_idx in path:
            counts[combo_idx] = counts.get(combo_idx, 0) + 1
    assert set(counts.keys()) == set(range(15))  # all 15 combinations used
    assert all(c == 2 for c in counts.values())  # each appears k_test=2 times


def test_cpcv_path_assignments_cell_partition_exhaustive_and_consistent() -> None:
    """Post-impl reviewer Medium 1: the load-bearing invariant is the
    (combination, group) test-cell partition, not just the per-combination
    count. Every (combo_idx, group) cell where the combination tests that
    group must be consumed EXACTLY once across all paths, and every path
    position g must read a combination that genuinely tests group g.
    """
    n_groups, k_test = 6, 2
    splitter = CPCVSplitter(n_groups=n_groups, k_test=k_test)
    combinations = list(itertools.combinations(range(n_groups), k_test))
    expected_cells = {
        (combo_idx, g)
        for combo_idx, combo in enumerate(combinations)
        for g in combo
    }
    consumed: list[tuple[int, int]] = []
    for path in splitter.path_assignments():
        for g, combo_idx in enumerate(path):
            # each path position g reads a combination that tests group g
            assert g in combinations[combo_idx]
            consumed.append((combo_idx, g))
    assert len(consumed) == len(set(consumed))  # each cell consumed once
    assert set(consumed) == expected_cells  # all cells consumed, none extra


def test_cpcv_test_groups_match_combination() -> None:
    """Each Split's test_groups is the held-out combination in ascending order."""
    splitter = CPCVSplitter(n_groups=4, k_test=2)
    splits = list(splitter.split(_obs(12), _zero_horizon(12)))
    expected_combos = [
        (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3),
    ]
    assert [s.test_groups for s in splits] == expected_combos


def test_cpcv_partition_is_exhaustive_and_disjoint() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2, embargo_pct=0.05)
    for split in splitter.split(_obs(18), _k_day_horizon(18, 1)):
        groups = [
            set(split.train_indices),
            set(split.test_indices),
            set(split.purged_indices),
            set(split.embargo_indices),
        ]
        union: set[int] = set()
        for g in groups:
            assert union.isdisjoint(g)
            union |= g
        assert union == set(range(18))


def test_cpcv_test_indices_span_two_groups() -> None:
    """k_test=2 -> each Split holds out two contiguous group blocks."""
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    splits = list(splitter.split(_obs(18), _zero_horizon(18)))
    # groups of 18/6 = 3 each: g0={0,1,2}, g1={3,4,5}, ...
    # First combo (0,1) -> test {0,1,2,3,4,5}.
    assert splits[0].test_indices == (0, 1, 2, 3, 4, 5)
    assert splits[0].test_groups == (0, 1)


@pytest.mark.parametrize(
    "n_groups,k_test,expected",
    [
        (6, 2, 5),
        (4, 2, 3),
        (7, 3, 15),
        (10, 4, 84),
    ],
)
def test_cpcv_expected_path_count_regression(
    n_groups: int, k_test: int, expected: int
) -> None:
    """Plan-reviewer H3: pin phi(N,k) across several cells so an
    off-by-floor-division regression is caught. phi = (k/N) * C(N,k).
    """
    assert CPCVSplitter(n_groups, k_test).expected_path_count() == expected


def test_cpcv_walk_forward_degenerate_equivalence_single_combination() -> None:
    """CPCV(N, k=N-1) leaves one group as the train fold per combination;
    N combinations, each testing N-1 groups. A sanity check that the
    combination count is C(N, N-1) = N.
    """
    splitter = CPCVSplitter(n_groups=4, k_test=3)
    splits = list(splitter.split(_obs(12), _zero_horizon(12)))
    assert len(splits) == 4  # C(4, 3)


# ----- CPCVSplitter domain violations -----


def test_cpcv_init_raises_on_n_groups_below_two() -> None:
    with pytest.raises(ValueError, match="n_groups >= 2"):
        CPCVSplitter(n_groups=1, k_test=1)


def test_cpcv_init_raises_on_k_test_not_below_n_groups() -> None:
    with pytest.raises(ValueError, match="k_test < n_groups"):
        CPCVSplitter(n_groups=6, k_test=6)


def test_cpcv_init_raises_on_k_test_below_one() -> None:
    with pytest.raises(ValueError, match="k_test"):
        CPCVSplitter(n_groups=6, k_test=0)


def test_cpcv_split_raises_when_height_below_n_groups() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    with pytest.raises(ValueError, match="n_groups"):
        list(splitter.split(_obs(5), _zero_horizon(5)))


def test_cpcv_split_raises_on_null_horizon() -> None:
    splitter = CPCVSplitter(n_groups=2, k_test=1)
    horizons = pl.Series(
        "h", [date(2024, 1, 1), None, date(2024, 1, 3)], dtype=pl.Date
    )
    with pytest.raises(ValueError, match="non-null"):
        list(splitter.split(_obs(3), horizons))


# ----- Determinism -----


def test_cpcv_split_deterministic_across_calls() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    a = list(splitter.split(_obs(18), _zero_horizon(18)))
    b = list(splitter.split(_obs(18), _zero_horizon(18)))
    assert a == b


def test_cpcv_path_assignments_deterministic_across_calls() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2)
    assert splitter.path_assignments() == splitter.path_assignments()


def test_all_split_index_tuples_sorted_ascending() -> None:
    splitter = CPCVSplitter(n_groups=6, k_test=2, embargo_pct=0.05)
    for split in splitter.split(_obs(18), _k_day_horizon(18, 1)):
        assert split.train_indices == tuple(sorted(split.train_indices))
        assert split.test_indices == tuple(sorted(split.test_indices))
        assert split.purged_indices == tuple(sorted(split.purged_indices))
        assert split.embargo_indices == tuple(sorted(split.embargo_indices))
        assert split.test_groups == tuple(sorted(split.test_groups))


def test_split_records_are_frozen() -> None:
    split = Split(
        train_indices=(0,),
        test_indices=(1,),
        purged_indices=(),
        embargo_indices=(),
        test_groups=(0,),
    )
    import attrs

    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        split.train_indices = (9,)  # type: ignore[misc]
