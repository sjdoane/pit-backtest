"""JT1993 12-month total-return momentum, excluding the most recent month.

Used by the M5 worked study per ADR 0002 decision 20.

PIT + total-return discipline (per docs/methodology/total_return_reconstruction.md
line 175): the score is built from Sharadar's RAW `closeunadj` plus the
explicit ACTIONS dividend + split streams, NEVER the back-adjusted `close`.
Sharadar's `close` is back-adjusted with a cumulative factor that bakes in
EVERY future split and dividend up to the data-pull date; reading it at a
past bar is a lookahead leak (it imports corporate actions that postdate the
rebalance). The forward reconstruction here uses only actions whose ex-date
falls inside the lookback window (all strictly before the rebalance dt), so
it is point-in-time safe.

Split handling: `closeunadj` is the as-traded price, so it drops by the
split ratio on a split ex-date (e.g. a 2:1 split halves it). Left raw, the
total-return multiplier would read a spurious ~50% loss on the split day.
The signal divides both the price and the dividend amount by the cumulative
in-window split factor (the product of ratios for splits with ex-date AFTER
the bar) before reconstructing the total return, which cancels the drop.
This matters: AAPL, TSLA, NVDA, and GOOGL all split inside the 2005-2024
study window.

PitView table contract: this signal requires `pit_view` to serve three
tables, each a LazyFrame already sliced to `available_dt < dt` by the
engine: `sep` (dt, closeunadj, ticker), `actions` (ticker, date, action,
value), and `tickers` (permaticker, ticker, firstpricedate, lastpricedate).
The Signal protocol passes AssetIds (permaticker ints) via the universe but
SEP is ticker-keyed, so the signal resolves AssetId to ticker from the
`tickers` view using the same asof interval logic as the resolver
(firstpricedate <= dt <= lastpricedate, lastpricedate null meaning active).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import polars as pl
from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

from pit_backtest.data.adjustments import reconstruct_total_return
from pit_backtest.data.records import AssetId
from pit_backtest.data.universe import Universe
from pit_backtest.signal.base import PitView, Signal


class Momentum12_1Signal(Signal):
    """Jegadeesh-Titman 1993 12-1 momentum.

    Score = 12-month total return through one month before dt (the most
    recent month is skipped to drop the JT1993 short-term reversal). The
    total return is reconstructed forward from `closeunadj` + PIT ACTIONS
    dividends + PIT ACTIONS splits, so splits and dividends are correctly
    incorporated without the lookahead leak of the back-adjusted close.

    Per the Signal protocol, assets with insufficient history (no bar at or
    before either window edge) are OMITTED from the result, never emitted
    with a NaN score. The result dict is sorted by AssetId for determinism.
    """

    def required_lookback_days(self) -> int:
        # The far window edge is ~12 calendar months before dt. 273 trading
        # days (~12 months + a ~1-month buffer) is a generous floor; the
        # window itself is calendar-resolved (relativedelta), not counted in
        # trading-day indices, so this is a lookback guarantee, not the
        # window definition.
        return 273

    def compute(
        self, universe: Universe, dt: datetime, pit_view: PitView
    ) -> dict[AssetId, float]:
        members = universe.members_at(dt)
        if not members:
            return {}
        member_set = set(members)
        as_of: date = dt.date() if isinstance(dt, datetime) else dt
        t_start_target: date = as_of - relativedelta(months=12)
        t_skip_target: date = as_of - relativedelta(months=1)

        asset_to_ticker = _resolve_member_tickers(
            pit_view("tickers").collect(), member_set, as_of
        )
        sep = pit_view("sep").collect()
        actions = pit_view("actions").collect()

        scores: dict[AssetId, float] = {}
        for asset_id in sorted(member_set):
            ticker = asset_to_ticker.get(asset_id)
            if ticker is None:
                continue
            score = _momentum_score_for_ticker(
                sep, actions, ticker, t_start_target, t_skip_target
            )
            if score is not None:
                scores[asset_id] = score
        return dict(sorted(scores.items()))


def _resolve_member_tickers(
    tickers_df: pl.DataFrame, members: set[AssetId], as_of: date
) -> dict[AssetId, str]:
    """Asof AssetId -> ticker map for members active at as_of.

    Mirrors the resolver interval logic: firstpricedate <= as_of <=
    lastpricedate, with a null lastpricedate meaning still-active.
    """
    resolved: dict[AssetId, str] = {}
    for row in tickers_df.iter_rows(named=True):
        asset_id = AssetId(int(row["permaticker"]))
        if asset_id not in members:
            continue
        first = row["firstpricedate"]
        last = row["lastpricedate"]
        if first is not None and as_of < first:
            continue
        if last is not None and as_of > last:
            continue
        resolved[asset_id] = str(row["ticker"])
    return resolved


def _momentum_score_for_ticker(
    sep: pl.DataFrame,
    actions: pl.DataFrame,
    ticker: str,
    t_start_target: date,
    t_skip_target: date,
) -> float | None:
    """12-1 total-return score for one ticker, or None if omitted.

    Resolves the window edges to the last available bar on or before each
    target date, split-adjusts the price + dividends over the window, and
    reconstructs the total return. Returns None (OMIT, never NaN) when
    either window edge has no bar or the reconstructed series is degenerate.
    """
    px = (
        sep.filter(pl.col("ticker") == ticker)
        .select(["dt", "closeunadj"])
        .sort("dt")
    )
    if px.height < 2:
        return None

    t_start = _last_bar_on_or_before(px, t_start_target)
    t_skip = _last_bar_on_or_before(px, t_skip_target)
    if t_start is None or t_skip is None or t_start >= t_skip:
        return None

    window = px.filter(
        (pl.col("dt") >= t_start) & (pl.col("dt") <= t_skip)
    ).sort("dt")
    if window.height < 2:
        return None

    ticker_actions = actions.filter(
        (pl.col("ticker") == ticker)
        & (pl.col("date") > t_start)
        & (pl.col("date") <= t_skip)
    )
    splits = (
        ticker_actions.filter(pl.col("action") == "split")
        .select(["date", "value"])
        .sort("date")
    )

    # Cumulative in-window split factor: at bar dt the factor is the product
    # of ratios for splits whose ex-date is strictly AFTER dt (those splits
    # have not yet hit the price at dt, so the pre-split price must be scaled
    # down to the latest share basis). On/after an ex-date the price already
    # reflects that split, so it is excluded.
    split_factor: pl.Expr = pl.lit(1.0)
    for srow in splits.iter_rows(named=True):
        ratio = float(srow["value"])
        split_date = srow["date"]
        split_factor = split_factor * (
            pl.when(pl.col("dt") < split_date).then(ratio).otherwise(1.0)
        )

    window_adj = window.with_columns(split_factor.alias("sf")).with_columns(
        (pl.col("closeunadj") / pl.col("sf")).alias("close")
    )
    adj_prices = window_adj.select(["dt", "close"])
    if adj_prices.filter(pl.col("close") <= 0.0).height > 0:
        return None

    # Adjust each dividend by the split factor at its ex-date (the same basis
    # as the adjusted price), then shape to the reconstruct contract. The
    # split factor is read via a BACKWARD join_asof (nearest prior bar's sf)
    # rather than an exact date match: sf is a step that only changes on a
    # split ex-date (itself a trading day), so a dividend ex-date that is not
    # a SEP bar still inherits the correct factor from the most recent prior
    # bar, and a dividend sitting after an in-window split is not mis-scaled
    # (post-impl reviewer High 1). fill_null(1.0) floors the degenerate case
    # of a dividend preceding the first window bar.
    adj_dividends = (
        ticker_actions.filter(pl.col("action") == "dividend")
        .select(["date", "value"])
        .sort("date")
        .join_asof(
            window_adj.select(["dt", "sf"]).sort("dt"),
            left_on="date",
            right_on="dt",
            strategy="backward",
        )
        .with_columns(pl.col("sf").fill_null(1.0))
        .select(
            pl.col("date").alias("ex_date"),
            (pl.col("value") / pl.col("sf"))
            .cast(pl.Float64)
            .alias("amount_per_share"),
        )
    )

    tr = reconstruct_total_return(
        prices=adj_prices,
        dividends=adj_dividends,
        start_dt=t_start,
        end_dt=t_skip,
        expense_ratio_annual=Decimal("0"),
    )
    tr_first = float(tr["tr"][0])
    tr_last = float(tr["tr"][-1])
    if tr_first <= 0.0:
        return None
    return tr_last / tr_first - 1.0


def _last_bar_on_or_before(px: pl.DataFrame, target: date) -> date | None:
    """The latest `dt` in px that is <= target, or None if none exists."""
    eligible = px.filter(pl.col("dt") <= target)
    if eligible.height == 0:
        return None
    last = eligible["dt"][-1]  # px is sorted ascending
    assert isinstance(last, date)
    return last
