"""BacktestPathDistribution: container for CPCV path-distributed results.

Per ADR 0001 decision 3 and ADR 0003 architecture: any single-Sharpe
API on CPCV is a correctness bug. The distribution exposes percentiles,
medians, and aggregation methods; an aggregate mean is intentionally
NOT exposed because reporting `mean(per_path_sr)` as a scalar is the
anti-pattern ADR 0001 dec 4 calls out.

Implementation notes (per the M4 PR 2 Plan-reviewer):
- **Medium 5**: TypeVar `T` is intentionally UNBOUNDED. The
  Plan-reviewer rejected the original plan's `runtime_checkable
  Protocol Comparable` as over-engineered; bounding to `float` was the
  proposed alternative but the existing `engine/runner.py:92` stub
  declares `run_cpcv -> BacktestPathDistribution[BacktestResult]` so a
  `bound=float` would break the engine surface at mypy time. The
  unbounded TypeVar matches the original scaffold's posture and defers
  the sortability decision (per-path Sharpe scalars vs per-path
  BacktestResult with `__lt__` defined vs an explicit `sort_key`
  callable) to M4 PR 3 where `run_cpcv` ships its real body. The
  runtime sort at `percentiles` raises `TypeError` if `T` lacks
  `__lt__`; the caller's contract is to construct with a sortable `T`.
- **Medium 6**: `__init__` raises on empty paths (consistent with the
  codebase loud-failure discipline) rather than the prior warning-only
  posture. Sparse `path_count < 30` continues to warn.
- **High 2**: percentile algorithm is nearest-rank via `math.ceil`,
  defended on first principles (deterministic across version bumps;
  matches `scipy.stats.scoreatpercentile(interpolation_method='lower')`
  conservative-tail convention; no IEEE-754 interpolation noise at
  small CPCV path counts). The original plan attributed nearest-rank
  to "LdP 2018 ch. 13 convention"; grep verified zero such citation in
  the methodology research note, so the rationale is rewritten here
  rather than perpetuated.
"""

from __future__ import annotations

import math
import warnings
from typing import Final, Generic, TypeVar


T = TypeVar("T")

_MIN_STABLE_PATH_COUNT: Final[int] = 30


class BacktestPathDistribution(Generic[T]):
    """Container for the multiple paths produced by CPCV.

    Constructed once per backtest at the Runner boundary; consumed by
    the M4 PR 5 scorecard renderer + the M5 worked-study fan chart.

    Construction-time checks:
      - Empty `paths` raises `ValueError` per M4 PR 2 Plan-reviewer
        Medium 6 (an empty distribution is mathematically incoherent;
        deferring the raise to method-call time was the original plan's
        API-hostile behavior).
      - `path_count < _MIN_STABLE_PATH_COUNT` (30) warns at
        construction; below this, the per-path-Sharpe distribution is
        too noisy to rank confidently (the stability threshold from
        ADR 0001 reviewer pass).
      - NaN guard: fires only for `T = float` (the v1 use case of
        per-path Sharpe scalars). For non-float `T` (e.g., a
        `BacktestResult` Pydantic model in M4 PR 3), the caller must
        run its own NaN gate before construction. Per M4 PR 2 post-impl
        reviewer High 2 this contract obligation is loaded onto M4 PR 3
        rather than silently accepted here.

    All percentile methods are pure functions of the sorted `paths`
    list; no mutation of `self._paths` ever occurs.
    """

    def __init__(self, paths: list[T], path_count: int) -> None:
        if not paths:
            raise ValueError(
                "BacktestPathDistribution requires at least one path; "
                "got an empty list"
            )
        # NaN check fires only for T = float (the v1 use case of per-path
        # Sharpe scalars). For other T, M4 PR 3 owns its own NaN gate
        # before construction. Per M4 PR 2 post-impl reviewer High 2 this
        # converts the known soundness gap into a contract obligation on
        # the caller, rather than pretending the check is comprehensive.
        for p in paths:
            if isinstance(p, float) and math.isnan(p):
                raise ValueError(
                    "BacktestPathDistribution received NaN paths; sort "
                    "order is undefined under IEEE-754. Inspect the "
                    "upstream Runner output"
                )
        self._paths = paths
        self.path_count = path_count
        if path_count < _MIN_STABLE_PATH_COUNT:
            warnings.warn(
                f"CPCV path count {path_count} below stability threshold "
                f"({_MIN_STABLE_PATH_COUNT}); distribution statistics may "
                f"be noisy.",
                stacklevel=2,
            )

    def percentiles(self, percentiles: list[float]) -> dict[float, T]:
        """Nearest-rank percentile lookup.

        For each `p` in `percentiles`, returns the path at rank
        `max(1, ceil(p / 100 * n))` of the sorted paths list. The
        nearest-rank convention is deterministic across float-arithmetic
        version bumps and matches `scipy.stats`'s
        `interpolation_method='lower'` (the conservative-tail choice
        for risk reporting).

        Raises:
          ValueError: when any `p` in the input list is outside
            `[0, 100]`. The error message lists every offending value
            so a caller passing a malformed list sees all the failures
            at once.
        """
        invalid = [p for p in percentiles if not (0.0 <= p <= 100.0)]
        if invalid:
            raise ValueError(
                f"percentiles requires every value in [0, 100]; "
                f"got out-of-range: {invalid}"
            )
        # The unbounded TypeVar requires us to bypass mypy's sortable
        # check here; the caller's contract is to construct with a
        # sortable T. M4 PR 3 picks the per-path emission shape.
        # TODO(M4 PR 3): when the per-path emission type is fixed, swap
        # the unbounded TypeVar for either `bound=float` or a
        # `bound=SupportsRichComparison` Protocol and remove this ignore.
        sorted_paths = sorted(self._paths)  # type: ignore[type-var]
        n = len(sorted_paths)
        result: dict[float, T] = {}
        for p in percentiles:
            rank = max(1, math.ceil((p / 100.0) * n))
            result[p] = sorted_paths[rank - 1]
        return result

    def median(self) -> T:
        """50th percentile (p50). Convenience wrapper around `percentiles`."""
        return self.percentiles([50.0])[50.0]

    def p10(self) -> T:
        """10th percentile. Convenience wrapper around `percentiles`."""
        return self.percentiles([10.0])[10.0]

    def p90(self) -> T:
        """90th percentile. Convenience wrapper around `percentiles`."""
        return self.percentiles([90.0])[90.0]
