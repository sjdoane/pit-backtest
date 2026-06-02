"""Build the committed, plot-ready study artifact for the M5 figures (PR 4).

This is the EXPENSIVE, one-time step (needs the real Sharadar bundle; ~11-15 min
for one full 2005-2024 contiguous momentum backtest plus the bootstrap). It writes
a small set of JSON files under `scripts/figures/study_artifact/` which are
COMMITTED, so that `scripts/figures/regenerate.py` (the `make figures` target) can
render every README figure in seconds WITHOUT the gitignored bundle and WITHOUT
re-running the study. Parquet is NOT used because `.gitignore` excludes
`*.parquet`; the JSON payloads are small (a few hundred KB) and diffable in review.

The figures are sourced from a SINGLE contiguous zero-cost backtest (the same
level/Sharpe reference the 3b report leads with) plus the monthly block bootstrap.
The CPCV degeneracy and the cost surfaces are report tables (PR 3b/3c), not
figures, so this build deliberately skips run_cpcv and the eta sweep to stay
tractable; the cost band at SPY-uniform liquidity is sub-bp (visually degenerate)
and is reported as a table, not plotted. The survivorship two-panel (ADR 0002
line 96) is deferred: the survivorship study's total-return reconstruction does
not split-adjust, so reverse-split jumps in delisted names inflate its cohort CAGR
on the current bundle; the figure waits on that data-layer fix.

Run (clean venv, from the repo root):
  python scripts/figures/build_study_artifact.py
Then render:
  python scripts/figures/regenerate.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from examples.sp500_momentum_study import (  # noqa: E402
    STRATEGY_FAMILY,
    UNIVERSE_ID,
    MomentumStudyRecipe,
    _build_momentum_bar_loop,
    _monthly_returns_from_curve,
    _pnl_concentration_hhi,
    _sharpe,
    build_ever_member_union,
    choose_block_length,
    momentum_rebalance_dates,
)

from pit_backtest.analytics.bootstrap import stationary_block_bootstrap  # noqa: E402
from pit_backtest.analytics.result_adapter import to_backtest_result  # noqa: E402
from pit_backtest.data.sources.sharadar import SharadarDataSource  # noqa: E402
from pit_backtest.data.universe import SharadarSP500Universe  # noqa: E402
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle  # noqa: E402
from pit_backtest.validation.trial_registry import TrialRegistry  # noqa: E402

_SCHEMA_VERSION = 1
_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "study_artifact"
_DATA_FILES = (
    "momentum_equity.json",
    "bootstrap_fan.json",
    "by_year.json",
    "scalars.json",
)

_log = logging.getLogger("build_study_artifact")


def _git_sha() -> str:
    """Best-effort short git SHA for provenance; 'unknown' if git is absent."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def _equity_fan(
    monthly_returns: list[float],
    rebalance_dates: tuple[date, ...],
    *,
    n_paths: int,
    block_length: float,
    seed: int,
) -> dict[str, object]:
    """Per-month cumulative-equity percentile bands from the block bootstrap.

    Cumulates each resampled monthly-return path into an equity curve (starting
    at 1.0) and reports the p5/p25/p50/p75/p95 envelope plus the realized curve.
    This is the EQUITY projection of the same bootstrap whose SHARPE projection
    the report quotes; the caption says so. The x-axis is the rebalance dates:
    the i-th cumulative point sits at rebalance_dates[i] (point 0 = 1.0 at the
    first rebalance; the first monthly return advances to the second rebalance).
    """

    def _cumulative(returns: list[float]) -> list[float]:
        equity = [1.0]
        for r in returns:
            equity.append(equity[-1] * (1.0 + r))
        return equity

    paths = stationary_block_bootstrap(
        monthly_returns,
        n_paths=n_paths,
        expected_block_length=block_length,
        seed=seed,
    )
    # shape (n_paths, n_monthly + 1)
    matrix = np.array([_cumulative(p) for p in paths], dtype=float)
    pct = np.percentile(matrix, [5, 25, 50, 75, 95], axis=0)
    realized = _cumulative(monthly_returns)
    n_points = len(realized)
    # rebalance_dates has n_monthly + 1 = n_points entries; align directly.
    dts = [d.isoformat() for d in rebalance_dates[:n_points]]
    return {
        "dt": dts,
        "realized": realized,
        "p5": pct[0].tolist(),
        "p25": pct[1].tolist(),
        "p50": pct[2].tolist(),
        "p75": pct[3].tolist(),
        "p95": pct[4].tolist(),
    }


def build_momentum_artifact(
    recipe: MomentumStudyRecipe, *, n_bootstrap: int, seed: int
) -> dict[str, dict[str, object]]:
    """Run ONE contiguous zero-cost momentum backtest and derive the figure data.

    Returns the momentum_equity / bootstrap_fan / by_year payloads plus the
    momentum scalars, all from a single ~11-15 min run (no CPCV, no cost sweep).
    """
    _log.info("momentum_contiguous_run_begin n_rebalances=%d", len(recipe.rebalance_dates))
    contig = _build_momentum_bar_loop(recipe).run(
        start_dt=recipe.start_dt, end_dt=recipe.end_dt
    )
    curve = contig.equity_curve.sort("dt")
    equity: dict[str, object] = {
        "dt": [d.isoformat() for d in curve["dt"].to_list()],
        "nav": [float(v) for v in curve["nav"].to_list()],
    }

    with tempfile.TemporaryDirectory() as tmp:
        registry = TrialRegistry(Path(tmp) / "figures.db", naive_effective_n=1)
        result = to_backtest_result(
            contig,
            registry=registry,
            strategy_family=STRATEGY_FAMILY,
            universe_id=UNIVERSE_ID,
            periods_per_year=252,
        )

    monthly = _monthly_returns_from_curve(curve, recipe.rebalance_dates)
    block_length, _acf = choose_block_length(monthly)
    fan = _equity_fan(
        monthly,
        recipe.rebalance_dates,
        n_paths=n_bootstrap,
        block_length=block_length,
        seed=seed,
    )
    by_year: dict[str, object] = {
        str(year): ret
        for year, ret in sorted(result.scorecard.attribution.by_year.items())
    }
    perf = result.scorecard.performance
    scalars: dict[str, object] = {
        "sharpe_per_period": result.sr_hat,
        "psr": result.psr,
        "dsr": result.dsr,  # may be None
        "realized_monthly_sharpe": _sharpe(monthly),
        "block_length": block_length,
        "n_rebalances": contig.n_rebalances,
        "n_monthly_returns": len(monthly),
        "union_size": len(recipe.union_tickers),
        "total_return": perf.total_return,
        "annualized_return": perf.annualized_return,
        "annualized_volatility": perf.annualized_volatility,
        "max_drawdown": result.scorecard.runs_and_drawdowns.max_drawdown,
        "pnl_concentration_hhi": _pnl_concentration_hhi(curve, recipe.rebalance_dates),
        "window_start": recipe.start_dt.isoformat(),
        "window_end": recipe.end_dt.isoformat(),
    }
    return {
        "momentum_equity": equity,
        "bootstrap_fan": fan,
        "by_year": by_year,
        "momentum_scalars": scalars,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _content_hash(output_dir: Path) -> str:
    """SHA256 over the sorted data files (excluding meta.json) for provenance."""
    digest = hashlib.sha256()
    for name in _DATA_FILES:
        digest.update((output_dir / name).read_bytes())
    return digest.hexdigest()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start-dt", type=date.fromisoformat, default=date(2005, 1, 4))
    parser.add_argument("--end-dt", type=date.fromisoformat, default=date(2024, 12, 31))
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--n-bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260601)
    parser.add_argument("--bundle-prefix", default="sharadar")
    parser.add_argument("--snapshots-root", type=Path, default=_REPO_ROOT / "data" / "snapshots")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--built-at", default=None,
        help="ISO timestamp for meta provenance; default = now (UTC)",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    snapshots_root = args.snapshots_root.resolve()
    bundle_name = discover_latest_bundle(snapshots_root, args.bundle_prefix)
    if bundle_name is None:
        print(
            f"no snapshot under {snapshots_root} matching prefix "
            f"{args.bundle_prefix}_; pull per docs/methodology/dataset_versioning.md",
            file=sys.stderr,
        )
        return 2
    source = SharadarDataSource(bundle_name, snapshots_root)
    universe = SharadarSP500Universe(source)

    rebalance_dates = momentum_rebalance_dates(args.start_dt, args.end_dt)
    union = build_ever_member_union(universe, rebalance_dates)
    recipe = MomentumStudyRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name=bundle_name,
        start_dt=rebalance_dates[0],
        end_dt=args.end_dt,
        initial_capital=args.initial_capital,
        rebalance_dates=rebalance_dates,
        union_tickers=tuple(int(a) for a in union),
    )

    momentum = build_momentum_artifact(
        recipe, n_bootstrap=args.n_bootstrap, seed=args.seed
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "momentum_equity.json", momentum["momentum_equity"])
    _write_json(output_dir / "bootstrap_fan.json", momentum["bootstrap_fan"])
    _write_json(output_dir / "by_year.json", momentum["by_year"])
    _write_json(output_dir / "scalars.json", momentum["momentum_scalars"])

    built_at = args.built_at or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    content_hash = _content_hash(output_dir)
    scalars = momentum["momentum_scalars"]
    meta: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_name": bundle_name,
        "bundle_pull_date": str(source.bundle_entry.pull_date),
        "git_sha": _git_sha(),
        "built_at": built_at,
        "seed": args.seed,
        "n_bootstrap": args.n_bootstrap,
        "block_length": scalars["block_length"],
        "window_start": recipe.start_dt.isoformat(),
        "window_end": recipe.end_dt.isoformat(),
        "python_version": sys.version.split()[0],
        "polars_version": pl.__version__,
        "numpy_version": np.__version__,
        "content_hash": content_hash,
    }
    _write_json(output_dir / "meta.json", meta)
    _log.info(
        "wrote study artifact to %s (content_hash=%s)", output_dir, content_hash[:12]
    )
    print(f"study artifact written to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
