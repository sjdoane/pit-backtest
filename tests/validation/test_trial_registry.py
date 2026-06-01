"""Tests for validation.trial_registry (M4 PR 4).

v_sr is the ddof=1 sample variance of the recorded sr_hat values:
  {1.0, 2.0}      -> (1-2)^2 / 2            = 0.5
  {1.0, 2.0, 3.0} -> ((1)+(0)+(1)) / (3-1)  = 1.0   (ddof=0 would give 0.6667)

The Bailey-LdP 0.766 DSR anchor is reachable through the registry: record
two trials with sr_hat = 1.5 +/- sqrt(0.2) (ddof=1 variance = 0.4) under
naive_effective_n=30, then dsr(sr_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5.0,
v_sr=0.4, n_effective=30) == 0.766 within 1e-3 (ADR 0013 dec 1).
"""

from __future__ import annotations

import json
import math
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from pit_backtest.analytics.sharpe import dsr
from pit_backtest.validation.trial_registry import (
    InsufficientTrialsForPCAError,
    TrialRegistry,
)


def _record_n(
    registry: TrialRegistry,
    sr_hats: list[float],
    *,
    fingerprint: str = "fp",
    family: str = "fam",
) -> None:
    for sr in sr_hats:
        registry.record(
            dataset_fingerprint=fingerprint,
            strategy_family=family,
            sr_hat=sr,
            t_observations=60,
            gamma_3=-0.5,
            gamma_4=5.0,
            metadata={},
        )


# ----- record + roundtrip -----


def test_db_path_property_roundtrips(tmp_path: Path) -> None:
    """The db_path accessor returns the backing file, so Runner.run_cpcv can
    open a naive=1 sibling registry over the same db for its isolated
    CPCV-path sub-family."""
    db = tmp_path / "x.db"
    registry = TrialRegistry(db, naive_effective_n=3)
    assert registry.db_path == db


def test_record_returns_positive_trial_id(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    trial_id = registry.record(
        dataset_fingerprint="fp",
        strategy_family="fam",
        sr_hat=1.0,
        t_observations=60,
        gamma_3=0.0,
        gamma_4=3.0,
        metadata={},
    )
    assert trial_id == 1


def test_record_returns_increasing_ids(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    _record_n(registry, [1.0])
    second = registry.record(
        dataset_fingerprint="fp",
        strategy_family="fam",
        sr_hat=2.0,
        t_observations=60,
        gamma_3=0.0,
        gamma_4=3.0,
        metadata={},
    )
    assert second == 2


def test_recorded_values_roundtrip(tmp_path: Path) -> None:
    """Read back the input columns only (NOT recorded_at, which is wall-clock
    audit metadata and would make the assertion non-deterministic).
    """
    db = tmp_path / "r.db"
    registry = TrialRegistry(db)
    registry.record(
        dataset_fingerprint="fp1",
        strategy_family="famA",
        sr_hat=1.25,
        t_observations=42,
        gamma_3=-0.3,
        gamma_4=4.5,
        metadata={"eta": 0.142, "seed": 7, "tag": "spy"},
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """
            SELECT dataset_fingerprint, strategy_family, sr_hat,
                   t_observations, gamma_3, gamma_4, metadata_json
            FROM trials WHERE trial_id = 1
            """
        ).fetchone()
    assert row[0] == "fp1"
    assert row[1] == "famA"
    assert row[2] == pytest.approx(1.25)
    assert row[3] == 42
    assert row[4] == pytest.approx(-0.3)
    assert row[5] == pytest.approx(4.5)
    assert json.loads(row[6]) == {"eta": 0.142, "seed": 7, "tag": "spy"}


def test_registry_persists_across_object_restart(tmp_path: Path) -> None:
    """ADR 0002 acceptance criterion 4: persists across process restart.
    Drop the object, reopen on the same db_path, the row survives.
    """
    db = tmp_path / "r.db"
    first = TrialRegistry(db, naive_effective_n=3)
    _record_n(first, [1.0, 2.0])
    del first
    second = TrialRegistry(db, naive_effective_n=3)
    n_eff, v_sr = second.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 3
    assert v_sr == pytest.approx(0.5)


# ----- naive effective-N + v_sr pins -----


def test_v_sr_two_trials_ddof_one(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=4)
    _record_n(registry, [1.0, 2.0])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 4
    assert v_sr == pytest.approx(0.5, abs=1e-12)


def test_v_sr_three_trials_uses_sample_variance_not_population(
    tmp_path: Path,
) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=10)
    _record_n(registry, [1.0, 2.0, 3.0])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 10
    assert v_sr == pytest.approx(1.0, abs=1e-12)  # ddof=1; ddof=0 would be 0.6667
    assert v_sr != pytest.approx(2.0 / 3.0, abs=1e-3)


def test_default_naive_effective_n_is_one(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    _record_n(registry, [1.0, 2.0])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 1
    assert v_sr == pytest.approx(0.5)


def test_query_isolates_by_strategy_family(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=2)
    _record_n(registry, [1.0, 2.0], family="famA")
    _record_n(registry, [10.0, 20.0, 30.0], family="famB")
    _, v_a = registry.effective_n_and_sr_variance("fp", "famA")
    _, v_b = registry.effective_n_and_sr_variance("fp", "famB")
    assert v_a == pytest.approx(0.5)
    assert v_b == pytest.approx(100.0)  # var{10,20,30} ddof=1 = 100


def test_query_isolates_by_dataset_fingerprint(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=2)
    _record_n(registry, [1.0, 2.0], fingerprint="fp1")
    _record_n(registry, [5.0, 9.0], fingerprint="fp2")
    _, v1 = registry.effective_n_and_sr_variance("fp1", "fam")
    _, v2 = registry.effective_n_and_sr_variance("fp2", "fam")
    assert v1 == pytest.approx(0.5)
    assert v2 == pytest.approx(8.0)  # var{5,9} ddof=1 = (4^2)/2 = 8


# ----- single-trial behavior (post-impl H2 from Plan-reviewer) -----


def test_single_trial_with_naive_one_returns_zero_v_sr(tmp_path: Path) -> None:
    """naive_effective_n=1 degenerates DSR to PSR (v_sr unused); a single
    recorded trial returns (1, 0.0) rather than raising.
    """
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    _record_n(registry, [1.5])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 1
    assert v_sr == 0.0


def test_single_trial_with_naive_above_one_raises(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=5)
    _record_n(registry, [1.5])
    with pytest.raises(ValueError, match="single trial"):
        registry.effective_n_and_sr_variance("fp", "fam")


# ----- domain violations -----


def test_init_raises_on_naive_effective_n_below_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="naive_effective_n >= 1"):
        TrialRegistry(tmp_path / "r.db", naive_effective_n=0)
    with pytest.raises(ValueError, match="naive_effective_n >= 1"):
        TrialRegistry(tmp_path / "r.db", naive_effective_n=-1)


def test_init_raises_on_non_int_naive_effective_n(tmp_path: Path) -> None:
    """Post-impl reviewer Medium 1: a float or bool naive_effective_n would
    flow into the int return tuple and silently distort DSR. bool is an int
    subclass, so it is excluded explicitly.
    """
    with pytest.raises(ValueError, match="int naive_effective_n"):
        TrialRegistry(tmp_path / "r.db", naive_effective_n=2.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="int naive_effective_n"):
        TrialRegistry(tmp_path / "r.db", naive_effective_n=True)  # type: ignore[arg-type]


def test_record_raises_on_empty_fingerprint(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="dataset_fingerprint"):
        registry.record(
            dataset_fingerprint="",
            strategy_family="fam",
            sr_hat=1.0,
            t_observations=60,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={},
        )


def test_record_raises_on_empty_family(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="strategy_family"):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family="",
            sr_hat=1.0,
            t_observations=60,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={},
        )


def test_record_raises_on_non_finite_sr_hat(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="finite sr_hat"):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family="fam",
            sr_hat=float("nan"),
            t_observations=60,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={},
        )


def test_record_raises_on_non_finite_gamma(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="finite gamma"):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family="fam",
            sr_hat=1.0,
            t_observations=60,
            gamma_3=0.0,
            gamma_4=float("inf"),
            metadata={},
        )


def test_record_raises_on_t_observations_below_two(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="t_observations >= 2"):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family="fam",
            sr_hat=1.0,
            t_observations=1,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={},
        )


def test_record_raises_on_non_serializable_metadata(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="JSON-serializable"):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family="fam",
            sr_hat=1.0,
            t_observations=60,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={"bad": object()},
        )


def test_effective_n_raises_on_zero_trials(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    with pytest.raises(ValueError, match="no trials recorded"):
        registry.effective_n_and_sr_variance("fp", "missing")


# ----- PCA deferral -----


def test_pca_method_raises_not_implemented_even_with_many_trials(
    tmp_path: Path,
) -> None:
    """Structural deferral: PCA needs per-trial return-series storage the
    scalar schema lacks, so it raises regardless of trial count (here 60).
    """
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=60)
    _record_n(registry, [float(i) for i in range(60)])
    with pytest.raises(NotImplementedError, match="deferred to v1.1"):
        registry.effective_n_and_sr_variance("fp", "fam", method="pca")


def test_unknown_method_raises_value_error(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db")
    _record_n(registry, [1.0, 2.0])
    with pytest.raises(ValueError, match="must be 'naive' or 'pca'"):
        registry.effective_n_and_sr_variance("fp", "fam", method="xyz")


def test_insufficient_trials_for_pca_error_is_value_error_subclass() -> None:
    assert issubclass(InsufficientTrialsForPCAError, ValueError)


# ----- DSR integration -----


def test_effective_n_output_feeds_dsr_to_finite_probability(
    tmp_path: Path,
) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=10)
    _record_n(registry, [1.0, 1.4, 0.8, 1.2, 1.1])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    result = dsr(
        sr_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5.0, v_sr=v_sr, n_effective=n_eff
    )
    assert 0.0 <= result <= 1.0


def test_registry_reaches_bailey_ldp_0766_anchor(tmp_path: Path) -> None:
    """Two trials with sr_hat = 1.5 +/- sqrt(0.2) give ddof=1 variance 0.4;
    with naive_effective_n=30 the DSR anchor 0.766 is reproduced (ADR 0013).
    """
    spread = math.sqrt(0.2)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=30)
    _record_n(registry, [1.5 - spread, 1.5 + spread])
    n_eff, v_sr = registry.effective_n_and_sr_variance("fp", "fam")
    assert n_eff == 30
    assert v_sr == pytest.approx(0.4, abs=1e-12)
    result = dsr(
        sr_hat=1.5, T=60, gamma_3=-0.5, gamma_4=5.0, v_sr=v_sr, n_effective=n_eff
    )
    assert result == pytest.approx(0.766, abs=1e-3)


# ----- WAL + determinism -----


def test_journal_mode_is_wal(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    TrialRegistry(db)
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_effective_n_deterministic_across_calls(tmp_path: Path) -> None:
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=7)
    _record_n(registry, [0.3, 0.7, 0.2, 0.9, 0.5])
    first = registry.effective_n_and_sr_variance("fp", "fam")
    second = registry.effective_n_and_sr_variance("fp", "fam")
    assert first == second


# ----- concurrency (ADR 0002 acceptance criterion 4 / dec 19) -----


def _write_trials_worker(db_path_str: str, m: int, family: str) -> None:
    """Module-level worker so it pickles cleanly under multiprocessing.spawn
    on Windows (the project standard per ADR 0010 lock #6). Constructs its
    own TrialRegistry and records m trials.
    """
    registry = TrialRegistry(Path(db_path_str))
    for i in range(m):
        registry.record(
            dataset_fingerprint="fp",
            strategy_family=family,
            sr_hat=float(i),
            t_observations=60,
            gamma_3=0.0,
            gamma_4=3.0,
            metadata={"worker": family, "i": i},
        )


def test_concurrent_writes_from_two_processes_all_persist(
    tmp_path: Path,
) -> None:
    """ADR 0002 acceptance criterion 4: two parallel writers x 50 trials each
    against the same db_path produce 100 rows with no corruption. Cold-start:
    both processes begin with no db file, exercising concurrent CREATE TABLE
    IF NOT EXISTS + the WAL one-writer busy_timeout serialization.
    """
    db = tmp_path / "concurrent.db"
    ctx = multiprocessing.get_context("spawn")
    m = 50
    procs = [
        ctx.Process(
            target=_write_trials_worker, args=(str(db), m, f"fam{w}")
        )
        for w in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    for p in procs:
        assert p.exitcode == 0
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    assert count == 2 * m
