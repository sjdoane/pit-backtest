"""SQLite WAL-backed trial registry.

Per ADR 0002 decision 19 + acceptance criterion 4: single-machine
concurrent (multiple notebooks plus pytest workers); WAL mode; serialized
writes via SQLite's one-writer lock with a busy-timeout retry. Per ADR
0003 decision 21 (as reconciled by the ADR 0003 amendment footer this PR
adds): the v1 effective-N method is `naive` (the user-declared count of
independent strategy families, supplied at construction); PCA-based
effective-N is deferred to v1.1 because it needs per-trial return-series
storage that this scalar-only schema does not carry.

The registry feeds the Deflated Sharpe Ratio: `effective_n_and_sr_variance`
returns `(n_effective, v_sr)` consumed by `analytics.sharpe.dsr(...,
v_sr=..., n_effective=...)`.

PCA threshold reconciliation (ADR 0003 dec 21 stated both "opt-in for
N>=50" and "with N<30 PCA raises"): in the v1.1 PCA path, PCA is the
recommended method at N>=50, hard-errors below N=30, and the 30-to-50
band is allowed-but-not-default. In v1, PCA is structurally deferred, so
any `method="pca"` request raises `NotImplementedError` regardless of
trial count. `InsufficientTrialsForPCAError` is reserved for the v1.1 PCA
path's genuine low-N case.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_BUSY_TIMEOUT_MS = 5000


class InsufficientTrialsForPCAError(ValueError):
    """Reserved for the v1.1 PCA effective-N path's genuine low-N case.

    Per ADR 0003 decision 21 the v1.1 PCA method hard-errors below 30
    trials. In v1, PCA is structurally deferred (the scalar-only schema
    cannot store the per-trial return series PCA needs), so
    `effective_n_and_sr_variance(method="pca")` raises `NotImplementedError`
    rather than this exception. This class is exported now so the v1.1
    PCA implementation and any forward-looking caller `except` clause have
    a stable symbol to reference.
    """


class TrialRegistry:
    """Persistent trial registry feeding DSR.

    Construction-time `naive_effective_n` is the user-declared count of
    independent strategy families (ADR 0003 dec 21). It is a statement of
    experimental intent (how many independent bets were placed), NOT the
    recorded row count; a user who declares `naive_effective_n=30` after
    recording 3 trials gets `n_effective=30` against a 3-row variance.
    The `v_sr` term is always computed from the actual recorded rows.
    """

    def __init__(self, db_path: Path, naive_effective_n: int = 1) -> None:
        # bool is an int subclass; exclude it so naive_effective_n=True does
        # not slip through as n_effective=1 (True < 1 is False). record()
        # defends every other numeric input, so the one construction-time
        # integer is guarded symmetrically (post-impl reviewer Medium 1).
        if not isinstance(naive_effective_n, int) or isinstance(
            naive_effective_n, bool
        ):
            raise ValueError(
                f"TrialRegistry requires an int naive_effective_n; got "
                f"{type(naive_effective_n).__name__}"
            )
        if naive_effective_n < 1:
            raise ValueError(
                f"TrialRegistry requires naive_effective_n >= 1; got "
                f"{naive_effective_n}"
            )
        self._db_path = db_path
        self._naive_effective_n = naive_effective_n
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id            INTEGER PRIMARY KEY,
                    dataset_fingerprint TEXT    NOT NULL,
                    strategy_family     TEXT    NOT NULL,
                    sr_hat              REAL    NOT NULL,
                    t_observations      INTEGER NOT NULL,
                    gamma_3             REAL    NOT NULL,
                    gamma_4             REAL    NOT NULL,
                    metadata_json       TEXT    NOT NULL,
                    recorded_at         TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trials_fingerprint_family
                ON trials (dataset_fingerprint, strategy_family)
                """
            )
            conn.commit()

    @property
    def db_path(self) -> Path:
        """The SQLite file backing this registry.

        Exposed so a derived sibling registry can be opened over the SAME db
        file: `Runner.run_cpcv` isolates its phi-identical CPCV-path trials
        into a `::cpcv_paths` sub-family at naive_effective_n=1 over this same
        file, keeping the study family's (n_effective, v_sr) untouched.
        """
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection with the WAL + busy-timeout pragmas.

        Connection-per-operation: SQLite Connection objects are not safe
        to share across processes, and the concurrency model is
        multi-process (parallel pytest workers + notebooks). Re-asserting
        `journal_mode=WAL` on every connection is idempotent (a no-op once
        the database header records WAL) and sidesteps any first-connection
        ordering concern. `busy_timeout` and `synchronous` are per-connection
        and must be set each time.

        `synchronous=NORMAL` is the WAL-recommended setting and is chosen
        deliberately: under WAL it never corrupts the database, but a
        committed transaction can be lost if the OS crashes or power is cut
        before the next checkpoint. For a research trial registry that
        power-loss window is an acceptable durability tradeoff for the
        per-commit fsync it saves over `FULL`.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

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
        """Persist a single trial. Returns the autoincrement trial id.

        Raises:
          ValueError: on empty `dataset_fingerprint` or `strategy_family`
            (an empty key silently creates a degenerate partition no read
            can match coherently); on non-finite `sr_hat`, `gamma_3`, or
            `gamma_4` (a NaN `sr_hat` would poison the downstream v_sr
            variance and pass the `dsr` `v_sr < 0` guard, since
            `NaN < 0` is False, silently producing a NaN DSR); on
            `t_observations < 2` (mirrors the `psr`/`dsr` `T >= 2` floor);
            on non-JSON-serializable `metadata` (the offending key is
            surfaced in the message).
        """
        if not dataset_fingerprint:
            raise ValueError("record requires a non-empty dataset_fingerprint")
        if not strategy_family:
            raise ValueError("record requires a non-empty strategy_family")
        if not math.isfinite(sr_hat):
            raise ValueError(
                f"record requires finite sr_hat; got {sr_hat}"
            )
        if not math.isfinite(gamma_3) or not math.isfinite(gamma_4):
            raise ValueError(
                f"record requires finite gamma_3 + gamma_4; got "
                f"gamma_3={gamma_3}, gamma_4={gamma_4}"
            )
        if t_observations < 2:
            raise ValueError(
                f"record requires t_observations >= 2; got {t_observations}"
            )
        try:
            metadata_json = json.dumps(
                metadata, sort_keys=True, separators=(",", ":")
            )
        except TypeError as exc:
            raise ValueError(
                f"record requires JSON-serializable metadata; got a "
                f"non-serializable value ({exc})"
            ) from exc
        recorded_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO trials (
                    dataset_fingerprint, strategy_family, sr_hat,
                    t_observations, gamma_3, gamma_4, metadata_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_fingerprint,
                    strategy_family,
                    sr_hat,
                    t_observations,
                    gamma_3,
                    gamma_4,
                    metadata_json,
                    recorded_at,
                ),
            )
            conn.commit()
            trial_id = cursor.lastrowid
        if trial_id is None:  # pragma: no cover - sqlite always sets lastrowid
            raise RuntimeError("INSERT did not return a lastrowid")
        return int(trial_id)

    def effective_n_and_sr_variance(
        self,
        dataset_fingerprint: str,
        strategy_family: str,
        method: str = "naive",
    ) -> tuple[int, float]:
        """Return `(n_effective, v_sr)` for the DSR computation.

        `method="naive"` (default): `n_effective` is the construction-time
        `naive_effective_n`; `v_sr` is the ddof=1 sample variance of the
        recorded `sr_hat` values for the key (Bailey-LdP 2014 V[{SR_n}]).

        `method="pca"`: structurally deferred to v1.1 (the scalar-only
        schema cannot store the per-trial return series PCA needs); raises
        `NotImplementedError` regardless of trial count.

        Raises:
          ValueError: when no trials are recorded for the key; when the
            key has a single trial AND `naive_effective_n > 1` (the
            multiple-testing case genuinely needs the cross-sectional
            variance, which is undefined for one point); when `method` is
            neither "naive" nor "pca".
          NotImplementedError: when `method="pca"` (v1.1 deferral).

        For `naive_effective_n == 1` the DSR consumer degenerates to
        `psr(sr_hat, sr_star=0.0, ...)` and never reads `v_sr`, so a single
        recorded trial returns `(1, 0.0)` rather than raising.
        """
        if method == "pca":
            raise NotImplementedError(
                "PCA effective-N is deferred to v1.1: it requires per-trial "
                "return-series storage this scalar-only schema does not "
                "carry. Use method='naive'. See ADR 0003 dec 21 + the "
                "trial_registry amendment footer."
            )
        if method != "naive":
            raise ValueError(
                f"effective_n_and_sr_variance method must be 'naive' or "
                f"'pca'; got {method!r}"
            )
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT sr_hat FROM trials
                WHERE dataset_fingerprint = ? AND strategy_family = ?
                ORDER BY trial_id
                """,
                (dataset_fingerprint, strategy_family),
            ).fetchall()
        sr_hats = [float(r[0]) for r in rows]
        n_trials = len(sr_hats)
        if n_trials == 0:
            raise ValueError(
                f"no trials recorded for (dataset_fingerprint="
                f"{dataset_fingerprint!r}, strategy_family="
                f"{strategy_family!r}); cannot compute v_sr"
            )
        if n_trials < 2:
            if self._naive_effective_n > 1:
                raise ValueError(
                    f"v_sr is undefined for a single trial but "
                    f"naive_effective_n={self._naive_effective_n} > 1 "
                    f"requires the cross-sectional variance; record >= 2 "
                    f"trials for (dataset_fingerprint={dataset_fingerprint!r}"
                    f", strategy_family={strategy_family!r})"
                )
            return (self._naive_effective_n, 0.0)
        v_sr = statistics.variance(sr_hats)
        return (self._naive_effective_n, v_sr)
