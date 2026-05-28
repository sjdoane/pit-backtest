"""SQLite WAL-backed trial registry.

Per ADR 0002 decision 19: single-machine concurrent (multiple notebooks
plus pytest workers); WAL mode; serialized writes. Per ADR 0003 decision 21:
PCA-based effective N is opt-in for N>=50; default is
naive_effective_n = number_of_independent_strategy_families supplied by
the user at construction; with N<30 PCA raises InsufficientTrialsForPCAError.
"""

from __future__ import annotations

from pathlib import Path


class TrialRegistry:
    """Persistent trial registry feeding DSR."""

    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError("M4 deliverable")

    def record(
        self,
        dataset_fingerprint: str,
        strategy_family: str,
        sr_hat: float,
        t_observations: int,
        gamma_3: float,
        gamma_4: float,
        metadata: dict[str, object],
    ) -> int:
        """Persist a single trial. Returns the trial id."""
        raise NotImplementedError("M4 deliverable")

    def effective_n_and_sr_variance(
        self,
        dataset_fingerprint: str,
        strategy_family: str,
        method: str = "naive",
    ) -> tuple[int, float]:
        """For DSR computation.

        method='naive' uses the user-supplied count of independent strategy
        families (default). method='pca' uses PCA on the trial correlation
        matrix; requires N >= 50 or raises InsufficientTrialsForPCAError.
        """
        raise NotImplementedError("M4 deliverable")


class InsufficientTrialsForPCAError(ValueError):
    """Raised when PCA-based effective N is requested with fewer than 50 trials."""
