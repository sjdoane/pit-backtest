"""Regenerate every README figure from the committed study artifact (M5 PR 4).

This is the `make figures` entry point. It reads the small, COMMITTED JSON
artifact under `scripts/figures/study_artifact/` (produced one-time by
`build_study_artifact.py` from the real bundle) and renders the PNGs under
`docs/figures/` in seconds. It needs NEITHER the gitignored Sharadar bundle NOR
the ~11-15 min study, so any reviewer who clones the repo can reproduce the
figures with `pip install -e ".[figures]"` (matplotlib) and one command.

Windows-first: run it directly with the project's python; `make figures` is a
thin convenience alias for Unix.

    python scripts/figures/regenerate.py

Loading + validating the artifact is matplotlib-free (so the numeric checks run
in any environment); only the rendering imports matplotlib (the optional
`figures` extra). PNG bytes are NOT asserted to be reproducible across matplotlib
/ freetype versions; the artifact's numeric content is the audited contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parent / "study_artifact"
_DEFAULT_OUTPUT_DIR = _REPO_ROOT / "docs" / "figures"

_DATA_FILES = (
    "momentum_equity.json",
    "bootstrap_fan.json",
    "by_year.json",
    "scalars.json",
)
_FAN_BANDS = ("p5", "p25", "p50", "p75", "p95")

_log = logging.getLogger("regenerate_figures")


def load_artifact(artifact_dir: Path) -> dict[str, object]:
    """Load the JSON artifact payloads (matplotlib-free).

    Returns a dict keyed by payload name (momentum_equity, bootstrap_fan,
    by_year, scalars, meta). Raises FileNotFoundError if the artifact has not
    been built, or ValueError on a schema_version mismatch.
    """
    meta_path = artifact_dir / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(
            f"no study artifact under {artifact_dir}; run "
            f"`python scripts/figures/build_study_artifact.py` first (needs the "
            f"Sharadar bundle). The committed artifact is what `make figures` "
            f"renders from."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("schema_version") != 1:
        raise ValueError(
            f"unsupported artifact schema_version {meta.get('schema_version')}; "
            f"this regenerate.py understands version 1"
        )
    payloads: dict[str, object] = {"meta": meta}
    for name in _DATA_FILES:
        key = name[: -len(".json")]
        payloads[key] = json.loads((artifact_dir / name).read_text(encoding="utf-8"))
    return payloads


def validate_artifact(payloads: dict[str, object]) -> None:
    """Assert the load-bearing numeric invariants (matplotlib-free).

    These are the deterministic checks the test relies on (NOT the PNG bytes):
    the bootstrap-fan percentile bands are monotone non-decreasing at every
    month, the by-year keys are a contiguous run of years, and the cost-free
    study scalars are present (dsr may be null). Raises ValueError on any
    violation.
    """
    fan = payloads["bootstrap_fan"]
    assert isinstance(fan, dict)
    bands = [fan[b] for b in _FAN_BANDS]
    for col in bands:
        assert isinstance(col, list)
    n = len(bands[0])
    for b in bands:
        if len(b) != n:
            raise ValueError("bootstrap_fan bands have inconsistent lengths")
    for i in range(n):
        row = [float(bands[j][i]) for j in range(len(_FAN_BANDS))]
        if any(row[k] > row[k + 1] + 1e-9 for k in range(len(row) - 1)):
            raise ValueError(
                f"bootstrap_fan percentiles not monotone at month {i}: {row}"
            )

    by_year = payloads["by_year"]
    assert isinstance(by_year, dict)
    years = sorted(int(y) for y in by_year)
    if years and years != list(range(years[0], years[-1] + 1)):
        raise ValueError(f"by_year keys are not a contiguous run: {years}")

    scalars = payloads["scalars"]
    assert isinstance(scalars, dict)
    for key in ("sharpe_per_period", "psr", "block_length", "n_rebalances"):
        if key not in scalars:
            raise ValueError(f"scalars missing required key {key!r}")


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def render_all(payloads: dict[str, object], output_dir: Path) -> list[Path]:
    """Render every figure to output_dir; return the written PNG paths.

    Imports matplotlib lazily (the optional `figures` extra) so the loader /
    validator above stay importable without it. Uses the Agg backend so it runs
    headless and does not depend on a display.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    equity = payloads["momentum_equity"]
    fan = payloads["bootstrap_fan"]
    by_year = payloads["by_year"]
    scalars = payloads["scalars"]
    assert isinstance(equity, dict) and isinstance(fan, dict)
    assert isinstance(by_year, dict) and isinstance(scalars, dict)

    def _dates(values: object) -> Any:
        # Returns Any (not list[date]) because matplotlib's plot/fill_between
        # stubs reject list[date] x-args even though matplotlib handles dates at
        # runtime; Any keeps this clean whether or not the matplotlib stubs are
        # installed (CI without the figures extra sees matplotlib as Any anyway).
        assert isinstance(values, list)
        return [date.fromisoformat(str(v)) for v in values]

    def _floats(container: dict[str, object], key: str) -> list[float]:
        values = container[key]
        assert isinstance(values, list)
        return [float(v) for v in values]

    def _scalar(key: str) -> float:
        return float(scalars[key])  # scalars narrowed to dict above

    # 1. Momentum equity curve (log scale; the contiguous zero-cost reference).
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(_dates(equity["dt"]), _floats(equity, "nav"), color="#1f5fa8", lw=1.2)
    ax.set_yscale("log")
    ax.set_title(
        "JT1993 12-1 momentum, top-quintile long: zero-cost NAV "
        f"({scalars.get('window_start')} to {scalars.get('window_end')})"
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("NAV ($, log scale)")
    dsr = _opt_float(scalars.get("dsr"))
    ax.annotate(
        f"per-period Sharpe={_scalar('sharpe_per_period'):.3f}  "
        f"PSR={_scalar('psr'):.3f}  "
        f"DSR={'n/a' if dsr is None else f'{dsr:.3f}'}  "
        f"total return={_scalar('total_return') * 100:.1f}%",
        xy=(0.01, 0.97), xycoords="axes fraction", va="top", fontsize=8,
    )
    ax.grid(True, which="both", alpha=0.25)
    fig.tight_layout()
    written.append(_save(fig, plt, output_dir / "momentum_equity.png"))

    # 2. Block-bootstrap equity fan (the genuine path-uncertainty surface; the
    #    EQUITY projection of the same bootstrap whose Sharpe the report quotes).
    fig, ax = plt.subplots(figsize=(10, 5))
    fan_dts = _dates(fan["dt"])
    p5 = _floats(fan, "p5")
    p25 = _floats(fan, "p25")
    p50 = _floats(fan, "p50")
    p75 = _floats(fan, "p75")
    p95 = _floats(fan, "p95")
    realized = _floats(fan, "realized")
    ax.fill_between(fan_dts, p5, p95, color="#1f5fa8", alpha=0.15, label="bootstrap 5-95%")
    ax.fill_between(fan_dts, p25, p75, color="#1f5fa8", alpha=0.30, label="bootstrap 25-75%")
    ax.plot(fan_dts, p50, color="#1f5fa8", lw=1.0, ls="--", label="bootstrap median")
    ax.plot(fan_dts, realized, color="#c0392b", lw=1.4, label="realized")
    ax.set_title(
        "Stationary block-bootstrap equity fan of the monthly return series "
        f"(block length {_scalar('block_length'):.0f} months)"
    )
    ax.set_xlabel("Rebalance date")
    ax.set_ylabel("Cumulative growth of $1")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    written.append(_save(fig, plt, output_dir / "bootstrap_fan.png"))

    # 3. Year-by-year decomposition.
    fig, ax = plt.subplots(figsize=(10, 4))
    years = sorted(int(y) for y in by_year)
    rets = [float(by_year[str(y)]) for y in years]
    colors = ["#2e8b57" if r >= 0 else "#c0392b" for r in rets]
    # Numeric x positions + explicit year tick labels (passing the year strings
    # directly makes matplotlib infer categorical units and warn that they parse
    # as numbers, which errors the render test under filterwarnings=error).
    positions = list(range(len(years)))
    ax.bar(positions, [r * 100 for r in rets], color=colors)
    ax.set_xticks(positions)
    ax.set_xticklabels([str(y) for y in years])
    ax.axhline(0.0, color="black", lw=0.6)
    ax.set_title("Year-by-year return (each calendar year stands alone)")
    ax.set_xlabel("Year")
    ax.set_ylabel("Return (%)")
    ax.tick_params(axis="x", rotation=60, labelsize=7)
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    written.append(_save(fig, plt, output_dir / "by_year.png"))

    _write_provenance(payloads, output_dir)
    return written


def _save(fig: Any, plt: Any, path: Path) -> Path:
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def _write_provenance(payloads: dict[str, object], output_dir: Path) -> None:
    """Stamp the artifact provenance next to the PNGs (C3 honesty link).

    Records which committed artifact (content hash + git SHA + bundle) the PNGs
    were rendered from, so a drift between the committed figures and the
    committed artifact is detectable.
    """
    meta = payloads["meta"]
    assert isinstance(meta, dict)
    lines = [
        "# Figure provenance",
        "",
        "These PNGs were rendered by `scripts/figures/regenerate.py` from the",
        "committed study artifact under `scripts/figures/study_artifact/`.",
        "",
        f"- bundle: {meta.get('bundle_name')} (pull {meta.get('bundle_pull_date')})",
        f"- artifact git SHA: {meta.get('git_sha')}",
        f"- artifact content hash: {meta.get('content_hash')}",
        f"- built at: {meta.get('built_at')}",
        f"- bootstrap: {meta.get('n_bootstrap')} paths, seed {meta.get('seed')}, "
        f"block length {meta.get('block_length')}",
        "",
        "Regenerate with: `python scripts/figures/regenerate.py`",
    ]
    (output_dir / "PROVENANCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact-dir", type=Path, default=_DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    try:
        payloads = load_artifact(args.artifact_dir.resolve())
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    validate_artifact(payloads)
    written = render_all(payloads, args.output_dir.resolve())
    for path in written:
        _log.info("wrote %s", path)
    print(f"rendered {len(written)} figures to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
