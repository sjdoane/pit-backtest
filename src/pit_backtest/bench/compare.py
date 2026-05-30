"""Perf-budget regression comparison (M2 PR D Phase 0).

Per ADR 0012 lock #1 and #2 the comparison reads two JSON records
produced by `bench/spy_20y.py` (`--current` and `--baseline`),
computes the median delta percentage, and produces a verdict:

- delta_pct = (current.median - baseline.median) / baseline.median * 100
- threshold_pct = max(threshold_pct_floor, threshold_sigma * stdev_pct)
  where stdev_pct = baseline.stdev / baseline.median * 100

Phase 0 (this PR) ALWAYS exits 0 regardless of verdict. The comparison
emits a `::warning::` annotation on regression beyond threshold; the
diagnostic is informational only at Phase 0. A follow-up Phase 1 PR
flips the exit code to 1 on regression after the empirical noise floor
is committed to `.bench-baseline.json`.

Per ADR 0012 lock #6 the comparison exits 0 with `::warning::` if the
baseline is missing or has `n_runs: 0` (Phase 0 bootstrap state). The
first PR that ships the empirical baseline in Phase 1 changes the
behavior in the same PR.

Per ADR 0012 lock #4 single-sample comparison is forbidden: both
current and baseline records must satisfy `n_runs >= 5` and
`warmup >= 1`. A current record built from a single run raises a
ValueError because the comparison would be testing noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_record(path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _is_bootstrap_baseline(record: dict[str, object]) -> bool:
    """A baseline record with `n_runs: 0` is a Phase 0 placeholder."""
    n_runs = record.get("n_runs", 0)
    return not isinstance(n_runs, int) or n_runs == 0


def _validate_record(record: dict[str, object], label: str) -> None:
    """Per ADR 0012 lock #4: minimum-runs requirement; reject single-sample."""
    n_runs = record.get("n_runs", 0)
    warmup = record.get("warmup", 0)
    if not isinstance(n_runs, int) or n_runs < 5:
        raise ValueError(
            f"{label} record has n_runs={n_runs}; per ADR 0012 lock #4 the "
            f"comparison requires n_runs >= 5 to amortize per-run variance"
        )
    if not isinstance(warmup, int) or warmup < 1:
        raise ValueError(
            f"{label} record has warmup={warmup}; per ADR 0012 lock #4 the "
            f"comparison requires warmup >= 1 to discard cold-start jitter"
        )


def _print_github_warning(message: str) -> None:
    """Emit a GitHub Actions workflow command annotation.

    Format: `::warning::message` (one line; printed to stdout for GH to
    parse). When not running in GH Actions the line is harmless.
    """
    print(f"::warning::{message}")


def _compare(
    current: dict[str, object],
    baseline: dict[str, object],
    threshold_pct_floor: float,
    threshold_sigma: float,
) -> tuple[float, float, str]:
    """Return (delta_pct, threshold_pct, verdict).

    verdict in {"pass", "warn", "fail"}. The function does NOT decide
    the exit code; the caller maps verdict to exit per the Phase 0/1 flag.
    """
    cur_median = float(current["median_seconds"])  # type: ignore[arg-type]
    base_median = float(baseline["median_seconds"])  # type: ignore[arg-type]
    base_stdev = float(baseline["stdev_seconds"])  # type: ignore[arg-type]

    delta_pct = (cur_median - base_median) / base_median * 100.0
    stdev_pct = base_stdev / base_median * 100.0 if base_median > 0.0 else 0.0
    threshold_pct = max(threshold_pct_floor, threshold_sigma * stdev_pct)

    if delta_pct > threshold_pct:
        verdict = "fail"
    elif delta_pct > 0.0:
        verdict = "warn"
    else:
        verdict = "pass"
    return delta_pct, threshold_pct, verdict


def _format_report(
    current: dict[str, object],
    baseline: dict[str, object],
    delta_pct: float,
    threshold_pct: float,
    verdict: str,
) -> str:
    cur_median = float(current["median_seconds"])  # type: ignore[arg-type]
    base_median = float(baseline["median_seconds"])  # type: ignore[arg-type]
    base_stdev = float(baseline["stdev_seconds"])  # type: ignore[arg-type]
    cur_n = current.get("n_runs", "?")
    base_n = baseline.get("n_runs", "?")
    sign = "+" if delta_pct >= 0 else ""
    return (
        f"perf-budget: current={cur_median:.3f}s (n={cur_n}), "
        f"baseline={base_median:.3f}s (n={base_n}, stdev={base_stdev:.3f}s), "
        f"delta={sign}{delta_pct:.2f}%, threshold={threshold_pct:.2f}%, "
        f"verdict={verdict.upper()}"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current", type=Path, required=True, help="path to current run JSON"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="path to baseline JSON (Phase 0 bootstrap state acceptable)",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=20.0,
        help="threshold floor in percent (default 20)",
    )
    parser.add_argument(
        "--threshold-sigma",
        type=float,
        default=3.0,
        help="threshold sigma multiplier (default 3)",
    )
    parser.add_argument(
        "--phase-1-gate",
        action="store_true",
        help=(
            "enable Phase 1 fail-on-regression behavior; exits 1 if "
            "delta_pct > threshold. Phase 0 (default) exits 0 always."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.current.is_file():
        print(f"--current file not found: {args.current}", file=sys.stderr)
        return 1
    if not args.baseline.is_file():
        _print_github_warning(
            f"baseline file not found at {args.baseline}; Phase 0 bootstrap "
            f"(commit a baseline.json to enable comparison)"
        )
        return 0

    current = _load_record(args.current)
    baseline = _load_record(args.baseline)
    _validate_record(current, "current")

    if _is_bootstrap_baseline(baseline):
        _print_github_warning(
            f"baseline has n_runs=0 (Phase 0 bootstrap state); skipping "
            f"comparison. Commit an empirical baseline to enable the gate."
        )
        return 0

    _validate_record(baseline, "baseline")
    delta_pct, threshold_pct, verdict = _compare(
        current, baseline, args.threshold_pct, args.threshold_sigma
    )
    report = _format_report(current, baseline, delta_pct, threshold_pct, verdict)
    print(report)

    if verdict == "fail":
        _print_github_warning(report)
        if args.phase_1_gate:
            return 1
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
