"""Data quality contracts that fire at ingest.

Per ADR 0002 decision 12: invariants that vendor data must satisfy before
the engine accepts a snapshot. Failures surface the offending rows.

Contracts (M3 deliverable):
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
