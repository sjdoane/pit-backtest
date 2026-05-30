"""Data quality contracts that fire at ingest plus the PIT lookahead gate.

Per ADR 0002 decision 12: invariants that vendor data must satisfy before
the engine accepts a snapshot. Failures surface the offending rows.

Per ADR 0001 decision 9 (dual-timestamp model): every PIT read must gate
on `available_dt <= simulation_dt`. The `LookaheadLeakError` +
`assert_not_lookahead` helper below is the canonical entry point that
every per-row PitDataSource method calls at the top of its body. Subsequent
M3 PRs wire the helper in; M3 PR 1 ships the helper standalone.

Contracts (M3 PR 2+ deliverable):
- Every TICKERS row has a SEP price within 5 trading days of firstpricedate.
- No SEP bars exist after the delisted date.
- SF1 datekey is non-null for ARQ rows after 1990.
- No duplicate (ticker, datekey) pairs in SF1.
- Every member listed in SP500 has a TICKERS row.

Additional contracts (per ADR 0003 trust boundary #3): user-supplied
additional_data frames must have an available_dt column whose values are
not in the future relative to any period_end_dt in the same frame.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import polars as pl


class DataQualityContract(Protocol):
    """A single data-quality invariant.

    Implementations return None on success and raise DataQualityError with
    the offending rows surfaced on failure.
    """

    name: str

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        """Run the invariant against the loaded frames."""
        ...


class DataQualityError(ValueError):
    """Raised when a data quality contract fails.

    The message includes the contract name and a sample of offending rows
    so the failure is actionable without re-running the check.
    """


class LookaheadLeakError(ValueError):
    """Raised when a PIT read crosses the dual-timestamp boundary.

    Per ADR 0001 decision 9, every PIT-aware adapter method must reject
    a read whose `available_dt` is greater than the `simulation_dt`. This
    is the engine's structural protection against the most common form of
    silent look-ahead bias (using a fundamental field at a date BEFORE the
    filing actually became observable).

    Inherits from ValueError so callers can broad-catch when they wrap a
    pit_view read; named catch is preferred when the recovery path is
    specific to the leak case.
    """


def assert_not_lookahead(
    available_dt: datetime,
    simulation_dt: datetime,
    *,
    context: str,
    period_end_dt: datetime | None = None,
) -> None:
    """Raise LookaheadLeakError when `available_dt > simulation_dt`.

    Per ADR 0001 decision 9 (dual-timestamp model), `available_dt` is when
    the record became observable (SEC submission date for fundamentals;
    bar close for daily price bars). Reading a record with
    `available_dt > simulation_dt` is a look-ahead leak and the engine
    must fail loudly rather than silently return the future value.

    Equal dates (`available_dt == simulation_dt`) are allowed: the record
    became observable at the same moment the simulation is asking for it.

    Per ADR 0002 decision 11, both inputs are interpreted in
    America/New_York; callers must normalize at the adapter boundary
    BEFORE invoking this helper. The helper does NOT inspect tzinfo to
    avoid silently masking a tz mismatch as a date comparison.

    Args:
      available_dt: when the record became observable.
      simulation_dt: the simulation's current time.
      context: a short string naming the call site for the error message
        (for example, `"SharadarDataSource.get_fundamental(asset=42, field='revenue')"`).
        Subsequent M3 PRs pass a context string that surfaces the offending
        asset_id + field + flavor so the failure is actionable.
      period_end_dt: optional, the period_end_dt of the offending row
        (per ADR 0001 decision 9 the dual-timestamp pair). When provided,
        the error message includes both timestamps; a future debug
        session has both halves without re-reading the source frame.

    Raises:
      LookaheadLeakError: when `available_dt > simulation_dt`.

    Usage pattern that future M3 per-row methods follow:

      def get_fundamental(self, asset_id, available_dt, field, flavor):
          assert_not_lookahead(
              available_dt,
              self._simulation_dt,
              context=f"SharadarDataSource.get_fundamental(asset={asset_id}, field={field!r})",
          )
          ...
    """
    if available_dt > simulation_dt:
        period_clause = (
            f" period_end_dt={period_end_dt.isoformat()}"
            if period_end_dt is not None
            else ""
        )
        raise LookaheadLeakError(
            f"lookahead leak in {context}: "
            f"available_dt={available_dt.isoformat()} > "
            f"simulation_dt={simulation_dt.isoformat()}{period_clause}"
        )
