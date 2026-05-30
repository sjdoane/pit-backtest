"""Bench harness tests (M2 PR D Phase 0).

Per ADR 0012 lock #1 the bench harness produces a JSON record with the
schema documented in `src/pit_backtest/bench/spy_20y.py`. The tests
exercise the harness end-to-end on a small fixture (days=60) so the
CI cost is minimal; the production workflow runs on days=20*365.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pit_backtest.bench.spy_20y import (
    _build_record,
    _generate_synthetic_prices,
    _measure,
    _parse_args,
    _write_synthetic_bundle,
    main,
)


def test_parse_args_defaults() -> None:
    """Defaults per ADR 0012 lock #4: runs=7, warmup=1."""
    args = _parse_args([])
    assert args.runs == 7
    assert args.warmup == 1
    assert args.seed == 42
    assert args.days == 20 * 365


def test_parse_args_overrides() -> None:
    args = _parse_args(["--runs", "3", "--warmup", "2", "--days", "60"])
    assert args.runs == 3
    assert args.warmup == 2
    assert args.days == 60


def test_main_writes_well_formed_json_with_small_fixture(tmp_path: Path) -> None:
    """The CLI writes a JSON record matching the documented schema."""
    output = tmp_path / "current.json"
    exit_code = main(
        [
            "--runs", "2",
            "--warmup", "1",
            "--days", "60",
            "--seed", "7",
            "--output", str(output),
        ]
    )
    assert exit_code == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    # Schema fields per ADR 0012 lock #1.
    assert record["schema_version"] == 1
    assert isinstance(record["median_seconds"], float)
    assert isinstance(record["stdev_seconds"], float)
    assert isinstance(record["min_seconds"], float)
    assert isinstance(record["max_seconds"], float)
    assert record["n_runs"] == 2
    assert record["warmup"] == 1
    assert record["median_seconds"] > 0.0
    assert record["min_seconds"] <= record["median_seconds"] <= record["max_seconds"]
    # Provenance fields.
    assert "python_version" in record
    assert "polars_version" in record
    assert "numpy_version" in record
    assert "platform" in record
    assert "measured_at" in record


def test_main_rejects_zero_runs() -> None:
    assert main(["--runs", "0"]) == 1


def test_main_rejects_negative_warmup() -> None:
    assert main(["--warmup", "-1"]) == 1


def test_generate_synthetic_prices_is_seeded_deterministic() -> None:
    """Two calls at the same seed produce bit-identical prices."""
    from datetime import date

    a = _generate_synthetic_prices(date(2024, 1, 1), 30, seed=42)
    b = _generate_synthetic_prices(date(2024, 1, 1), 30, seed=42)
    assert a == b


def test_measure_returns_correct_count_of_timings(tmp_path: Path) -> None:
    """_measure runs `warmup + runs` times and returns `runs` timings."""
    from datetime import date

    sep_rows: list[dict[str, object]] = []
    for ticker_rows in _generate_synthetic_prices(date(2024, 1, 1), 30, seed=42).values():
        sep_rows.extend(ticker_rows)
    _write_synthetic_bundle(tmp_path, sep_rows, "sharadar_bench")
    timings = _measure(runs=2, warmup=1, snapshots_root=tmp_path)
    assert len(timings) == 2
    assert all(t > 0.0 for t in timings)


def test_build_record_computes_summary_stats() -> None:
    """_build_record produces median/stdev/min/max correctly."""
    timings = [1.0, 1.5, 2.0, 2.5, 3.0]
    record = _build_record(timings, runs=5, warmup=1)
    assert record["median_seconds"] == 2.0
    assert record["min_seconds"] == 1.0
    assert record["max_seconds"] == 3.0
    assert record["n_runs"] == 5
    assert record["warmup"] == 1
    assert pytest.approx(float(record["stdev_seconds"]), abs=1e-9) == 0.7905694150420949


def test_main_writes_to_stdout_when_output_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When --output is omitted the JSON record prints to stdout."""
    exit_code = main(["--runs", "2", "--warmup", "1", "--days", "30"])
    assert exit_code == 0
    captured = capsys.readouterr()
    record = json.loads(captured.out)
    assert record["schema_version"] == 1
    assert record["n_runs"] == 2
