"""Real PitView BarLoop wiring tests (M5 PR 2b, ADR 0016).

`BarLoop._build_pit_view(bar_dt)` serves the three tables the momentum
signal consumes (sep / actions / tickers), each sliced to available_dt <
bar_dt (strict), and `use_real_pit_view=True` rebuilds it per bar. The
synthetic bundle here stores its date columns as `Datetime(time_unit='ns')`
to MATCH the real Sharadar bundle on disk (the unit dtype is the whole
point: a `pl.Date`-typed fixture would mask the Critical-1 TypeError the
post-impl Plan-reviewer reproduced, where the momentum resolver compares a
`date` to a `datetime`). It also exercises the injected `asset_id_to_ticker`
resolver that replaces the M1 three-name hardcoded map so a non-SPY/AGG/GLD
universe can run.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe
from pit_backtest.engine.bar_loop import BarLoop
from pit_backtest.engine.calendar import monthly_last_trading_day
from pit_backtest.execution.clock import TestClock
from pit_backtest.execution.matching import CloseFillMatchingEngine
from pit_backtest.signal.momentum import Momentum12_1Signal
from pit_backtest.policy.top_quintile import TopQuintileLongPolicy

_AAA = AssetId(1001)  # the momentum winner (price rises over the lookback)
_BBB = AssetId(1002)  # the loser (flat)
_ASSET_TO_TICKER = {_AAA: "AAA", _BBB: "BBB"}
_REBALANCE = date(2011, 1, 31)


def _resolve(asset_id: AssetId) -> str:
    return _ASSET_TO_TICKER[asset_id]


def _sep_rows() -> list[dict[str, object]]:
    """Monthly history bars (2009-12 .. 2010-12) plus daily Jan-2011 bars.

    Monthly history is enough for the 12-1 momentum lookback (reconstruct
    has no NYSE-calendar coupling, just consecutive-row ratios); the daily
    Jan-2011 bars feed the BarLoop run window. AAA rises 100 -> 200 over the
    history (a clear winner); BBB stays flat at 100.
    """
    rows: list[dict[str, object]] = []
    history = [date(2009, 12, 28)] + [date(2010, m, 28) for m in range(1, 13)]
    n = len(history)
    for i, d in enumerate(history):
        aaa = 100.0 + 100.0 * (i / (n - 1))  # 100 -> 200
        rows.append(_bar("AAA", d, aaa))
        rows.append(_bar("BBB", d, 100.0))
    jan = [date(2011, 1, day) for day in range(3, 32) if date(2011, 1, day).weekday() < 5]
    for d in jan:
        rows.append(_bar("AAA", d, 200.0))
        rows.append(_bar("BBB", d, 100.0))
    return rows


def _bar(ticker: str, d: date, px: float) -> dict[str, object]:
    return {
        "ticker": ticker, "date": d,
        "open": px, "high": px, "low": px, "close": px, "closeunadj": px,
        "volume": 1_000_000,
    }


def _tickers_rows() -> list[dict[str, object]]:
    return [
        {
            "permaticker": int(_AAA), "ticker": "AAA", "name": "Alpha Co",
            "exchange": "NYSE", "isdelisted": "N",
            "firstpricedate": date(2009, 12, 28), "lastpricedate": None,
            "firstquarter": date(2009, 12, 31), "lastquarter": None,
            "cusip": "AAA00001",
        },
        {
            "permaticker": int(_BBB), "ticker": "BBB", "name": "Beta Co",
            "exchange": "NYSE", "isdelisted": "N",
            "firstpricedate": date(2009, 12, 28), "lastpricedate": None,
            "firstquarter": date(2009, 12, 31), "lastquarter": None,
            "cusip": "BBB00001",
        },
    ]


def _sp500_rows() -> list[dict[str, object]]:
    # A 2010-12-31 historical snapshot listing both names; members_at(any
    # date >= 2010-12-31) returns {AAA, BBB}. No added/removed events.
    return [
        {"ticker": "AAA", "date": date(2010, 12, 31), "action": "historical"},
        {"ticker": "BBB", "date": date(2010, 12, 31), "action": "historical"},
    ]


def _to_ns(df: pl.DataFrame, cols: tuple[str, ...]) -> pl.DataFrame:
    """Cast the named date columns to Datetime[ns], matching the real bundle."""
    return df.with_columns([pl.col(c).cast(pl.Datetime("ns")) for c in cols])


def _write_momentum_bundle(tmp_path: Path) -> Path:
    bundle_name = "sharadar_momentum_test"
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
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
        f"[snapshots.{bundle_name}]\n"
        f'source = "sharadar"\n'
        f"pull_date = {pull_date.isoformat()}\n\n"
        f"[snapshots.{bundle_name}.files]\n" + "\n".join(file_lines) + "\n"
    )
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    return snapshots_root


def _make_bar_loop(
    snapshots_root: Path, *, use_real_pit_view: bool = True
) -> BarLoop:
    source = SharadarDataSource("sharadar_momentum_test", snapshots_root)
    universe = SharadarSP500Universe(source)
    clock = TestClock(start_dt=date(2011, 1, 3), end_dt=_REBALANCE)

    price_index: dict[tuple[AssetId, date], float] = {}
    for asset_id, ticker in _ASSET_TO_TICKER.items():
        frame = source.read_sep_prices(
            ticker=ticker, start_dt=date(2009, 12, 28), end_dt=_REBALANCE
        )
        for row in frame.iter_rows(named=True):
            price_index[(asset_id, row["dt"])] = float(row["closeunadj"])

    def price_lookup(asset_id: AssetId, dt: datetime) -> float | None:
        d = dt.date() if isinstance(dt, datetime) else dt
        return price_index.get((asset_id, d))

    policy = TopQuintileLongPolicy(
        rebalance_dates=frozenset({_REBALANCE}), price_lookup=price_lookup
    )
    return BarLoop(
        data_source=source,
        universe=universe,
        signal=Momentum12_1Signal(),
        policy=policy,
        matching_engine=CloseFillMatchingEngine(clock=clock),
        clock=clock,
        tickers=(_AAA, _BBB),
        initial_capital=100_000.0,
        use_real_pit_view=use_real_pit_view,
        asset_id_to_ticker=_resolve,
    )


# ----- pit_view slicing + dtype -----


def test_pit_view_sep_sliced_strictly_before_bar_dt(tmp_path: Path) -> None:
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    pv = bar_loop._build_pit_view(date(2010, 6, 28))
    sep = pv("sep").collect()
    assert set(sep.columns) == {"dt", "closeunadj", "ticker"}
    assert sep.schema["dt"] == pl.Date
    # Strict slice: a bar dated == bar_dt is absent; one before is present.
    dts = set(sep.get_column("dt").to_list())
    assert date(2010, 6, 28) not in dts
    assert date(2010, 5, 28) in dts


def test_pit_view_actions_serves_raw_vendor_columns(tmp_path: Path) -> None:
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    actions = bar_loop._build_pit_view(_REBALANCE)("actions").collect()
    # Raw vendor columns, NOT the read_actions_dividends ex_date/amount view.
    assert set(actions.columns) == {"ticker", "date", "action", "value"}
    assert actions.schema["date"] == pl.Date


def test_pit_view_tickers_is_full_table_with_date_columns_cast(tmp_path: Path) -> None:
    """Critical 1: the tickers view must cast firstpricedate/lastpricedate to
    pl.Date (the real bundle stores Datetime[ns]); otherwise the momentum
    resolver's date-vs-datetime comparison raises TypeError."""
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    tickers = bar_loop._build_pit_view(_REBALANCE)("tickers").collect()
    assert set(tickers.columns) == {
        "permaticker", "ticker", "firstpricedate", "lastpricedate"
    }
    assert tickers.schema["firstpricedate"] == pl.Date
    assert tickers.schema["lastpricedate"] == pl.Date
    # Full table, not sliced: both names present.
    assert set(tickers.get_column("ticker").to_list()) == {"AAA", "BBB"}


def test_pit_view_unknown_table_raises_keyerror(tmp_path: Path) -> None:
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    with pytest.raises(KeyError, match="sep"):
        bar_loop._build_pit_view(_REBALANCE)("sf1")


def test_pit_view_rebuilt_per_bar_sees_growing_history(tmp_path: Path) -> None:
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    early = bar_loop._build_pit_view(date(2010, 3, 28))("sep").collect().height
    late = bar_loop._build_pit_view(date(2010, 12, 28))("sep").collect().height
    assert late > early  # the per-bar closure sees more history at a later dt


# ----- Critical 1 end-to-end + full run -----


def test_real_pit_view_feeds_momentum_signal_without_typeerror(tmp_path: Path) -> None:
    """The reproduced Critical 1: with Datetime[ns] date columns the momentum
    resolver iterates the tickers view and compares dates; the pl.Date cast in
    _build_pit_view makes that succeed and the winner outscores the loser."""
    snapshots_root = _write_momentum_bundle(tmp_path)
    source = SharadarDataSource("sharadar_momentum_test", snapshots_root)
    universe = SharadarSP500Universe(source)
    bar_loop = _make_bar_loop(snapshots_root)

    pv = bar_loop._build_pit_view(_REBALANCE)
    scores = Momentum12_1Signal().compute(
        universe, datetime.combine(_REBALANCE, time(16, 0)), pv
    )
    assert _AAA in scores and _BBB in scores
    assert scores[_AAA] > scores[_BBB]  # AAA rose 100->200, BBB flat
    assert scores[_AAA] > 0.0


def test_full_bar_loop_run_with_real_pit_view_holds_the_winner(tmp_path: Path) -> None:
    """End-to-end: BarLoop.run with use_real_pit_view=True rebalances on the
    top quintile (cut=1 of 2 names -> the winner AAA) and produces a
    non-empty equity curve."""
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    result = bar_loop.run(start_dt=date(2011, 1, 3), end_dt=_REBALANCE)
    assert result.n_rebalances == 1
    assert result.equity_curve.height > 0
    # The rebalance longs AAA (the momentum winner); it holds AAA shares,
    # not BBB, after the 2011-01-31 rebalance.
    assert bar_loop.state.positions[_AAA] > 0.0
    assert bar_loop.state.positions[_BBB] == 0.0


# ----- back-compat: noop default + injected resolver -----


def test_noop_pit_view_is_the_default(tmp_path: Path) -> None:
    """use_real_pit_view defaults False so the constant-weight demos and the
    700+ existing tests keep the no-op stand-in."""
    bar_loop = _make_bar_loop(
        _write_momentum_bundle(tmp_path), use_real_pit_view=False
    )
    with pytest.raises(NotImplementedError):
        bar_loop._pit_view("sep")


def test_injected_asset_id_to_ticker_resolves_non_m1_universe(tmp_path: Path) -> None:
    """The injected resolver lets the BarLoop run a universe the M1 three-name
    map (SPY/AGG/GLD) would KeyError on. The run completes for AAA/BBB."""
    bar_loop = _make_bar_loop(_write_momentum_bundle(tmp_path))
    result = bar_loop.run(start_dt=date(2011, 1, 3), end_dt=_REBALANCE)
    assert set(result.tickers) == {"AAA", "BBB"}
