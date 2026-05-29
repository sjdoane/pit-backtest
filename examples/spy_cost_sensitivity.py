"""SPY cost-sensitivity band CLI demo (M2 PR C1).

Per ADR 0002 M2 acceptance criterion 2 and ADR 0010 the sensitivity
band renders five SPY equity curves at eta in [0.05, 0.10, 0.142, 0.20,
0.30] anchored on the central eta=0.142 per ADR 0007's revised criterion.

Per ADR 0010 lock #8 the factory and recipe live at module scope so
they pickle under multiprocessing.spawn on both Linux and Windows.

Per ADR 0010 lock #9 the CLI flags are:
  --start-dt YYYY-MM-DD       default 2005-01-04
  --end-dt YYYY-MM-DD          default 2024-12-31
  --ticker SPY                  single-ticker sensitivity demo
  --initial-capital 1000000     default $1M per ADR 0007 FIM-ceiling test
  --bundle-prefix sharadar      discovers latest sharadar_YYYY-MM-DD
  --snapshots-root data/snapshots
  --workers N                   default min(5, max(1, cpu_count() - 1))
  --log-level INFO

Exit codes:
  0 on success
  1 on cost-model-not-applicable (universe lacks the ticker)
  2 on missing snapshot
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import attrs

from pit_backtest.analytics.sensitivity import SensitivityBand
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.base import ImpactedPriceSource
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.engine.m1_demo import (
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)
from pit_backtest.engine.runner import Runner
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.cost.commission import PerShareCommission
from pit_backtest.execution.cost.impact import (
    MarketStateLookup,
    MarketStateRow,
    SquareRootImpactCostModel,
)
from pit_backtest.execution.matching import SquareRootImpactMatchingEngine
from pit_backtest.policy.equal_weight import EqualWeightMonthlyRebalancePolicy
from pit_backtest.signal.equal_weight import EqualWeightSignal
from pit_backtest.utils.logging import configure_logging


_DEFAULT_ETA_GRID: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.142"),
    Decimal("0.20"),
    Decimal("0.30"),
)
_CENTRAL_ETA: Decimal = Decimal("0.142")


@attrs.frozen(slots=True)
class SpyCostSensitivityRecipe:
    """Picklable bundle of fixed inputs for the sensitivity sweep.

    Per ADR 0010 lock #8 the recipe carries only picklable types
    (str for paths, primitives for dates and floats). The worker
    rebuilds the SharadarDataSource from the bundle name and
    snapshots_root inside the spawn.
    """

    snapshots_root: str
    bundle_name: str
    ticker: str
    start_dt: date
    end_dt: date
    initial_capital: float


def build_bar_loop_for_eta(
    params: dict[str, object],
    recipe: SpyCostSensitivityRecipe,
) -> BarLoop:
    """Module-level factory per ADR 0010 lock #6.

    Constructs a single BarLoop for a specific eta value. The factory is
    pickled with the recipe via partial-application at the runner call
    site (see _factory_partial below).
    """
    eta_obj = params["eta"]
    if not isinstance(eta_obj, Decimal):
        eta = Decimal(str(eta_obj))
    else:
        eta = eta_obj

    snapshots_root = Path(recipe.snapshots_root)
    data_source = SharadarDataSource(recipe.bundle_name, snapshots_root)
    clock = TestClock(start_dt=recipe.start_dt, end_dt=recipe.end_dt)
    asset_id = ticker_to_asset_id(recipe.ticker)
    asset_ids: tuple[AssetId, ...] = (asset_id,)
    universe = fixed_universe_from_tickers((recipe.ticker,))
    rebalance_dates = monthly_last_trading_day(clock.trading_days())
    signal = EqualWeightSignal(tickers=asset_ids)

    prices_frame = data_source.read_sep_prices(
        ticker=recipe.ticker, start_dt=recipe.start_dt, end_dt=recipe.end_dt
    )
    price_index: dict[tuple[AssetId, date], float] = {}
    market_state_by_key: dict[tuple[AssetId, date], MarketStateRow] = {}
    for row in prices_frame.iter_rows(named=True):
        d = row["dt"]
        price_index[(asset_id, d)] = float(row["closeunadj"])
        # MarketStateRow for the Almgren formula. sigma_D, V_D, Theta
        # are SPY-typical values per tests/execution/cost/test_impact.py.
        # In M3+ these come from compute_rolling_daily_vol /
        # compute_rolling_adv + shares-outstanding feeds.
        market_state_by_key[(asset_id, d)] = MarketStateRow(
            sigma_D=0.012, V_D=80_000_000.0, Theta=8_700_000_000.0
        )

    def price_lookup(asset_id_arg: AssetId, dt: object) -> float | None:
        d = dt.date() if hasattr(dt, "date") else dt  # type: ignore[union-attr]
        return price_index.get((asset_id_arg, d))  # type: ignore[arg-type]

    policy = EqualWeightMonthlyRebalancePolicy(
        rebalance_dates=rebalance_dates, price_lookup=price_lookup
    )
    market_state_lookup = MarketStateLookup(by_key=market_state_by_key)
    cost_model = SquareRootImpactCostModel(
        market_state=market_state_lookup, eta=eta
    )
    impacted_source = ImpactedPriceSource(raw=data_source)
    commission = PerShareCommission(rate_per_share=Decimal("0.005"))
    matcher = SquareRootImpactMatchingEngine(
        clock=clock,
        cost_model=cost_model,
        commission=commission,
        impacted_source=impacted_source,
    )

    return BarLoop(
        data_source=data_source,
        universe=universe,
        signal=signal,
        policy=policy,
        matching_engine=matcher,
        clock=clock,
        tickers=asset_ids,
        initial_capital=recipe.initial_capital,
        impacted_source=impacted_source,
        cost_estimator=cost_model,
    )


@attrs.frozen(slots=True)
class _FactoryPartial:
    """Picklable closure-equivalent that binds the recipe to the factory.

    The runner's bar_loop_factory signature is
    Callable[[dict[str, object]], BarLoop]; we adapt the
    build_bar_loop_for_eta(params, recipe) signature by pre-binding the
    recipe. This is the module-level alternative to functools.partial
    because functools.partial does not always pickle cleanly under
    multiprocessing.spawn on Windows when the wrapped function lives in
    __main__.
    """

    recipe: SpyCostSensitivityRecipe

    def __call__(self, params: dict[str, object]) -> BarLoop:
        return build_bar_loop_for_eta(params, self.recipe)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start-dt", type=date.fromisoformat, default=date(2005, 1, 4))
    parser.add_argument("--end-dt", type=date.fromisoformat, default=date(2024, 12, 31))
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--bundle-prefix", default="sharadar")
    parser.add_argument(
        "--snapshots-root",
        type=Path,
        default=Path("data/snapshots"),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="number of worker processes (default: min(5, cpu_count() - 1))",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    configure_logging(level=getattr(logging, args.log_level))

    snapshots_root = args.snapshots_root.resolve()
    bundle_name = discover_latest_bundle(snapshots_root, args.bundle_prefix)
    if bundle_name is None:
        print(
            f"no snapshot under {snapshots_root} matching prefix "
            f"{args.bundle_prefix}_; pull per docs/methodology/"
            f"dataset_versioning.md to run this demo",
            file=sys.stderr,
        )
        return 2

    try:
        ticker_to_asset_id(args.ticker)
    except KeyError:
        print(
            f"ticker {args.ticker!r} is not in the M1 demo universe; "
            f"the cost-sensitivity demo accepts only SPY, AGG, GLD at v1",
            file=sys.stderr,
        )
        return 1

    recipe = SpyCostSensitivityRecipe(
        snapshots_root=str(snapshots_root),
        bundle_name=bundle_name,
        ticker=args.ticker,
        start_dt=args.start_dt,
        end_dt=args.end_dt,
        initial_capital=args.initial_capital,
    )

    # Verify the bundle's manifest is well-formed up-front so a
    # corrupt-bundle case surfaces as exit 2 (per post-impl reviewer
    # High finding) rather than an uncaught exception inside the spawn
    # bootstrap.
    try:
        SharadarDataSource(bundle_name, snapshots_root)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(
            f"bundle {bundle_name!r} under {snapshots_root} is malformed "
            f"({type(e).__name__}: {e}); the discovery found a matching "
            f"directory but the manifest entry is missing or invalid. "
            f"Run scripts/sharadar_pull.py --refresh-hashes to rebuild "
            f"the manifest.",
            file=sys.stderr,
        )
        return 2

    param_grid: list[dict[str, object]] = [
        {"eta": eta} for eta in _DEFAULT_ETA_GRID
    ]
    factory = _FactoryPartial(recipe=recipe)

    if args.workers is not None:
        num_workers = args.workers
    else:
        num_workers = min(
            len(param_grid),
            max(1, multiprocessing.cpu_count() - 1),
        )

    runner = Runner(num_workers=num_workers)
    results = runner.run_sweep(
        param_grid=param_grid,
        bar_loop_factory=factory,
        start_dt=args.start_dt,
        end_dt=args.end_dt,
    )

    band = SensitivityBand.from_run_sweep(
        results=results,
        parameter_name="eta",
        parameter_values=_DEFAULT_ETA_GRID,
        central_value=_CENTRAL_ETA,
    )

    print(band.render_summary_line())
    print()
    print(band.render_band_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
