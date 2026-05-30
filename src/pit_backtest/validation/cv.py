"""CVSplitter, PurgedKFoldSplitter, WalkForwardSplitter, CPCVSplitter.

Per ADR 0001 decision 3: CPCV is primary; walk-forward is a CPCV
configuration with one path. Per ADR 0002 decision 17: WalkForwardSplitter
ships alongside as a sanity-check baseline (the original stub mis-cited
ADR 0003 decision 17; that decision is "Single-currency USD assumption"
and ADR 0003 itself cross-references ADR 0002 dec 17 at line 583; the
attribution was corrected by ADR 0015).

Algorithm fidelity (LdP 2018 *Advances in Financial Machine Learning*
chapters 7 + 12; pseudocode reconstructed in
`docs/research/sources/methodology-afml-backtesting.md:132-180`):

- **Purge** removes a training observation whose label interval
  `[dt[i], label_horizons[i]]` overlaps the test interval
  `[dt[t_start], dt[t_end]]`. This module uses calendar-DATE comparison
  (`dt[i] <= dt[t_end] and label_horizons[i] >= dt[t_start]`), a
  deliberate divergence from the AFML pseudocode's INDEX form
  (`i + h_i >= t_start`): the date form correctly handles non-uniform
  observation spacing (weekends, holidays) where index distance and
  calendar distance differ.
- **Embargo** removes training observations in the half-open interval
  `(t_end, t_end + embargo_count]` (open at `t_end`, closed at the
  upper bound), matching the AFML pseudocode boundary verbatim under
  the 1-based -> 0-based index shift. `embargo_count = floor(T * embargo_pct)`.
- **CPCV** holds out all `C(N, k)` combinations of `k_test` groups out
  of `N`, producing `phi(N, k) = (k/N) * C(N, k)` per-path backtests.
  The `path_assignments()` map stitches per-combination test predictions
  into per-path equity curves (see its docstring for the construction).

Determinism (per `docs/methodology/determinism.md`): every index tuple
is sorted ascending; combination enumeration follows
`itertools.combinations` (lexicographic); observations must be sorted
by `dt` ascending (enforced loudly).
"""

from __future__ import annotations

import itertools
import math
from datetime import datetime
from typing import Iterator, Protocol

import attrs
import polars as pl


_DATE_DTYPES = (pl.Date, pl.Datetime)


@attrs.frozen(slots=True)
class Split:
    """A single train/test split produced by a CVSplitter.

    Per ADR 0015 the `test_groups` field carries the group indices that
    the `test_indices` chunks came from, so the future
    `Runner.run_cpcv` body can stitch per-fold test predictions into
    per-path equity curves without re-deriving the group membership
    from the Split's index sets.

    Per-splitter semantics:
      - `PurgedKFoldSplitter`: `test_groups` is a length-1 tuple
        `(fold_index,)`.
      - `WalkForwardSplitter`: `test_groups` is the empty tuple `()`
        (single-window walk-forward has no group structure per ADR 0002
        dec 17's "sanity-check baseline" framing).
      - `CPCVSplitter`: `test_groups` is a length-`k_test` tuple of the
        held-out group indices in ascending order (matching the
        `itertools.combinations` enumeration).

    The `train_indices`, `test_indices`, `purged_indices`, and
    `embargo_indices` tuples partition `range(n_obs)` (disjoint and, for
    PurgedKFold/CPCV, exhaustive). `test_groups` is independently sorted
    ascending; it is NOT a subset of `test_indices` so a regression that
    permutes the combination iteration order surfaces as a test_groups
    ordering failure.
    """

    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]
    embargo_indices: tuple[int, ...]
    test_groups: tuple[int, ...]


class CVSplitter(Protocol):
    """Cross-validation splitter on time-ordered observations."""

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        """Yield one Split per fold (or per CPCV combination)."""
        ...


def _require_sorted_dt_column(
    observations: pl.DataFrame, fn_name: str
) -> None:
    """Validate that observations carries a sorted `dt` date-like column.

    Per ADR 0013 dec 7 loud-failure discipline: every domain violation
    raises ValueError with the offending value.
    """
    if "dt" not in observations.columns:
        raise ValueError(
            f"{fn_name} requires observations to carry a 'dt' column "
            f"(pl.Date or pl.Datetime); got columns {observations.columns}"
        )
    dt_dtype = observations.schema["dt"]
    if dt_dtype not in _DATE_DTYPES:
        raise ValueError(
            f"{fn_name} requires observations['dt'] to be pl.Date or "
            f"pl.Datetime; got {dt_dtype}"
        )
    if observations.height > 0 and not observations["dt"].is_sorted():
        raise ValueError(
            f"{fn_name} requires observations sorted by 'dt' ascending; "
            f"the contiguous-fold and purge logic assume time ordering"
        )


def _require_label_horizons(
    observations: pl.DataFrame, label_horizons: pl.Series, fn_name: str
) -> None:
    """Validate label_horizons shape + dtype + null-freeness + dt-dtype match."""
    if label_horizons.len() != observations.height:
        raise ValueError(
            f"{fn_name} requires label_horizons length == observations "
            f"height; got {label_horizons.len()} vs {observations.height}"
        )
    if label_horizons.dtype not in _DATE_DTYPES:
        raise ValueError(
            f"{fn_name} requires label_horizons to be pl.Date or "
            f"pl.Datetime; got {label_horizons.dtype}"
        )
    if label_horizons.dtype != observations.schema["dt"]:
        raise ValueError(
            f"{fn_name} requires label_horizons dtype to match "
            f"observations['dt'] dtype (avoids the date-vs-datetime "
            f"comparison trap); got {label_horizons.dtype} vs "
            f"{observations.schema['dt']}"
        )
    if label_horizons.null_count() > 0:
        raise ValueError(
            f"{fn_name} requires label_horizons to be non-null; got "
            f"{label_horizons.null_count()} null entries"
        )


def _contiguous_folds(n_obs: int, k: int) -> tuple[tuple[int, int], ...]:
    """Partition range(n_obs) into k contiguous (start, end_exclusive) folds.

    Remainder-front convention matching `numpy.array_split`: the first
    `n_obs % k` folds receive one extra element. E.g., n_obs=11, k=3 ->
    ((0, 4), (4, 8), (8, 11)). The convention is pinned by a test so a
    refactor to remainder-back cannot silently shift every purge boundary.
    """
    base, remainder = divmod(n_obs, k)
    folds: list[tuple[int, int]] = []
    start = 0
    for j in range(k):
        size = base + (1 if j < remainder else 0)
        folds.append((start, start + size))
        start += size
    return tuple(folds)


def _embargo_count(n_obs: int, embargo_pct: float) -> int:
    """floor(n_obs * embargo_pct) per AFML pseudocode line 137."""
    return math.floor(n_obs * embargo_pct)


def _purged_indices_for_block(
    train_candidates: set[int],
    t_start: int,
    t_end: int,
    dt_values: list[object],
    horizon_values: list[object],
) -> set[int]:
    """Training obs whose label interval overlaps the test block.

    Overlap predicate (calendar-date form): obs `i` is purged when
    `dt[i] <= dt[t_end]` AND `label_horizons[i] >= dt[t_start]`. For an
    obs after the block (`dt[i] > dt[t_end]`) the first clause is False,
    so only the embargo (not purge) removes after-block leakage.
    """
    test_start_dt = dt_values[t_start]
    test_end_dt = dt_values[t_end]
    purged: set[int] = set()
    for i in train_candidates:
        if dt_values[i] <= test_end_dt and horizon_values[i] >= test_start_dt:  # type: ignore[operator]
            purged.add(i)
    return purged


def _embargo_indices_for_block(
    train_candidates: set[int], t_end: int, embargo_count: int
) -> set[int]:
    """Training obs in the half-open interval (t_end, t_end + embargo_count]."""
    embargoed: set[int] = set()
    for offset in range(1, embargo_count + 1):
        idx = t_end + offset
        if idx in train_candidates:
            embargoed.add(idx)
    return embargoed


class PurgedKFoldSplitter(CVSplitter):
    """LdP chapter 7 purged k-fold with embargo.

    `k` contiguous test folds (remainder-front sizing); each fold's
    training set is the complement minus purged minus embargoed obs.
    """

    def __init__(self, k: int, embargo_pct: float = 0.05) -> None:
        if k < 2:
            raise ValueError(f"PurgedKFoldSplitter requires k >= 2; got k={k}")
        if not (0.0 <= embargo_pct < 1.0):
            raise ValueError(
                f"PurgedKFoldSplitter requires 0 <= embargo_pct < 1; "
                f"got embargo_pct={embargo_pct}"
            )
        self._k = k
        self._embargo_pct = embargo_pct

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        _require_sorted_dt_column(observations, "PurgedKFoldSplitter.split")
        _require_label_horizons(
            observations, label_horizons, "PurgedKFoldSplitter.split"
        )
        n_obs = observations.height
        if n_obs < self._k:
            raise ValueError(
                f"PurgedKFoldSplitter.split requires observations.height "
                f">= k; got height={n_obs}, k={self._k}"
            )
        dt_values = observations["dt"].to_list()
        horizon_values = label_horizons.to_list()
        embargo_count = _embargo_count(n_obs, self._embargo_pct)
        folds = _contiguous_folds(n_obs, self._k)
        all_indices = set(range(n_obs))
        for fold_idx, (start, end) in enumerate(folds):
            test_indices = set(range(start, end))
            t_start, t_end = start, end - 1
            train_candidates = all_indices - test_indices
            purged = _purged_indices_for_block(
                train_candidates, t_start, t_end, dt_values, horizon_values
            )
            embargoed = (
                _embargo_indices_for_block(
                    train_candidates, t_end, embargo_count
                )
                - purged
            )
            train = train_candidates - purged - embargoed
            yield Split(
                train_indices=tuple(sorted(train)),
                test_indices=tuple(sorted(test_indices)),
                purged_indices=tuple(sorted(purged)),
                embargo_indices=tuple(sorted(embargoed)),
                test_groups=(fold_idx,),
            )


class WalkForwardSplitter(CVSplitter):
    """Single-path baseline; per ADR 0002 decision 17 catches a class of
    CPCV implementation bugs.

    Yields exactly one Split: training obs strictly before `train_end`,
    test obs at or after `test_start`. No purge or embargo (the
    single-window baseline does not model label-horizon leakage). Per
    the M4 PR 3 Plan-reviewer Medium 1, `label_horizons` is validated
    for LENGTH parity only (so a caller cannot pass a mismatched-shape
    series) but its values are NOT read; full dtype/null validation
    would invent a non-functional dependency.

    Unlike `PurgedKFoldSplitter` and `CPCVSplitter`, this splitter has no
    minimum-height floor: a single window legitimately can have an empty
    train side (all observations at or after `test_start`) or an empty
    test side (all observations before `train_end`), and height-0
    observations yield one Split with empty train and test tuples rather
    than raising. Downstream consumers treat an empty-test Split as a
    no-op path (post-impl reviewer Medium 2).
    """

    def __init__(self, train_end: datetime, test_start: datetime) -> None:
        if train_end > test_start:
            raise ValueError(
                f"WalkForwardSplitter requires train_end <= test_start; "
                f"got train_end={train_end}, test_start={test_start}"
            )
        self._train_end = train_end
        self._test_start = test_start

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        _require_sorted_dt_column(observations, "WalkForwardSplitter.split")
        if label_horizons.len() != observations.height:
            raise ValueError(
                f"WalkForwardSplitter.split requires label_horizons length "
                f"== observations height; got {label_horizons.len()} vs "
                f"{observations.height}"
            )
        dt_dtype = observations.schema["dt"]
        train_bound = pl.lit(self._train_end).cast(dt_dtype)
        test_bound = pl.lit(self._test_start).cast(dt_dtype)
        with_idx = observations.with_row_index("__cv_idx")
        train_indices = tuple(
            int(i)
            for i in with_idx.filter(pl.col("dt") < train_bound)[
                "__cv_idx"
            ].to_list()
        )
        test_indices = tuple(
            int(i)
            for i in with_idx.filter(pl.col("dt") >= test_bound)[
                "__cv_idx"
            ].to_list()
        )
        yield Split(
            train_indices=train_indices,
            test_indices=test_indices,
            purged_indices=(),
            embargo_indices=(),
            test_groups=(),
        )


def _combinatorial_test_groups(
    n_groups: int, k_test: int
) -> tuple[tuple[int, ...], ...]:
    """All C(n_groups, k_test) combinations in lexicographic order.

    `itertools.combinations(range(n), k)` yields sorted tuples in
    lexicographic order, so each combination tuple is ascending and the
    enumeration order is deterministic.
    """
    return tuple(itertools.combinations(range(n_groups), k_test))


class CPCVSplitter(CVSplitter):
    """Combinatorial Purged Cross-Validation.

    Produces phi(N, k) = (k/N) * C(N, k) paths. Default N=6, k=2 gives 5
    paths; the acceptance criterion in ADR 0002 decision 2 is N=6, k=2.

    `split()` yields one Split per combination (C(N, k) of them).
    `path_assignments()` and `expected_path_count()` are the extra
    methods (beyond the CVSplitter Protocol) that the future
    `Runner.run_cpcv` body uses to stitch per-combination test
    predictions into phi(N, k) per-path equity curves.
    """

    def __init__(
        self, n_groups: int, k_test: int, embargo_pct: float = 0.05
    ) -> None:
        if n_groups < 2:
            raise ValueError(
                f"CPCVSplitter requires n_groups >= 2; got n_groups={n_groups}"
            )
        if not (1 <= k_test < n_groups):
            raise ValueError(
                f"CPCVSplitter requires 1 <= k_test < n_groups; got "
                f"k_test={k_test}, n_groups={n_groups}"
            )
        if not (0.0 <= embargo_pct < 1.0):
            raise ValueError(
                f"CPCVSplitter requires 0 <= embargo_pct < 1; got "
                f"embargo_pct={embargo_pct}"
            )
        self._n_groups = n_groups
        self._k_test = k_test
        self._embargo_pct = embargo_pct

    def expected_path_count(self) -> int:
        """phi(N, k) = (k / N) * C(N, k), computed as integer division.

        The result is ALWAYS an integer: the absorption identity
        `k * C(N, k) = N * C(N-1, k-1)` guarantees N divides
        `k * C(N, k)` exactly. Pinned by a regression test across
        multiple (N, k) cells.
        """
        return (
            self._k_test * math.comb(self._n_groups, self._k_test)
        ) // self._n_groups

    def path_assignments(self) -> tuple[tuple[int, ...], ...]:
        """Map each of phi(N, k) paths to one combination index per group.

        Returns a tuple of length `expected_path_count()`. Element `j`
        (path `j`) is a length-`N` tuple where position `g` holds the
        combination index whose test set includes group `g` and which is
        assigned to path `j`.

        Construction (LdP 2018 ch 7): for each group `g`, collect the
        ascending list of combination indices that test `g`; there are
        exactly `C(N-1, k-1) == phi(N, k)` of them (the absorption
        identity again). Path `j` takes the `j`-th such combination for
        every group.

        Invariants (pinned by tests):
          - `len(result) == expected_path_count()`.
          - every path tuple has length `n_groups`.
          - every combination index appears exactly `k_test` times across
            the whole map (each combination tests `k_test` groups, so it
            supplies one cell to `k_test` different paths).
        """
        combinations = _combinatorial_test_groups(self._n_groups, self._k_test)
        per_group: list[list[int]] = [[] for _ in range(self._n_groups)]
        for combo_idx, combo in enumerate(combinations):
            for g in combo:
                per_group[g].append(combo_idx)
        n_paths = self.expected_path_count()
        paths: list[tuple[int, ...]] = []
        for j in range(n_paths):
            paths.append(tuple(per_group[g][j] for g in range(self._n_groups)))
        return tuple(paths)

    def split(
        self, observations: pl.DataFrame, label_horizons: pl.Series
    ) -> Iterator[Split]:
        _require_sorted_dt_column(observations, "CPCVSplitter.split")
        _require_label_horizons(
            observations, label_horizons, "CPCVSplitter.split"
        )
        n_obs = observations.height
        if n_obs < self._n_groups:
            raise ValueError(
                f"CPCVSplitter.split requires observations.height >= "
                f"n_groups; got height={n_obs}, n_groups={self._n_groups}"
            )
        dt_values = observations["dt"].to_list()
        horizon_values = label_horizons.to_list()
        embargo_count = _embargo_count(n_obs, self._embargo_pct)
        groups = _contiguous_folds(n_obs, self._n_groups)
        all_indices = set(range(n_obs))
        for combo in _combinatorial_test_groups(self._n_groups, self._k_test):
            test_indices: set[int] = set()
            for g in combo:
                gs, ge = groups[g]
                test_indices |= set(range(gs, ge))
            train_candidates = all_indices - test_indices
            purged: set[int] = set()
            embargoed: set[int] = set()
            for g in combo:
                gs, ge = groups[g]
                purged |= _purged_indices_for_block(
                    train_candidates, gs, ge - 1, dt_values, horizon_values
                )
            for g in combo:
                gs, ge = groups[g]
                embargoed |= _embargo_indices_for_block(
                    train_candidates, ge - 1, embargo_count
                )
            embargoed -= purged
            train = train_candidates - purged - embargoed
            yield Split(
                train_indices=tuple(sorted(train)),
                test_indices=tuple(sorted(test_indices)),
                purged_indices=tuple(sorted(purged)),
                embargo_indices=tuple(sorted(embargoed)),
                test_groups=tuple(combo),
            )
