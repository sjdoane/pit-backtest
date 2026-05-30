"""On-disk baseline state regression test (M2 PR D Phase 1).

Per Plan-reviewer Counter on Choice 1 of the Phase 1 plan: a future PR
that reverts `.bench-baseline.json` to the Phase 0 `n_runs: 0` placeholder
would silently drop the perf-budget gate to warn-and-pass per ADR 0012
lock #6 bootstrap fallback. The comparator behavior is correct (lock #6
preserves the forward-revert path back to Phase 0) but the repo-level
Phase 1 commitment dies with no test failure.

This test asserts the on-disk baseline carries `n_runs >= 5` (ADR 0012
lock #4 minimum) and is NOT the bootstrap placeholder. A revert is
loud; a deliberate Phase 0 fallback requires editing this test in the
same PR, which surfaces the decision.
"""

from __future__ import annotations

import json
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PATH = _REPO_ROOT / ".bench-baseline.json"


def test_on_disk_baseline_is_not_phase_0_placeholder() -> None:
    """Per ADR 0012 lock #2 the Phase 1 baseline carries empirical
    median + stdev from a workflow_dispatch run on main; n_runs >= 5
    (lock #4 minimum); not the n_runs=0 bootstrap placeholder.
    """
    assert _BASELINE_PATH.is_file(), f"baseline file missing at {_BASELINE_PATH}"
    record = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert isinstance(record["n_runs"], int)
    assert record["n_runs"] >= 5, (
        "On-disk .bench-baseline.json is the Phase 0 placeholder; a revert "
        "of Phase 1 dropped the gate to warn-and-pass per ADR 0012 lock #6. "
        "If this is intentional, update Phase 1 ADR footer + CHANGELOG + "
        "this test in the same PR."
    )
    assert isinstance(record["warmup"], int)
    assert record["warmup"] >= 1
    assert float(record["median_seconds"]) > 0.0
    assert float(record["stdev_seconds"]) >= 0.0
    assert record["runner_image_sha"] is not None
    assert record["commit_sha"] is not None
