"""Reference constant-weight P&L computation.

Per the M1-day-3 skeptical reviewer: the "spreadsheet hand calculation"
from ADR 0002 acceptance criterion 2 is operationalized as a pure Python
scalar loop in this module. It performs EXACTLY the same float operations
in EXACTLY the same order as the M1 BarLoop. The integration test runs
both pipelines on the identical Polars input frames and asserts equality
to 1e-10.

The reference function is NOT used by production code; it is imported
only by tests. Keeping it as a separate pure function makes it the
"second implementation" check (per ADR 0001 reviewer recommendation:
differential testing against an independent implementation catches the
bugs that any single implementation has).

Float-determinism rules followed here (and matched by BarLoop):
- All sums iterate dict keys in sorted() order.
- Per-bar inputs are eager Polars DataFrames already collected and sorted
  by dt; we never reach below them to LazyFrames whose plan optimization
  could re-order reductions.
- No set iteration anywhere (frozenset for membership tests only).
- Float64 throughout; never Decimal in the arithmetic.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

import attrs
import polars as pl

from pit_backtest.data.records import AssetId


@attrs.frozen(slots=True)
class ReferenceEquityRow:
    """One row of the reference equity curve."""

    dt: date
    cash: float
    nav: float
    positions: dict[AssetId, float]


def _build_price_index(
    prices_by_asset: Mapping[AssetId, pl.DataFrame],
) -> dict[tuple[AssetId, date], float]:
    """Flatten per-asset price frames into a (asset_id, dt) -> closeunadj dict.

    Both engine and reference build this same structure from the same
    input frames (already collected and sorted by dt by SharadarDataSource).
    """
    price_at: dict[tuple[AssetId, date], float] = {}
    for asset_id in sorted(prices_by_asset.keys()):
        frame = prices_by_asset[asset_id]
        for row in frame.iter_rows(named=True):
            price_at[(asset_id, row["dt"])] = float(row["closeunadj"])
    return price_at


def _build_dividend_index(
    dividends_by_asset: Mapping[AssetId, pl.DataFrame],
) -> dict[date, dict[AssetId, float]]:
    """Flatten per-asset dividend frames into a dt -> {asset_id: amount} dict.

    Per-bar lookup is O(1); per-bar iteration over the inner dict uses
    sorted(asset_id) for determinism.
    """
    divs_at: dict[date, dict[AssetId, float]] = {}
    for asset_id in sorted(dividends_by_asset.keys()):
        frame = dividends_by_asset[asset_id]
        for row in frame.iter_rows(named=True):
            d = row["ex_date"]
            if d not in divs_at:
                divs_at[d] = {}
            divs_at[d][asset_id] = float(row["amount_per_share"])
    return divs_at


def reference_constant_weight_pnl(
    prices_by_asset: Mapping[AssetId, pl.DataFrame],
    dividends_by_asset: Mapping[AssetId, pl.DataFrame],
    rebalance_dates: frozenset[date],
    trading_days: Iterable[date],
    tickers: tuple[AssetId, ...],
    initial_capital: float,
) -> list[ReferenceEquityRow]:
    """Compute the reference equity curve for the constant-weight strategy.

    Per ADR 0004: start_dt is NOT forced as a rebalance date. The
    portfolio holds cash until the first scheduled rebalance.

    The arithmetic mirrors BarLoop step-for-step. Any divergence between
    this function and the BarLoop output (other than the absolute 1-ULP
    float noise) indicates a bug in one of the two implementations.
    """
    sorted_tickers = tuple(sorted(tickers))
    price_at = _build_price_index(prices_by_asset)
    divs_at = _build_dividend_index(dividends_by_asset)

    cash = float(initial_capital)
    positions: dict[AssetId, float] = {ticker: 0.0 for ticker in sorted_tickers}
    rows: list[ReferenceEquityRow] = []

    for bar_dt in trading_days:
        # Step 1: credit dividends from end-of-prior-bar shares.
        bar_divs = divs_at.get(bar_dt, {})
        for ticker in sorted(bar_divs.keys()):
            div = bar_divs[ticker]
            shares = positions.get(ticker, 0.0)
            if shares != 0.0:
                cash += shares * div

        # Step 2: today's prices for the strategy's tickers.
        prices_today: dict[AssetId, float] = {}
        for ticker in sorted_tickers:
            key = (ticker, bar_dt)
            if key in price_at:
                prices_today[ticker] = price_at[key]

        # Step 3: rebalance if today is a scheduled rebalance date.
        if bar_dt in rebalance_dates and prices_today:
            # NAV pre-trade
            nav = cash
            for ticker in sorted(positions.keys()):
                shares = positions[ticker]
                if shares != 0.0 and ticker in prices_today:
                    nav += shares * prices_today[ticker]

            # Target shares: equal weight over live tickers
            live = sorted(prices_today.keys())
            target_dollars = nav / len(live)
            for ticker in live:
                close_t = prices_today[ticker]
                target_shares = target_dollars / close_t
                current_shares = positions[ticker]
                qty = target_shares - current_shares
                cash -= qty * close_t
                positions[ticker] = target_shares

        # Step 4: mark to market at today's close
        nav_close = cash
        for ticker in sorted(positions.keys()):
            shares = positions[ticker]
            if shares != 0.0 and ticker in prices_today:
                nav_close += shares * prices_today[ticker]

        rows.append(
            ReferenceEquityRow(
                dt=bar_dt,
                cash=cash,
                nav=nav_close,
                positions=dict(positions),
            )
        )

    return rows


def reference_to_polars(rows: list[ReferenceEquityRow]) -> pl.DataFrame:
    """Convert reference rows to a Polars DataFrame for diffing with the
    BarLoop's equity_curve output.
    """
    if not rows:
        return pl.DataFrame({"dt": [], "cash": [], "nav": []})
    all_tickers = sorted(rows[0].positions.keys())
    data: dict[str, list[object]] = {
        "dt": [r.dt for r in rows],
        "cash": [r.cash for r in rows],
        "nav": [r.nav for r in rows],
    }
    for ticker in all_tickers:
        data[f"shares_{ticker}"] = [r.positions.get(ticker, 0.0) for r in rows]
    return pl.DataFrame(data)
