"""Bench compare tests (M2 PR D Phase 0).

Per ADR 0012 lock #2, #4, and #6 the comparison:
- Always exits 0 at Phase 0 (default; no --phase-1-gate flag)
- Requires n_runs >= 5 and warmup >= 1 on both sides
- Treats baselines with n_runs=0 as Phase 0 bootstrap (warn + exit 0)
- Computes threshold = max(threshold_pct_floor, threshold_sigma * stdev_pct)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pit_backtest.bench.compare import (
    _compare,
    _format_report,
    _is_bootstrap_baseline,
    _load_record,
    _validate_record,
    main,
)


def _write_record(path: Path, record: dict[str, object]) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")


def _make_record(
    median: float, stdev: float, n_runs: int = 7, warmup: int = 1
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "median_seconds": median,
        "stdev_seconds": stdev,
        "min_seconds": median - stdev,
        "max_seconds": median + stdev,
        "n_runs": n_runs,
        "warmup": warmup,
    }


def test_is_bootstrap_baseline_detects_n_runs_zero() -> None:
    assert _is_bootstrap_baseline({"n_runs": 0}) is True
    assert _is_bootstrap_baseline({"n_runs": 7}) is False
    assert _is_bootstrap_baseline({}) is True  # missing -> default 0


def test_validate_record_rejects_low_n_runs() -> None:
    with pytest.raises(ValueError, match="n_runs"):
        _validate_record(_make_record(1.0, 0.05, n_runs=4), "current")


def test_validate_record_rejects_zero_warmup() -> None:
    with pytest.raises(ValueError, match="warmup"):
        _validate_record(_make_record(1.0, 0.05, warmup=0), "current")


def test_compare_pass_on_faster_run() -> None:
    current = _make_record(0.9, 0.05)
    baseline = _make_record(1.0, 0.05)
    delta, threshold, verdict = _compare(current, baseline, 20.0, 3.0)
    # delta = (0.9 - 1.0) / 1.0 * 100 = -10
    assert delta == pytest.approx(-10.0, abs=1e-9)
    assert verdict == "pass"


def test_compare_warn_on_small_regression() -> None:
    current = _make_record(1.05, 0.05)
    baseline = _make_record(1.0, 0.05)
    delta, threshold, verdict = _compare(current, baseline, 20.0, 3.0)
    # delta = +5%; threshold = max(20%, 3*5%) = 20%
    assert verdict == "warn"


def test_compare_fail_on_large_regression() -> None:
    current = _make_record(1.5, 0.05)
    baseline = _make_record(1.0, 0.05)
    delta, threshold, verdict = _compare(current, baseline, 20.0, 3.0)
    assert delta == pytest.approx(50.0, abs=1e-9)
    assert verdict == "fail"


def test_compare_threshold_widens_with_high_baseline_variance() -> None:
    """Per ADR 0012 lock #4 threshold = max(20%, 3 * stdev_pct)."""
    current = _make_record(1.25, 0.1)
    baseline = _make_record(1.0, 0.1)  # stdev_pct = 10%
    delta, threshold, verdict = _compare(current, baseline, 20.0, 3.0)
    # threshold = max(20, 3*10) = 30%; delta = 25%; verdict = warn (not fail)
    assert threshold == pytest.approx(30.0, abs=1e-9)
    assert verdict == "warn"


def test_main_returns_0_when_baseline_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0012 lock #6 the bootstrap state (missing baseline)
    exits 0 with a `::warning::` annotation.
    """
    current_path = tmp_path / "current.json"
    baseline_path = tmp_path / "missing.json"
    _write_record(current_path, _make_record(1.0, 0.05))
    exit_code = main(
        ["--current", str(current_path), "--baseline", str(baseline_path)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "::warning::" in captured.out
    assert "baseline file not found" in captured.out


def test_main_returns_0_when_baseline_is_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0012 lock #6 a baseline with n_runs=0 is Phase 0 bootstrap."""
    current_path = tmp_path / "current.json"
    baseline_path = tmp_path / "baseline.json"
    _write_record(current_path, _make_record(1.0, 0.05))
    _write_record(baseline_path, _make_record(0.0, 0.0, n_runs=0, warmup=0))
    exit_code = main(
        ["--current", str(current_path), "--baseline", str(baseline_path)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Phase 0 bootstrap" in captured.out


def test_main_phase_0_always_exits_0_even_on_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0012 lock #2 Phase 0 emits a warning but exits 0."""
    current_path = tmp_path / "current.json"
    baseline_path = tmp_path / "baseline.json"
    _write_record(current_path, _make_record(2.0, 0.05))  # 100% regression
    _write_record(baseline_path, _make_record(1.0, 0.05))
    exit_code = main(
        ["--current", str(current_path), "--baseline", str(baseline_path)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "::warning::" in captured.out


def test_main_phase_1_gate_exits_1_on_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per ADR 0012 lock #2 Phase 1 (`--phase-1-gate`) flips exit on fail."""
    current_path = tmp_path / "current.json"
    baseline_path = tmp_path / "baseline.json"
    _write_record(current_path, _make_record(2.0, 0.05))
    _write_record(baseline_path, _make_record(1.0, 0.05))
    exit_code = main(
        [
            "--current", str(current_path),
            "--baseline", str(baseline_path),
            "--phase-1-gate",
        ]
    )
    assert exit_code == 1


def test_main_current_missing_returns_1(tmp_path: Path) -> None:
    """Missing current file is an operator error, not bootstrap."""
    baseline_path = tmp_path / "baseline.json"
    _write_record(baseline_path, _make_record(1.0, 0.05))
    exit_code = main(
        ["--current", "/nonexistent/current.json", "--baseline", str(baseline_path)]
    )
    assert exit_code == 1


def test_format_report_includes_required_fields() -> None:
    current = _make_record(1.05, 0.05)
    baseline = _make_record(1.0, 0.05)
    report = _format_report(current, baseline, 5.0, 20.0, "warn")
    assert "current=" in report
    assert "baseline=" in report
    assert "delta=" in report
    assert "threshold=" in report
    assert "verdict=WARN" in report
