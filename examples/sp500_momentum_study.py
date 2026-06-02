"""Worked JT1993 12-1 momentum study on the PIT S&P 500: the honest core (M5 PR 3b).

The M5 capstone study (ADR 0002 dec 20, ADR 0016): monthly-rebalance,
top-quintile-long, equal-weight JT1993 momentum over the survivorship-bias-free
Sharadar S&P 500 (2005-2024). This PR composes the milestone-meaningful CORE:

- the CONTIGUOUS zero-cost backtest as the return-level + Sharpe reference
  (ADR 0016 dec 2), adapted to a BacktestResult with PSR + the Deflated Sharpe
  at naive_effective_n=1 (the honest single-strategy choice: ONE pre-specified
  strategy, no multiple-testing family, so DSR degenerates to PSR-against-zero,
  ADR 0013 dec 5);
- Runner.run_cpcv on momentum, reporting the near-zero CPCV path dispersion as
  the deterministic-factor DEGENERACY (ADR 0016 dec 4), NOT as the headline;
- the stationary block-bootstrap fan on the MONTHLY return series as the genuine
  headline path-uncertainty surface (ADR 0016 dec 5/6), with the block length
  chosen from the MEASURED monthly autocorrelation (reported, not a magic number);
- the year-by-year regime decomposition (the scorecard Attribution.by_year);
- an honest DSR conclusion (the milestone passes whether or not DSR >= 0.95; a
  non-viable result is reported plainly, per the kill-early thesis).

The cost-sensitivity band and the commission-only contiguous-vs-CPCV seam-cost
demonstration are PR 3c; figures + METHODOLOGY.md + the M5 SHIPPED flip are PR 4.

Survivorship safety: the BarLoop's fixed ticker tuple is the ever-member union
over the rebalance calendar (every name that was an S&P 500 member at ANY
rebalance), so a name that later left the index is still priced while held; the
policy stops selecting it once members_at (as-of) drops it.

This is a one-time study run on the real bundle (~11-15 min gated); the gated
integration test runs a short-window subset. The signal is gated to the
rebalance calendar (the PR 3a perf gate) so signal.compute fires ~240 times,
not ~5000.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import tempfile
import warnings
from datetime import date, datetime, time
from pathlib import Path

import attrs
import polars as pl

from pit_backtest.analytics.bootstrap import stationary_block_bootstrap
from pit_backtest.analytics.result_adapter import to_backtest_result
from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.runner import Runner
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.top_quintile import TopQuintileLongPolicy
from pit_backtest.signal.momentum import Momentum12_1Signal
from pit_backtest.utils.logging import configure_logging
from pit_backtest.validation.cv import CPCVSplitter
from pit_backtest.validation.trial_registry import TrialRegistry

STRATEGY_FAMILY = "jt1993_mom_12_1_topq"
UNIVERSE_ID = "sp500_pit"
_DSR_THRESHOLD = 0.95
_CPCV_N_GROUPS = 6
_CPCV_K_TEST = 2

_log = logging.getLogger(__name__)


def momentum_rebalance_dates(start: date, end: date) -> tuple[date, ...]:
    """The monthly last trading days inside [start, end], sorted ascending.

    monthly_last_trading_day returns a frozenset and the TestClock caches a
    +/-14 day pad, so filter to the window then sort (the single source of
    truth for the policy rebalance set, the BarLoop signal_calendar, and the
    CPCV observations).
    """
    clock = TestClock(start_dt=start, end_dt=end)
    return tuple(
        sorted(
            d
            for d in monthly_last_trading_day(clock.trading_days())
            if start <= d <= end
        )
    )


def build_ever_member_union(
    universe: SharadarSP500Universe, rebalance_dates: tuple[date, ...]
) -> tuple[AssetId, ...]:
    """The set of all AssetIds that were S&P 500 members at ANY rebalance.

    members_at is as-of, so the union over the rebalance calendar is the
    survivorship-safe universe: every name that ever entered the index in-window
    is carried (so preload + score + snapshot see it), and the policy simply
    stops selecting a name once members_at drops it. Sorted (Determinism Req 3).
    """
    union: set[AssetId] = set()
    for rebalance in rebalance_dates:
        union.update(
            universe.members_at(datetime.combine(rebalance, time(16, 0)))
        )
    return tuple(sorted(union, key=int))


def build_asset_id_to_ticker(
    source: SharadarDataSource, union: tuple[AssetId, ...]
) -> dict[AssetId, str]:
    """AssetId -> ticker map RESTRICTED to the union (date-agnostic permaticker).

    Restricting to the union (which the universe already proved resolves 1:1 to
    permatickers) sidesteps any permaticker-reuse last-wins ambiguity in the
    full tickers table. Raises if a union member is absent or non-unique.
    """
    want = {int(a) for a in union}
    tbl = (
        source.get_table("tickers")
        .select(pl.col("permaticker").cast(pl.Int64), pl.col("ticker"))
        .filter(pl.col("permaticker").is_in(list(want)))
        .collect()
    )
    mapping: dict[AssetId, str] = {}
    for row in tbl.iter_rows(named=True):
        asset_id = AssetId(int(row["permaticker"]))
        if asset_id in mapping:
            raise ValueError(
                f"permaticker {int(asset_id)} appears more than once in the "
                f"tickers table; the union->ticker map would be ambiguous"
            )
        mapping[asset_id] = str(row["ticker"])
    missing = want - {int(a) for a in mapping}
    if missing:
        raise ValueError(
            f"{len(missing)} union members have no tickers row (e.g. "
            f"{sorted(missing)[:5]}); the union must resolve 1:1 to tickers"
        )
    return mapping


@attrs.frozen(slots=True)
class _ResolverMap:
    """AssetId -> ticker callable (a frozen dict wrapper)."""

    mapping: dict[AssetId, str]

    def __call__(self, asset_id: AssetId) -> str:
        return self.mapping[asset_id]


@attrs.frozen(slots=True)
class MomentumStudyRecipe:
    """The study's fixed inputs over one window.

    union_tickers are plain ints (the AssetId permatickers); rebalance_dates is
    the in-window monthly calendar. The same recipe drives the contiguous run
    and (per window) the CPCV group backtests.
    """

    snapshots_root: str
    bundle_name: str
    start_dt: date
    end_dt: date
    initial_capital: float
    rebalance_dates: tuple[date, ...]
    union_tickers: tuple[int, ...]


def build_observations(
    rebalance_dates: tuple[date, ...],
) -> tuple[pl.DataFrame, pl.Series]:
    """The (observations, label_horizons) pair the CPCV splitter consumes.

    observations carries a sorted pl.Date 'dt' column (one row per rebalance);
    label_horizons is a same-dtype pl.Date series (a zero-day horizon is fine
    since purge/embargo are inert for the deterministic factor, but the dtypes
    must match per the splitter's _require_label_horizons check).
    """
    observations = pl.DataFrame({"dt": list(rebalance_dates)}).with_columns(
        pl.col("dt").cast(pl.Date)
    )
    label_horizons = pl.Series("h", list(rebalance_dates), dtype=pl.Date)
    return observations, label_horizons


def _build_momentum_bar_loop(recipe: MomentumStudyRecipe) -> BarLoop:
    """Build the zero-cost momentum BarLoop for the recipe's window.

    The zero-cost CloseFillMatchingEngine is the level/Sharpe reference and the
    CPCV/bootstrap input (all cost-independent; the cost band + seam are PR 3c).
    The signal is gated to the rebalance calendar (the PR 3a perf gate).
    """
    source = SharadarDataSource(recipe.bundle_name, Path(recipe.snapshots_root))
    universe = SharadarSP500Universe(source)
    clock = TestClock(start_dt=recipe.start_dt, end_dt=recipe.end_dt)
    union = tuple(AssetId(t) for t in recipe.union_tickers)
    resolver = _ResolverMap(build_asset_id_to_ticker(source, union))
    rebalances = frozenset(recipe.rebalance_dates)

    price_index: dict[tuple[AssetId, date], float] = {}
    for asset_id in union:
        frame = source.read_sep_prices(
            ticker=resolver(asset_id),
            start_dt=recipe.start_dt,
            end_dt=recipe.end_dt,
        )
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt: datetime) -> float | None:
        d = dt.date() if isinstance(dt, datetime) else dt
        return price_index.get((asset_id, d))

    return BarLoop(
        data_source=source,
        universe=universe,
        signal=Momentum12_1Signal(),
        policy=TopQuintileLongPolicy(
            rebalance_dates=rebalances, price_lookup=price_lookup
        ),
        matching_engine=CloseFillMatchingEngine(clock=clock),
        clock=clock,
        tickers=union,
        initial_capital=recipe.initial_capital,
        use_real_pit_view=True,
        asset_id_to_ticker=resolver,
        signal_calendar=rebalances,
    )


@attrs.frozen(slots=True)
class _MomentumCpcvFactory:
    """In-process Callable[[date, date], BarLoop] for Runner.run_cpcv.

    Builds a per-group BarLoop scoped to [group_start, group_end] over the full
    study union (so the stitched per-path curves are consistent), with the
    group's rebalances as both the policy calendar and the signal gate. run_cpcv
    runs in-process so this need not pickle.
    """

    recipe: MomentumStudyRecipe

    def __call__(self, group_start: date, group_end: date) -> BarLoop:
        window_rebals = tuple(
            d
            for d in self.recipe.rebalance_dates
            if group_start <= d <= group_end
        )
        window_recipe = attrs.evolve(
            self.recipe,
            start_dt=group_start,
            end_dt=group_end,
            rebalance_dates=window_rebals,
        )
        return _build_momentum_bar_loop(window_recipe)


def _monthly_returns_from_curve(
    equity_curve: pl.DataFrame, rebalance_dates: tuple[date, ...]
) -> list[float]:
    """Per-month returns = pct_change of the NAV sampled at each rebalance date.

    The strategy holds for one month between rebalances, so the rebalance-date
    NAVs are the natural monthly series (240 obs, not the ~5000 daily bars); the
    bootstrap block length is chosen on this series' autocorrelation.
    """
    at_rebalance = (
        equity_curve.filter(pl.col("dt").is_in(list(rebalance_dates)))
        .sort("dt")
    )
    navs = [float(v) for v in at_rebalance["nav"].to_list()]
    return [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs))]


def choose_block_length(
    monthly_returns: list[float], max_lag: int = 6
) -> tuple[float, dict[int, float]]:
    """Pick the stationary-bootstrap block length from the MEASURED ACF.

    Returns (expected_block_length, {lag: autocorrelation}). The block length is
    the largest lag (1..max_lag) whose sample autocorrelation is significant at
    the +/- 2/sqrt(n) band, plus one (so a block spans the dependence), floored
    at 2.0 (the bootstrap requires > 1.0; 2.0 is the minimal serial-dependence
    block). This defends the value on the data rather than a magic number.
    """
    n = len(monthly_returns)
    if n < 3:
        return 2.0, {}
    mean = sum(monthly_returns) / n
    var = sum((r - mean) ** 2 for r in monthly_returns)
    acf: dict[int, float] = {}
    for lag in range(1, max_lag + 1):
        if var <= 0.0 or n - lag < 1:
            acf[lag] = 0.0
            continue
        cov = sum(
            (monthly_returns[t] - mean) * (monthly_returns[t + lag] - mean)
            for t in range(n - lag)
        )
        acf[lag] = cov / var
    threshold = 2.0 / math.sqrt(n)
    significant = [lag for lag in range(1, max_lag + 1) if abs(acf[lag]) > threshold]
    block = float(max(significant) + 1) if significant else 2.0
    return max(2.0, block), acf


@attrs.frozen(slots=True)
class MomentumStudyReport:
    """The composed study result (the renderable + the assertable numbers)."""

    result: BacktestResult
    n_rebalances: int
    n_monthly_returns: int
    union_size: int
    member_count_min: int
    member_count_max: int
    cpcv_path_count: int
    cpcv_sr_min: float
    cpcv_sr_max: float
    block_length: float
    acf: dict[int, float]
    realized_monthly_sharpe: float
    bootstrap_sharpe_p5: float
    bootstrap_sharpe_p50: float
    bootstrap_sharpe_p95: float
    n_bootstrap: int
    markdown: str


def compute_momentum_study_report(
    recipe: MomentumStudyRecipe,
    universe: SharadarSP500Universe,
    *,
    n_bootstrap: int = 1000,
    seed: int = 20260601,
) -> MomentumStudyReport:
    """Compose the honest momentum study from the zero-cost surfaces.

    Records exactly ONE study trial (the contiguous run) into a throwaway
    registry at naive_effective_n=1, so DSR == PSR-against-zero (the honest
    single-strategy choice). run_cpcv isolates its phi-identical path trials
    into a `::cpcv_paths` sub-family, so the study family is untouched.
    """
    rebalance_dates = recipe.rebalance_dates
    member_counts = [
        len(universe.members_at(datetime.combine(d, time(16, 0))))
        for d in rebalance_dates
    ]

    # 1. The contiguous zero-cost backtest = the level + Sharpe reference.
    _log.info("study_contiguous_run_begin n_rebalances=%d", len(rebalance_dates))
    contig = _build_momentum_bar_loop(recipe).run(
        start_dt=recipe.start_dt, end_dt=recipe.end_dt
    )

    # A fresh tempfile registry per run (no cross-run trial accumulation); the
    # single recorded study trial at naive=1 makes DSR degenerate to PSR.
    with tempfile.TemporaryDirectory() as tmp:
        registry = TrialRegistry(Path(tmp) / "study.db", naive_effective_n=1)
        result = to_backtest_result(
            contig,
            registry=registry,
            strategy_family=STRATEGY_FAMILY,
            universe_id=UNIVERSE_ID,
            periods_per_year=252,
        )

        # 2. run_cpcv: the deterministic-factor degeneracy (paths coincide).
        _log.info("study_cpcv_begin")
        splitter = CPCVSplitter(n_groups=_CPCV_N_GROUPS, k_test=_CPCV_K_TEST)
        observations, label_horizons = build_observations(rebalance_dates)
        with warnings.catch_warnings():
            # N=6, k=2 -> 5 paths, which is below BacktestPathDistribution's
            # stability threshold (30) and warns. For a deterministic factor
            # that sparse count is EXPECTED (the paths coincide; this is the
            # degeneracy the report discusses explicitly), so suppress the
            # generic warning here rather than leaking it to the CLI / a
            # filterwarnings=error test. The report's "CPCV degeneracy" section
            # is the honest, study-specific replacement.
            warnings.filterwarnings(
                "ignore", message="CPCV path count", category=UserWarning
            )
            cpcv_dist = Runner().run_cpcv(
                splitter,
                observations,
                label_horizons,
                _MomentumCpcvFactory(recipe),
                registry=registry,
                strategy_family=STRATEGY_FAMILY,
                universe_id=UNIVERSE_ID,
                periods_per_year=252,
            )
    cpcv_srs = [
        cpcv_dist.p10().sr_hat,
        cpcv_dist.median().sr_hat,
        cpcv_dist.p90().sr_hat,
    ]

    # 3. The headline path surface: a stationary block bootstrap of the MONTHLY
    #    returns, block length chosen from the measured monthly ACF.
    monthly = _monthly_returns_from_curve(contig.equity_curve, rebalance_dates)
    block_length, acf = choose_block_length(monthly)
    realized_sharpe = _sharpe(monthly)
    paths = stationary_block_bootstrap(
        monthly, n_paths=n_bootstrap, expected_block_length=block_length, seed=seed
    )
    boot_sharpes = sorted(_sharpe(p) for p in paths)
    p5, p50, p95 = (
        _percentile(boot_sharpes, 5.0),
        _percentile(boot_sharpes, 50.0),
        _percentile(boot_sharpes, 95.0),
    )

    report = MomentumStudyReport(
        result=result,
        n_rebalances=contig.n_rebalances,
        n_monthly_returns=len(monthly),
        union_size=len(recipe.union_tickers),
        member_count_min=min(member_counts),
        member_count_max=max(member_counts),
        cpcv_path_count=cpcv_dist.path_count,
        cpcv_sr_min=min(cpcv_srs),
        cpcv_sr_max=max(cpcv_srs),
        block_length=block_length,
        acf=acf,
        realized_monthly_sharpe=realized_sharpe,
        bootstrap_sharpe_p5=p5,
        bootstrap_sharpe_p50=p50,
        bootstrap_sharpe_p95=p95,
        n_bootstrap=n_bootstrap,
        markdown="",
    )
    return attrs.evolve(report, markdown=render_study_markdown(report))


def _sharpe(returns: list[float]) -> float:
    """Per-period Sharpe (mean / sample-stdev) of a return series; 0 if flat."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    return mean / std if std > 0.0 else 0.0


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile (matches BacktestPathDistribution's convention)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def render_study_markdown(report: MomentumStudyReport) -> str:
    """Compose the scorecard render + the study-specific honest sections."""
    r = report
    lines: list[str] = []
    lines.append("# JT1993 12-1 Momentum, Top-Quintile Long: PIT S&P 500 Study")
    lines.append("")
    lines.append(r.result.scorecard.to_markdown())
    lines.append("")
    lines.append(
        "_The Implementation Shortfall section above is structurally zero: this "
        "is the zero-cost reference run (no commission, slippage, or impact "
        "model is wired). The cost-sensitivity band is PR 3c; the $0.00 values "
        "are NOT a realized-cost estimate._"
    )
    lines.append("")
    lines.append("## Universe coverage")
    lines.append("")
    lines.append(
        f"The scorecard `Assets` value ({r.union_size}) is the "
        f"survivorship-safe ever-member union over the {r.n_rebalances} "
        f"rebalances (the BarLoop's fixed ticker tuple), NOT the held count. "
        f"The held set each rebalance is the top quintile of the "
        f"{r.member_count_min}-{r.member_count_max} members "
        f"(`ceil(n/5)`), i.e. roughly {r.member_count_max // 5} names (fewer when "
        f"a member lacks the ~12-month history the signal needs); turnover "
        f"replaces names as membership and momentum ranks change. members_at is "
        f"as-of (the most-recent quarterly snapshot), so the union is "
        f"survivorship-bias-free."
    )
    lines.append("")
    lines.append("## CPCV degeneracy (deterministic factor)")
    lines.append("")
    lines.append(
        f"CPCV produces {r.cpcv_path_count} reconstructed paths (N="
        f"{_CPCV_N_GROUPS}, k={_CPCV_K_TEST}). For a DETERMINISTIC factor like "
        f"momentum there is no fitted parameter whose retraining could vary the "
        f"paths, so they COINCIDE: the per-path Sharpe ranges only "
        f"[{r.cpcv_sr_min:.6f}, {r.cpcv_sr_max:.6f}] (float reassociation, not "
        f"genuine dispersion). This is the instructive finding of ADR 0016 "
        f"decision 4, not a fan: CPCV is the wrong path-uncertainty tool for a "
        f"factor with no estimated parameters, so the genuine path surface is "
        f"the bootstrap below."
    )
    lines.append("")
    lines.append("## Path uncertainty (stationary block bootstrap)")
    lines.append("")
    acf_str = ", ".join(f"lag{lag}={r.acf[lag]:+.3f}" for lag in sorted(r.acf))
    lines.append(
        f"The headline path-uncertainty surface is a Politis-Romano stationary "
        f"block bootstrap of the MONTHLY return series ({r.n_monthly_returns} "
        f"observations, the rebalance-to-rebalance NAV returns). The expected "
        f"block length is {r.block_length:.1f} months, chosen from the measured "
        f"monthly autocorrelation "
        f"({acf_str}; significant beyond +/- 2/sqrt(n)). The realized monthly "
        f"Sharpe is {r.realized_monthly_sharpe:.4f}; the bootstrap Sharpe "
        f"distribution over {r.n_bootstrap} paths is p5={r.bootstrap_sharpe_p5:.4f}"
        f" / p50={r.bootstrap_sharpe_p50:.4f} / p95={r.bootstrap_sharpe_p95:.4f}. "
        f"This nonparametric path surface COMPLEMENTS the parametric PSR/DSR "
        f"(they are different uncertainty objects, ADR 0016 dec 6); with "
        f"{r.n_monthly_returns} monthly observations the fan is statistically thin."
    )
    lines.append("")
    lines.append("## Honest DSR conclusion")
    lines.append("")
    dsr = r.result.dsr
    if dsr is None:
        verdict = (
            "the Deflated Sharpe is undefined (the realized Sharpe does not "
            "exceed the benchmark), so the strategy does not clear the bar."
        )
    elif dsr >= _DSR_THRESHOLD:
        verdict = (
            f"the zero-cost Deflated Sharpe is {dsr:.4f} >= {_DSR_THRESHOLD}, so "
            f"the strategy clears the deflated-Sharpe bar BEFORE costs."
        )
    else:
        verdict = (
            f"the zero-cost Deflated Sharpe is {dsr:.4f} < {_DSR_THRESHOLD}, so "
            f"the strategy does NOT clear the deflated-Sharpe bar even before "
            f"costs. This is a negative result, reported plainly per the "
            f"kill-early / intellectual-honesty thesis."
        )
    lines.append(
        f"The DSR is computed at naive_effective_n=1 (ONE pre-specified "
        f"strategy, no multiple-testing family), so it degenerates to the "
        f"PSR-against-zero on the single realized return series (ADR 0013 dec "
        f"5); there is no after-cost claim here (the cost band is PR 3c). "
        f"Conclusion: {verdict}"
    )
    lines.append("")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start-dt", type=date.fromisoformat, default=date(2005, 1, 4))
    parser.add_argument("--end-dt", type=date.fromisoformat, default=date(2024, 12, 31))
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--bundle-prefix", default="sharadar")
    parser.add_argument("--snapshots-root", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--n-bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260601)
    parser.add_argument("--output", type=Path, default=None, help="write the Markdown report here")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (0 ok, 2 missing/bad bundle)."""
    args = _parse_args(argv)
    configure_logging(level=getattr(logging, args.log_level))
    snapshots_root = args.snapshots_root.resolve()
    bundle_name = discover_latest_bundle(snapshots_root, args.bundle_prefix)
    if bundle_name is None:
        print(
            f"no snapshot under {snapshots_root} matching prefix "
            f"{args.bundle_prefix}_; pull per docs/methodology/"
            f"dataset_versioning.md to run this study",
            file=sys.stderr,
        )
        return 2
    try:
        source = SharadarDataSource(bundle_name, snapshots_root)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(
            f"bundle {bundle_name!r} under {snapshots_root} is malformed "
            f"({type(exc).__name__}: {exc}); run scripts/sharadar_pull.py "
            f"--refresh-hashes to rebuild the manifest.",
            file=sys.stderr,
        )
        return 2

    universe = SharadarSP500Universe(source)
    rebalance_dates = momentum_rebalance_dates(args.start_dt, args.end_dt)
    if not rebalance_dates:
        print("no monthly rebalances in the window", file=sys.stderr)
        return 2
    union = build_ever_member_union(universe, rebalance_dates)
    # Start the contiguous backtest at the first rebalance so there is no
    # leading all-cash drag in the first calendar year (the policy buys on the
    # first bar). The CPCV groups derive their windows from the same calendar.
    recipe = MomentumStudyRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name=bundle_name,
        start_dt=rebalance_dates[0],
        end_dt=args.end_dt,
        initial_capital=args.initial_capital,
        rebalance_dates=rebalance_dates,
        union_tickers=tuple(int(a) for a in union),
    )
    report = compute_momentum_study_report(
        recipe, universe, n_bootstrap=args.n_bootstrap, seed=args.seed
    )
    if args.output is not None:
        args.output.write_text(report.markdown, encoding="utf-8")
        _log.info("wrote study report to %s", args.output)
    print(report.markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
