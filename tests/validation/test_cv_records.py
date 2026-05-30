"""Tests for the Split record (ADR 0015 prep PR 3a).

The splitter bodies (PurgedKFoldSplitter, WalkForwardSplitter,
CPCVSplitter) are still NotImplementedError stubs after this PR;
M4 PR 3b lands the bodies. These tests cover the Split record's
new `test_groups` field per ADR 0015 decision 6 + 7.
"""

from __future__ import annotations

import attrs
import pytest

from pit_backtest.validation.cv import Split


def test_split_carries_five_tuple_fields_including_test_groups() -> None:
    """Per ADR 0015: Split gains test_groups: tuple[int, ...] as the
    fifth tuple field, alongside train/test/purged/embargo indices.
    """
    split = Split(
        train_indices=(0, 1, 2),
        test_indices=(3, 4),
        purged_indices=(),
        embargo_indices=(5,),
        test_groups=(0,),
    )
    assert split.train_indices == (0, 1, 2)
    assert split.test_indices == (3, 4)
    assert split.purged_indices == ()
    assert split.embargo_indices == (5,)
    assert split.test_groups == (0,)


def test_split_is_frozen() -> None:
    """attrs.frozen immutability per the codebase record discipline;
    test_groups inherits the freeze.
    """
    split = Split(
        train_indices=(0,),
        test_indices=(1,),
        purged_indices=(),
        embargo_indices=(),
        test_groups=(0,),
    )
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        split.test_groups = (1,)  # type: ignore[misc]


def test_split_test_groups_walk_forward_empty_tuple() -> None:
    """Per ADR 0015 dec 6: WalkForwardSplitter Split has test_groups=()
    (single-window walk-forward has no group structure).
    """
    split = Split(
        train_indices=(0, 1, 2),
        test_indices=(3, 4, 5),
        purged_indices=(),
        embargo_indices=(),
        test_groups=(),
    )
    assert split.test_groups == ()


def test_split_test_groups_cpcv_k_test_2_two_groups() -> None:
    """Per ADR 0015 dec 6: CPCVSplitter Split has test_groups of length
    k_test, holding the indices of the held-out groups in ascending
    order (matches the sorted itertools.combinations enumeration).
    """
    split = Split(
        train_indices=(0, 1, 2),
        test_indices=(3, 4, 5, 6),
        purged_indices=(),
        embargo_indices=(),
        test_groups=(1, 4),
    )
    assert split.test_groups == (1, 4)
    assert len(split.test_groups) == 2
