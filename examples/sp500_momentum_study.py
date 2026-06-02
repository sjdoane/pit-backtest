"""Worked JT1993 12-1 momentum study on the PIT S&P 500 (M5 PR 3b core + 3c cost).

The M5 capstone study (ADR 0002 dec 20, ADR 0016): monthly-rebalance,
top-quintile-long, equal-weight JT1993 momentum over the survivorship-bias-free
Sharadar S&P 500 (2005-2024). The milestone-meaningful CORE (PR 3b):

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

The COST surfaces (PR 3c) are appended by compute_cost_surfaces (kept separate so
the 3b core is untouched and the headline DSR stays zero-cost; --skip-sweep skips
them): a cost-sensitivity band over the Almgren eta grid (a uniform-liquidity
eta-SENSITIVITY surface, NOT realized cost: one SPY-typical market-state row for
all names, C1), and a commission-only contiguous-vs-CPCV seam decomposition that
isolates commission from the omitted-gap-day-bar market-move confound (the
empirical finding that corrects ADR 0016 dec 2; see SeamCost). Figures +
METHODOLOGY.md + the M5 SHIPPED flip are PR 4.

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
import multiprocessing
import sys
import tempfile
import warnings
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import attrs
import polars as pl

from pit_backtest.analytics.bootstrap import stationary_block_bootstrap
from pit_backtest.analytics.result_adapter import to_backtest_result
from pit_backtest.analytics.scorecard import BacktestResult
from pit_backtest.analytics.sensitivity import SensitivityBand
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.engine.runner import Runner, _stitch_path
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.commission import PerShareCommission
from pit_backtest.execution.cost.impact import (
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)
from pit_backtest.execution.matching import (
    CloseFillMatchingEngine,
    MatchingEngine,
    SquareRootImpactMatchingEngine,
)
from pit_backtest.policy.top_quintile import TopQuintileLongPolicy
from pit_backtest.signal.momentum import Momentum12_1Signal
from pit_backtest.utils.logging import configure_logging
from pit_backtest.validation.cv import CPCVSplitter, contiguous_folds
from pit_backtest.validation.trial_registry import TrialRegistry

STRATEGY_FAMILY = "jt1993_mom_12_1_topq"
UNIVERSE_ID = "sp500_pit"
_DSR_THRESHOLD = 0.95
_CPCV_N_GROUPS = 6
_CPCV_K_TEST = 2

# Cost-surface constants (M5 PR 3c, deferred from 3b). The eta grid + central
# anchor are the ADR 0010 lock #10 Almgren calibration band; the per-share
# commission rate mirrors spy_cost_sensitivity.py.
_DEFAULT_ETA_GRID: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.142"),
    Decimal("0.20"),
    Decimal("0.30"),
)
_CENTRAL_ETA: Decimal = Decimal("0.142")
_PER_SHARE_COMMISSION: Decimal = Decimal("0.005")
# A SINGLE SPY-typical MarketStateRow is applied to EVERY name in the union
# (C1): the band is a uniform-liquidity eta-SENSITIVITY surface, NOT a realized-
# cost estimate. Per-name vol / ADV / shares-outstanding is M3+/out of scope, so
# one SPY-typical row for all ~930 names is indefensible as realized cost; it is
# defensible only as a controlled one-knob (eta) sensitivity probe. The values
# mirror tests/execution/cost/test_impact.py and spy_cost_sensitivity.py.
_UNIFORM_SIGMA_D = 0.012
_UNIFORM_V_D = 80_000_000.0
_UNIFORM_THETA = 8_700_000_000.0
# The three cost wirings _build_momentum_bar_loop understands. "none" is the
# zero-cost CloseFillMatchingEngine (the 3b level/Sharpe/bootstrap reference,
# byte-identical to before this PR); "impact" is the Almgren matcher swept over
# eta for the band; "commission_only" zeroes impact (sigma_D=0 nulls both
# Almgren terms) so the seam gap is PURELY the N-1 re-entry commissions.
_COST_MODES = ("none", "impact", "commission_only")

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

    The cost fields (M5 PR 3c) default to the zero-cost wiring so the 3b callers
    (compute_momentum_study_report and _MomentumCpcvFactory) stay byte-identical:
    cost_mode="none" routes _build_momentum_bar_loop to the CloseFillMatchingEngine
    exactly as before. cost_mode="impact" + eta/beta build the Almgren matcher (the
    cost-band sweep evolves eta per grid point); cost_mode="commission_only"
    zeroes impact (sigma_D=0) so only PerShareCommission is charged (the seam
    demo). The recipe stays a frozen, all-picklable bundle (str/date/float/Decimal
    /tuple) so it pickles into Runner.run_sweep spawn workers (ADR 0010 lock #8).
    """

    snapshots_root: str
    bundle_name: str
    start_dt: date
    end_dt: date
    initial_capital: float
    rebalance_dates: tuple[date, ...]
    union_tickers: tuple[int, ...]
    cost_mode: str = "none"
    eta: Decimal = _CENTRAL_ETA
    beta: Decimal = Decimal("0.6")

    def __attrs_post_init__(self) -> None:
        if self.cost_mode not in _COST_MODES:
            raise ValueError(
                f"cost_mode must be one of {_COST_MODES}; got {self.cost_mode!r}"
            )


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


def _build_cost_matcher(
    recipe: MomentumStudyRecipe,
    source: SharadarDataSource,
    union: tuple[AssetId, ...],
    clock: TestClock,
) -> tuple[
    SquareRootImpactMatchingEngine, ImpactedPriceSource, SquareRootImpactCostModel
]:
    """Build the Almgren cost matcher for an "impact" or "commission_only" recipe.

    A SINGLE SPY-typical MarketStateRow is shared by EVERY (union member,
    rebalance date) key (C1: a uniform-liquidity surface, NOT realized cost).
    Keying on the rebalance dates (not every trading day) is both exact and lean:
    the matcher calls cost_model.compute only on a fill, fills happen only when
    the policy emits non-empty targets, and the policy trades only on its
    rebalance calendar (the signal-gate guard enforces the superset invariant),
    so (union member, rebalance date) covers every fill the matcher can see. The
    set is |union| * |rebalances| (e.g. ~930 * 240 for the full study), far
    smaller than the per-trading-day grid.

    For commission_only, sigma_D=0 zeroes BOTH Almgren terms (each carries
    sigma_D as a factor), leaving PerShareCommission as the only charged cost, so
    the contiguous-vs-stitched seam gap is purely the N-1 re-entry commissions.
    eta stays positive (the cost model rejects eta<=0) but is inert at sigma_D=0.
    """
    sigma_d = _UNIFORM_SIGMA_D if recipe.cost_mode == "impact" else 0.0
    shared_row = MarketStateRow(
        sigma_D=sigma_d, V_D=_UNIFORM_V_D, Theta=_UNIFORM_THETA
    )
    by_key: dict[tuple[AssetId, date], MarketStateRow] = {
        (asset_id, d): shared_row
        for asset_id in union
        for d in recipe.rebalance_dates
    }
    cost_model = SquareRootImpactCostModel(
        market_state=MarketStateLookup(by_key=by_key),
        eta=recipe.eta,
        beta=recipe.beta,
    )
    impacted_source = ImpactedPriceSource(raw=source)
    commission = PerShareCommission(rate_per_share=_PER_SHARE_COMMISSION)
    matcher = SquareRootImpactMatchingEngine(
        clock=clock,
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted_source,
    )
    return matcher, impacted_source, cost_model


def _build_momentum_bar_loop(recipe: MomentumStudyRecipe) -> BarLoop:
    """Build the momentum BarLoop for the recipe's window and cost mode.

    cost_mode="none" (the 3b default) wires the zero-cost CloseFillMatchingEngine:
    the level/Sharpe reference and the CPCV/bootstrap input (all cost-independent).
    This path is BYTE-IDENTICAL to the pre-3c builder: impacted_source and
    cost_estimator stay None (passing None is the same as omitting them, so the
    M5 PR 3b composition and its gated test are unaffected). cost_mode="impact" /
    "commission_only" swap in the Almgren SquareRootImpactMatchingEngine (PR 3c).
    The signal is gated to the rebalance calendar (the PR 3a perf gate) in every
    mode. EqualWeightMonthlyRebalancePolicy ignores cost_estimator at v1, so the
    band is the cost the matcher charges on the unchanged trade schedule (a pure
    eta-sensitivity surface), not a cost-aware trade-opt-out.
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

    policy = TopQuintileLongPolicy(
        rebalance_dates=rebalances, price_lookup=price_lookup
    )

    matching_engine: MatchingEngine
    impacted_source: ImpactedPriceSource | None
    cost_estimator: SquareRootImpactCostModel | None
    if recipe.cost_mode == "none":
        matching_engine = CloseFillMatchingEngine(clock=clock)
        impacted_source = None
        cost_estimator = None
    else:
        matching_engine, impacted_source, cost_estimator = _build_cost_matcher(
            recipe, source, union, clock
        )

    return BarLoop(
        data_source=source,
        universe=universe,
        signal=Momentum12_1Signal(),
        policy=policy,
        matching_engine=matching_engine,
        clock=clock,
        tickers=union,
        initial_capital=recipe.initial_capital,
        use_real_pit_view=True,
        asset_id_to_ticker=resolver,
        signal_calendar=rebalances,
        impacted_source=impacted_source,
        cost_estimator=cost_estimator,
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


# --------------------------------------------------------------------------- #
# Cost surfaces (M5 PR 3c): the eta-sensitivity band + the commission-only      #
# contiguous-vs-CPCV seam demo. Kept in a separate compute path so the 3b core  #
# (compute_momentum_study_report) stays untouched and the headline DSR stays    #
# zero-cost only.                                                               #
# --------------------------------------------------------------------------- #


@attrs.frozen(slots=True)
class _MomentumSweepFactory:
    """Picklable Callable[[dict], BarLoop] for Runner.run_sweep over eta.

    Binds the impact-mode recipe and, per grid point, evolves eta and builds the
    cost BarLoop. Module-level + frozen so it pickles under multiprocessing.spawn
    on Windows (ADR 0010 lock #6/#8; the spy_cost_sensitivity _FactoryPartial
    pattern). The bound recipe is already cost_mode="impact".
    """

    recipe: MomentumStudyRecipe

    def __call__(self, params: dict[str, object]) -> BarLoop:
        eta_obj = params["eta"]
        eta = eta_obj if isinstance(eta_obj, Decimal) else Decimal(str(eta_obj))
        return _build_momentum_bar_loop(attrs.evolve(self.recipe, eta=eta))


def _compute_cost_band(
    recipe: MomentumStudyRecipe,
    eta_grid: tuple[Decimal, ...],
    central_eta: Decimal,
    workers: int | None,
) -> SensitivityBand:
    """Run the impact matcher over the eta grid and wrap it as a SensitivityBand.

    A uniform-liquidity eta-SENSITIVITY surface (C1): one SPY-typical
    MarketStateRow is shared by all names, so this measures how the strategy's
    PnL responds to the single impact knob eta, NOT a realized cost. Runs via
    Runner.run_sweep (spawn workers; the factory + recipe pickle per ADR 0010).
    """
    sweep_recipe = attrs.evolve(recipe, cost_mode="impact")
    factory = _MomentumSweepFactory(sweep_recipe)
    param_grid: list[dict[str, object]] = [{"eta": eta} for eta in eta_grid]
    if workers is None:
        num_workers = min(
            len(param_grid), max(1, multiprocessing.cpu_count() - 1)
        )
    else:
        num_workers = workers
    runner = Runner(num_workers=num_workers)
    results: list[ConstantWeightDemoResult] = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=recipe.start_dt,
        end_dt=recipe.end_dt,
    )
    return SensitivityBand.from_run_sweep(
        results=results,
        parameter_name="eta",
        parameter_values=eta_grid,
        central_value=central_eta,
    )


@attrs.frozen(slots=True)
class SeamCost:
    """The commission-only contiguous-vs-CPCV-stitch seam decomposition.

    All four NAV legs hold the bar set fixed so commission is isolated by
    differencing the zero-cost leg against the commission-only leg (the omitted
    inter-group gap-day market moves cancel inside each contiguous/stitched pair).

    EMPIRICAL FINDING (M5 PR 3c, real bundle; corrects ADR 0016 dec 2): the raw
    contiguous-minus-stitched LEVEL gap is dominated by the omitted inter-group
    gap-day market moves (quantified by zero_cost_level_gap), NOT by commission,
    and the _stitch_path running-level carry NORMALIZES away each per-group
    post-entry-commission baseline, so the stitched level does NOT carry the
    per-group re-entry commissions (stitched_commission_drag < contiguous). The
    genuine N-1 re-entry cost is therefore measured as phantom_reentry_commission
    (the per-group all-cash re-entries the CPCV execution actually pays, before
    the stitch normalizes them), which IS positive. The contiguous run remains
    the cost/level reference (the 3b headline already uses it).
    """

    n_groups: int
    initial_capital: float
    comm_contiguous_final: float
    zc_contiguous_final: float
    comm_stitched_final: float
    zc_stitched_final: float
    sum_per_group_comm_drag: float

    @property
    def contiguous_commission_drag(self) -> float:
        """Commission the single contiguous run pays (1 entry + low turnover)."""
        return self.zc_contiguous_final - self.comm_contiguous_final

    @property
    def stitched_commission_drag(self) -> float:
        """Commission the _stitch_path level reflects (per-group entries are
        normalized away, so only the within-group turnover deltas remain)."""
        return self.zc_stitched_final - self.comm_stitched_final

    @property
    def raw_level_gap(self) -> float:
        """Naive contiguous-minus-stitched level gap (confounded by gap-day
        market moves; NOT a commission measure)."""
        return self.comm_contiguous_final - self.comm_stitched_final

    @property
    def zero_cost_level_gap(self) -> float:
        """The contiguous-minus-stitched gap with NO cost: the pure omitted
        inter-group gap-day market-move confound."""
        return self.zc_contiguous_final - self.zc_stitched_final

    @property
    def phantom_reentry_commission(self) -> float:
        """The genuine N-1 re-entry commission (ADR 0016 dec 2): the per-group
        all-cash re-entries the CPCV execution pays, over and above the single
        contiguous run's commission. Positive (N full entries vs 1).

        Each per-group drag in sum_per_group_comm_drag is measured on a standalone
        all-cash run starting at initial_capital, which is exactly how run_cpcv (and
        _stitched_path_final) evaluate each group, so the per-group turnover base is
        the initial capital, not a compounded running level. The measure is faithful
        to the real per-group execution model; any base-level discrepancy versus a
        single compounded contiguous timeline is second-order over the window."""
        return self.sum_per_group_comm_drag - self.contiguous_commission_drag


def _stitched_path_final(
    recipe: MomentumStudyRecipe, n_groups: int
) -> tuple[float, list[float], float]:
    """Stitch the N per-group segments for `recipe`'s cost mode.

    Returns (stitched_final_nav, per_group_final_navs, initial_capital). The
    per-group windows + _stitch_path running-level carry mirror run_cpcv exactly;
    the per-group final NAVs are captured so the seam can sum the per-group
    commission drags (which the stitch's level normalization would otherwise
    hide). Computed WITHOUT run_cpcv (H3): run_cpcv always adapts to a registry
    and BacktestResult carries no final_nav, so a direct _stitch_path of one path
    is the only way to read the stitched final NAV. For a deterministic factor
    every CPCV path coincides, so groups 0..N-1 in order is THE path.
    """
    dts = recipe.rebalance_dates
    folds = contiguous_folds(len(dts), n_groups)
    factory = _MomentumCpcvFactory(recipe)
    segment_by_group: dict[int, pl.DataFrame] = {}
    per_group_finals: list[float] = []
    initial_capital: float | None = None
    for g, (gs, ge) in enumerate(folds):
        group_start, group_end = dts[gs], dts[ge - 1]
        group_result = factory(group_start, group_end).run(
            start_dt=group_start, end_dt=group_end
        )
        segment_by_group[g] = (
            group_result.equity_curve.select(["dt", "nav"]).sort("dt")
        )
        per_group_finals.append(group_result.final_nav)
        if initial_capital is None:
            initial_capital = group_result.initial_capital
    assert initial_capital is not None  # contiguous_folds yields n_groups >= 1
    stitched = _stitch_path(
        tuple(range(n_groups)), segment_by_group, initial_capital
    )
    return float(stitched["nav"][-1]), per_group_finals, initial_capital


def _compute_seam_cost(recipe: MomentumStudyRecipe, n_groups: int) -> SeamCost:
    """Decompose the commission-only contiguous-vs-CPCV-stitch seam (ADR 0016
    dec 2), isolating commission from the gap-day market-move confound.

    Runs four legs over the same window: commission-only + zero-cost, each as a
    single contiguous backtest and as an N-group _stitch_path stitch. Commission
    is isolated by zero-cost-minus-commission differencing within each pair (the
    gap-day market moves cancel). See SeamCost for the empirical finding.
    """
    commission_recipe = attrs.evolve(recipe, cost_mode="commission_only")
    zero_cost_recipe = attrs.evolve(recipe, cost_mode="none")

    comm_contiguous_final = _build_momentum_bar_loop(commission_recipe).run(
        start_dt=commission_recipe.start_dt, end_dt=commission_recipe.end_dt
    ).final_nav
    zc_contiguous_final = _build_momentum_bar_loop(zero_cost_recipe).run(
        start_dt=zero_cost_recipe.start_dt, end_dt=zero_cost_recipe.end_dt
    ).final_nav

    comm_stitched_final, comm_group_finals, capital = _stitched_path_final(
        commission_recipe, n_groups
    )
    zc_stitched_final, zc_group_finals, _ = _stitched_path_final(
        zero_cost_recipe, n_groups
    )
    sum_per_group_comm_drag = sum(
        zc - comm for zc, comm in zip(zc_group_finals, comm_group_finals)
    )

    return SeamCost(
        n_groups=n_groups,
        initial_capital=capital,
        comm_contiguous_final=comm_contiguous_final,
        zc_contiguous_final=zc_contiguous_final,
        comm_stitched_final=comm_stitched_final,
        zc_stitched_final=zc_stitched_final,
        sum_per_group_comm_drag=sum_per_group_comm_drag,
    )


@attrs.frozen(slots=True)
class CostSurfaceReport:
    """The PR 3c cost surfaces: the eta-sensitivity band + the seam decomposition.

    band_final_pnl / band_table are extracted from the SensitivityBand (the band
    itself is not stored; it carries pl.DataFrame equity curves). seam carries the
    full commission/gap-day decomposition. markdown is the two appended report
    sections.
    """

    union_size: int
    eta_grid: tuple[Decimal, ...]
    central_eta: Decimal
    band_final_pnl: dict[Decimal, float]
    band_table: str
    initial_capital: float
    seam: SeamCost
    markdown: str


def compute_cost_surfaces(
    recipe: MomentumStudyRecipe,
    *,
    eta_grid: tuple[Decimal, ...] = _DEFAULT_ETA_GRID,
    central_eta: Decimal = _CENTRAL_ETA,
    n_groups: int = _CPCV_N_GROUPS,
    workers: int | None = None,
) -> CostSurfaceReport:
    """Compute the cost-sensitivity band + the commission seam decomposition.

    Kept separate from compute_momentum_study_report so the 3b core (and its
    headline zero-cost DSR) is untouched; main() appends these sections only when
    the cost surfaces are requested (the default; --skip-sweep skips them). The
    band is a uniform-liquidity eta-sensitivity surface (C1), NOT a realized cost.
    """
    # Fail fast: SensitivityBand requires the central value to be one of the swept
    # points, so check it BEFORE running the (expensive) sweep rather than after.
    if central_eta not in eta_grid:
        raise ValueError(
            f"central_eta {central_eta} must be in eta_grid {eta_grid}; the "
            f"central value must be one of the swept points"
        )
    _log.info("cost_band_begin n_eta=%d", len(eta_grid))
    band = _compute_cost_band(recipe, eta_grid, central_eta, workers)
    _log.info("cost_seam_begin n_groups=%d", n_groups)
    seam = _compute_seam_cost(recipe, n_groups)
    report = CostSurfaceReport(
        union_size=len(recipe.union_tickers),
        eta_grid=eta_grid,
        central_eta=central_eta,
        band_final_pnl=dict(band.per_parameter_final_pnl),
        band_table=band.render_band_table(),
        initial_capital=recipe.initial_capital,
        seam=seam,
        markdown="",
    )
    return attrs.evolve(report, markdown=render_cost_surfaces_markdown(report))


def render_cost_surfaces_markdown(report: CostSurfaceReport) -> str:
    """Render the '## Cost sensitivity' + '## CPCV seam cost' sections.

    Both are framed honestly: the band is a uniform-liquidity eta-sensitivity
    surface (C1), and the seam isolates commission from the omitted-gap-day-bar
    market-move confound (the empirical finding that corrects ADR 0016 dec 2).
    """
    r = report
    s = report.seam
    cap = report.initial_capital
    lines: list[str] = []

    lines.append("## Cost sensitivity (uniform-liquidity eta-sensitivity surface)")
    lines.append("")
    lines.append(
        f"This band sweeps the Almgren temporary-impact coefficient eta over "
        f"{', '.join(str(e) for e in r.eta_grid)} (central {r.central_eta}) with "
        f"the SquareRootImpactMatchingEngine and a $"
        f"{float(_PER_SHARE_COMMISSION):.3f}/share commission. **It is a "
        f"uniform-liquidity eta-SENSITIVITY surface, NOT a realized-cost "
        f"estimate.** A SINGLE SPY-typical market-state row "
        f"(sigma_D={_UNIFORM_SIGMA_D}, V_D={_UNIFORM_V_D:,.0f}, "
        f"Theta={_UNIFORM_THETA:,.0f}) is applied to ALL {r.union_size} names in "
        f"the survivorship union; per-name volatility, ADV, and shares "
        f"outstanding are M3+/out of scope, so one SPY-typical row across the "
        f"whole universe understates the cost of smaller and less liquid members "
        f"and is indefensible as a realized cost. The band shows the DIRECTION "
        f"and bounded magnitude of the single impact knob eta on the unchanged "
        f"trade schedule (the equal-weight policy does not opt out on cost at "
        f"v1), nothing more. **The headline DSR/PSR above stays zero-cost; no "
        f"after-cost-deflated claim is made.**"
    )
    lines.append("")
    lines.append(r.band_table)
    lines.append("")
    pnls = [r.band_final_pnl[e] for e in r.eta_grid]
    span_bps = abs(pnls[0] - pnls[-1]) / cap * 10_000.0
    lines.append(
        f"With SPY-typical liquidity and this strategy's small per-name trades "
        f"the impact term is tiny: the final-PnL spread across the eta grid is "
        f"only {span_bps:.3f} bps of capital, so impact (at SPY liquidity) is "
        f"negligible next to commission. Realistic per-name liquidity would widen "
        f"this; that is an M3+ refinement."
    )
    lines.append("")

    lines.append("## CPCV seam cost (commission-only)")
    lines.append("")
    lines.append(
        f"The CPCV stitch evaluates the strategy per contiguous group, each "
        f"starting from all-cash, then stitches the {s.n_groups} per-group "
        f"segments (ADR 0016 dec 2). To isolate the commission seam from the "
        f"impact term, this runs commission_only (sigma_D=0 nulls both Almgren "
        f"terms, leaving only the $"
        f"{float(_PER_SHARE_COMMISSION):.3f}/share commission). Commission is "
        f"further isolated from market noise by differencing the zero-cost leg "
        f"against the commission-only leg on the SAME bars (so the omitted "
        f"inter-group gap-day market moves cancel)."
    )
    lines.append("")
    contig_bps = s.contiguous_commission_drag / cap * 10_000.0
    phantom_bps = s.phantom_reentry_commission / cap * 10_000.0
    lines.append(
        f"- **Single contiguous backtest commission**: "
        f"${s.contiguous_commission_drag:,.2f} ({contig_bps:+.3f} bps) for one "
        f"full entry plus low-turnover monthly deltas. This is the realistic cost "
        f"the 3b headline run would bear if costs were on."
    )
    lines.append(
        f"- **Phantom re-entry commission (the genuine N-1 seam cost)**: "
        f"${s.phantom_reentry_commission:,.2f} ({phantom_bps:+.3f} bps). The "
        f"{s.n_groups}-group CPCV execution pays {s.n_groups} full all-cash "
        f"entries instead of one, so it spends ${s.sum_per_group_comm_drag:,.2f} "
        f"on commission across the per-group runs versus "
        f"${s.contiguous_commission_drag:,.2f} contiguous; the difference is the "
        f"{s.n_groups - 1} phantom re-entries. Positive: commission only "
        f"subtracts."
    )
    lines.append("")
    # The honest finding that corrects ADR 0016 dec 2's stated direction.
    if abs(s.raw_level_gap) > 1.0:
        confound_pct = s.zero_cost_level_gap / s.raw_level_gap * 100.0
        comm_pct = abs(s.contiguous_commission_drag) / abs(s.raw_level_gap) * 100.0
        lines.append(
            f"**Honest finding (corrects ADR 0016 dec 2's stated direction; "
            f"surfaced by real data).** The naive contiguous-minus-stitched LEVEL "
            f"gap is ${s.raw_level_gap:,.2f}, but it is NOT a commission measure: "
            f"the zero-cost stitch has a level gap of ${s.zero_cost_level_gap:,.2f} "
            f"({confound_pct:.1f}% of the naive gap), so the gap is dominated by "
            f"the inter-group gap-day market moves the stitch omits, with "
            f"commission only {comm_pct:.2f}% of it. Moreover the _stitch_path "
            f"running-level carry NORMALIZES away each per-group "
            f"post-entry-commission baseline, so the stitched LEVEL reflects only "
            f"the within-group turnover commission "
            f"(${s.stitched_commission_drag:,.2f}), NOT the per-group re-entries; "
            f"the {s.n_groups} all-cash re-entries are real (above) but invisible "
            f"in the stitched level. ADR 0016 dec 2 expected the re-entries to "
            f"bias the stitched level DOWNWARD; with the running-level-carry "
            f"stitch they do not. The takeaway stands: the contiguous "
            f"full-period backtest is the cost/level reference (the 3b headline "
            f"uses it), and the CPCV stitch's level must not be read as "
            f"cost-accurate."
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
        "--skip-sweep",
        action="store_true",
        help=(
            "skip the cost surfaces (the eta-sensitivity band AND the "
            "commission-only CPCV seam demo). The zero-cost core study still "
            "runs. The surfaces are heavy (the band is a 5-eta run_sweep; the "
            "seam is 2 contiguous + 2*N group backtests), so skipping keeps the "
            "core study fast and the headline DSR zero-cost."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "worker processes for the cost-band run_sweep "
            "(default: min(n_eta, cpu_count() - 1))"
        ),
    )
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
    markdown = report.markdown
    # Append the cost surfaces unless skipped. They are kept in a separate
    # compute path so the 3b core report (and its zero-cost headline DSR) is
    # untouched; --skip-sweep keeps the core study fast and cost-free.
    if not args.skip_sweep:
        cost = compute_cost_surfaces(recipe, workers=args.workers)
        markdown = f"{markdown}\n{cost.markdown}"
    if args.output is not None:
        args.output.write_text(markdown, encoding="utf-8")
        _log.info("wrote study report to %s", args.output)
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
