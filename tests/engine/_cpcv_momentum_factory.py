"""Module-level momentum BarLoop factory + synthetic bundle for run_cpcv tests.

`Runner.run_cpcv` takes a `Callable[[date, date], BarLoop]` that builds a
backtest scoped to a contiguous [group_start, group_end] window. This module
provides that factory (as an `attrs.frozen` callable) wired with the real
`Momentum12_1Signal` + `TopQuintileLongPolicy` + `use_real_pit_view=True`, plus
the synthetic Sharadar bundle it runs against. The leading-underscore module
name keeps pytest from collecting it as a test module (matching the
`_runner_test_factories.py` convention).

Unlike `run_sweep`, `run_cpcv` does NOT pickle the factory (it runs the N
group-backtests in-process), so the factory may hold a recipe directly.

The synthetic bundle is the load-bearing part. Two design constraints drive it:

1. The headline degeneracy test needs NON-FLAT per-group equity segments, or
   `to_backtest_result` rejects each path as flat and every path is skipped.
   With a two-name universe `TopQuintileLongPolicy` selects `ceil(2/5)=1` name,
   so a single held winner drives the NAV. AAA therefore rises on a MONOTONE
   path with ALTERNATING daily increments (so its per-bar returns have strictly
   positive variance, never the zero variance an exponential constant-return
   path would give); BBB stays flat. AAA is always the momentum winner, so the
   selection is deterministic and the phi paths coincide.

2. The date columns are stored as `Datetime("ns")` to MATCH the real Sharadar
   bundle and exercise the `BarLoop._build_pit_view` pl.Date cast; a pl.Date
   fixture would mask the Critical-1 dtype trap (see test_bar_loop_pit_view).

History runs from 2009-12-01 (a buffer before the first rebalance's 12-month
lookback edge) through 2011-12-31; daily weekday bars cover every NYSE trading
day in both the lookback and the per-group run windows, so the held position is
priced on every bar (a sparse bar grid would drop the holding from the NAV on
unbarred days). Rebalances are the monthly last trading days of 2011 (12 of
them); over N=6 groups that is two rebalances per group, so every group window
spans roughly a month of daily bars and fires at least one rebalance.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

import attrs
import polars as pl

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.policy.top_quintile import TopQuintileLongPolicy
from pit_backtest.signal.momentum import Momentum12_1Signal

_AAA = AssetId(1001)  # the momentum winner (price rises over the lookback)
_BBB = AssetId(1002)  # the loser (flat)
_ASSET_TO_TICKER = {_AAA: "AAA", _BBB: "BBB"}

BUNDLE_NAME = "sharadar_cpcv_momentum"
HISTORY_START = date(2009, 12, 1)
LAST_DATE = date(2011, 12, 31)
SNAPSHOT_DATE = date(2010, 12, 31)  # sp500 historical snapshot listing AAA, BBB
INITIAL_CAPITAL = 100_000.0


def _resolve(asset_id: AssetId) -> str:
    return _ASSET_TO_TICKER[asset_id]


def _weekdays(start: date, end: date) -> list[date]:
    """Every Mon-Fri date in [start, end] (a superset of NYSE trading days)."""
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _bar(ticker: str, d: date, px: float) -> dict[str, object]:
    return {
        "ticker": ticker, "date": d,
        "open": px, "high": px, "low": px, "close": px, "closeunadj": px,
        "volume": 1_000_000,
    }


def _sep_rows() -> list[dict[str, object]]:
    """Daily weekday bars: AAA monotone-rising (alternating +1.0 / +0.5 daily
    increments, so per-bar returns vary -> non-zero variance), BBB flat at 100.
    """
    rows: list[dict[str, object]] = []
    aaa = 100.0
    for i, d in enumerate(_weekdays(HISTORY_START, LAST_DATE)):
        rows.append(_bar("AAA", d, aaa))
        rows.append(_bar("BBB", d, 100.0))
        aaa += 1.0 if i % 2 == 0 else 0.5
    return rows


def _tickers_rows() -> list[dict[str, object]]:
    return [
        {
            "permaticker": int(_AAA), "ticker": "AAA", "name": "Alpha Co",
            "exchange": "NYSE", "isdelisted": "N",
            "firstpricedate": HISTORY_START, "lastpricedate": None,
            "firstquarter": date(2009, 12, 31), "lastquarter": None,
            "cusip": "AAA00001",
        },
        {
            "permaticker": int(_BBB), "ticker": "BBB", "name": "Beta Co",
            "exchange": "NYSE", "isdelisted": "N",
            "firstpricedate": HISTORY_START, "lastpricedate": None,
            "firstquarter": date(2009, 12, 31), "lastquarter": None,
            "cusip": "BBB00001",
        },
    ]


def _sp500_rows() -> list[dict[str, object]]:
    # A single historical snapshot listing both names; members_at(any date >=
    # SNAPSHOT_DATE) returns {AAA, BBB}. All rebalances are in 2011.
    return [
        {"ticker": "AAA", "date": SNAPSHOT_DATE, "action": "historical"},
        {"ticker": "BBB", "date": SNAPSHOT_DATE, "action": "historical"},
    ]


def _to_ns(df: pl.DataFrame, cols: tuple[str, ...]) -> pl.DataFrame:
    """Cast the named date columns to Datetime[ns], matching the real bundle."""
    return df.with_columns([pl.col(c).cast(pl.Datetime("ns")) for c in cols])


def write_momentum_bundle(tmp_path: Path) -> Path:
    """Write the synthetic momentum bundle under tmp_path; return the root."""
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / BUNDLE_NAME
    bundle_dir.mkdir(parents=True)

    frames = {
        "sep": _to_ns(pl.DataFrame(_sep_rows()), ("date",)),
        "actions": _to_ns(
            pl.DataFrame(
                [],
                schema={
                    "ticker": pl.String, "date": pl.Date,
                    "action": pl.String, "value": pl.Float64,
                },
            ),
            ("date",),
        ),
        "tickers": _to_ns(
            pl.DataFrame(_tickers_rows()),
            ("firstpricedate", "lastpricedate", "firstquarter", "lastquarter"),
        ),
        "sp500": _to_ns(pl.DataFrame(_sp500_rows()), ("date",)),
    }

    file_lines: list[str] = []
    for name, df in frames.items():
        path = bundle_dir / f"{name}.parquet"
        df.write_parquet(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        file_lines.append(
            f'"{name}.parquet" = {{ sha256 = "{sha}", '
            f"size_bytes = {path.stat().st_size}, row_count = {df.height} }}"
        )
    pull_date = date.today() - timedelta(days=2)  # recent: no STALE warning
    manifest = (
        f"[snapshots.{BUNDLE_NAME}]\n"
        f'source = "sharadar"\n'
        f"pull_date = {pull_date.isoformat()}\n\n"
        f"[snapshots.{BUNDLE_NAME}.files]\n" + "\n".join(file_lines) + "\n"
    )
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    return snapshots_root


def momentum_rebalance_dates() -> tuple[date, ...]:
    """The monthly last trading days of 2011 (the 12 rebalance observations).

    The TestClock caches a 14-day pad beyond [start, end], so trading_days()
    bleeds into Dec-2010 and Jan-2012; filter to 2011 before taking the
    monthly-last days. monthly_last_trading_day returns a frozenset, so sort
    the result into the ascending tuple the splitter requires.
    """
    clock = TestClock(start_dt=date(2011, 1, 1), end_dt=date(2011, 12, 31))
    days_2011 = tuple(
        d
        for d in clock.trading_days()
        if date(2011, 1, 1) <= d <= date(2011, 12, 31)
    )
    return tuple(sorted(monthly_last_trading_day(days_2011)))


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


@attrs.frozen(slots=True)
class MomentumWindowFactory:
    """Builds a momentum BarLoop scoped to a contiguous [start, end] window.

    rebalance_dates is the FULL set of rebalance observations over the whole
    timeline; __call__ filters it to the window so the per-group BarLoop only
    rebalances on the observations inside its own group. An empty
    rebalance_dates yields a no-trade (flat, all-cash) BarLoop, used by the
    flat-path skip test. When gate=True the window's rebalances are passed as
    the BarLoop signal_calendar (the M5 PR 3a perf gate), so signal.compute
    fires only on rebalance bars; this is behavior-preserving (the policy
    no-ops off-calendar) and is exercised by the gate's byte-identical test.
    """

    snapshots_root: str
    rebalance_dates: tuple[date, ...]
    initial_capital: float = INITIAL_CAPITAL
    gate: bool = False

    def __call__(self, group_start: date, group_end: date) -> BarLoop:
        source = SharadarDataSource(BUNDLE_NAME, Path(self.snapshots_root))
        universe = SharadarSP500Universe(source)
        clock = TestClock(start_dt=group_start, end_dt=group_end)
        window_rebals = frozenset(
            d for d in self.rebalance_dates if group_start <= d <= group_end
        )

        price_index: dict[tuple[AssetId, date], float] = {}
        for asset_id, ticker in _ASSET_TO_TICKER.items():
            frame = source.read_sep_prices(
                ticker=ticker, start_dt=group_start, end_dt=group_end
            )
            for row in frame.iter_rows(named=True):
                price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

        def price_lookup(asset_id: AssetId, dt: datetime) -> float | None:
            d = dt.date() if isinstance(dt, datetime) else dt
            return price_index.get((asset_id, d))

        policy = TopQuintileLongPolicy(
            rebalance_dates=window_rebals, price_lookup=price_lookup
        )
        return BarLoop(
            data_source=source,
            universe=universe,
            signal=Momentum12_1Signal(),
            policy=policy,
            matching_engine=CloseFillMatchingEngine(clock=clock),
            clock=clock,
            tickers=(_AAA, _BBB),
            initial_capital=self.initial_capital,
            use_real_pit_view=True,
            asset_id_to_ticker=_resolve,
            signal_calendar=(window_rebals if self.gate else None),
        )
