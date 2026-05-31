"""Tests for Momentum12_1Signal (M5 PR 1).

The signal computes JT1993 12-1 total-return momentum from raw closeunadj +
PIT ACTIONS dividends + PIT ACTIONS splits. With no dividends/splits the
total-return ratio telescopes to closeunadj[t_skip] / closeunadj[t_start],
so scores are hand-pinnable.

Window for dt = 2024-01-15: t_start_target = 2023-01-15, t_skip_target =
2023-12-15. With dense daily bars, t_start = 2023-01-15 and t_skip =
2023-12-15. Bars after t_skip are EXCLUDED (the "skip the most recent
month" mechanic); bars at/after dt are never seen (the engine slices the
pit_view to available_dt < dt).
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.signal.base import PitView
from pit_backtest.signal.momentum import Momentum12_1Signal

_DT = datetime(2024, 1, 15)
_T_START = date(2023, 1, 15)
_T_SKIP = date(2023, 12, 15)
_SPIKE = 999.0


class _FakeUniverse:
    """Minimal Universe stub: members_at returns a fixed AssetId list."""

    def __init__(self, members: list[AssetId]) -> None:
        self._members = members

    def members_at(self, dt: datetime) -> list[AssetId]:
        return list(self._members)


def _daily_dates(start: date, end: date) -> list[date]:
    return pl.date_range(start, end, "1d", eager=True).to_list()


def _sep_rows(
    ticker: str,
    target_at_skip: float,
    *,
    start: date = date(2023, 1, 1),
    end: date = date(2024, 1, 14),
    pre_window: float = 100.0,
) -> list[dict[str, object]]:
    """One ticker's dense daily closeunadj: flat `pre_window` through the
    window, `target_at_skip` on t_skip, and a large spike after t_skip (which
    must be excluded from the score).
    """
    rows: list[dict[str, object]] = []
    for d in _daily_dates(start, end):
        if d == _T_SKIP:
            close = target_at_skip
        elif d > _T_SKIP:
            close = _SPIKE
        else:
            close = pre_window
        rows.append({"ticker": ticker, "dt": d, "closeunadj": close})
    return rows


def _make_pit_view(
    sep_rows: list[dict[str, object]],
    action_rows: list[dict[str, object]],
    ticker_rows: list[dict[str, object]],
) -> PitView:
    sep = pl.DataFrame(
        sep_rows,
        schema={"ticker": pl.Utf8, "dt": pl.Date, "closeunadj": pl.Float64},
    )
    actions = pl.DataFrame(
        action_rows,
        schema={
            "ticker": pl.Utf8,
            "date": pl.Date,
            "action": pl.Utf8,
            "value": pl.Float64,
        },
    )
    tickers = pl.DataFrame(
        ticker_rows,
        schema={
            "permaticker": pl.Int64,
            "ticker": pl.Utf8,
            "firstpricedate": pl.Date,
            "lastpricedate": pl.Date,
        },
    )
    frames = {"sep": sep, "actions": actions, "tickers": tickers}

    def pit_view(table_name: str) -> pl.LazyFrame:
        return frames[table_name].lazy()

    return pit_view


def _ticker_row(
    permaticker: int,
    ticker: str,
    *,
    first: date = date(2022, 1, 1),
    last: date | None = None,
) -> dict[str, object]:
    return {
        "permaticker": permaticker,
        "ticker": ticker,
        "firstpricedate": first,
        "lastpricedate": last,
    }


# ----- happy path -----


def test_momentum_scores_pin_exact_price_ratios() -> None:
    """3 assets, no dividends/splits: score = closeunadj[t_skip]/100 - 1."""
    sep = (
        _sep_rows("AAA", 110.0)  # +0.10
        + _sep_rows("BBB", 120.0)  # +0.20
        + _sep_rows("CCC", 90.0)  # -0.10
    )
    tickers = [
        _ticker_row(1, "AAA"),
        _ticker_row(2, "BBB"),
        _ticker_row(3, "CCC"),
    ]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(1), AssetId(2), AssetId(3)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-12)
    assert scores[AssetId(2)] == pytest.approx(0.20, abs=1e-12)
    assert scores[AssetId(3)] == pytest.approx(-0.10, abs=1e-12)


def test_momentum_result_is_sorted_by_asset_id() -> None:
    sep = _sep_rows("AAA", 110.0) + _sep_rows("BBB", 120.0)
    tickers = [_ticker_row(2, "BBB"), _ticker_row(1, "AAA")]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(2), AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert list(scores.keys()) == [AssetId(1), AssetId(2)]


# ----- the skip-most-recent-month mechanic -----


def test_spike_after_t_skip_does_not_change_score() -> None:
    """A 999 spike in (t_skip, dt) is excluded; the score is the pre-skip
    ratio. This is the load-bearing test that the 1-month skip skips.
    """
    sep = _sep_rows("AAA", 110.0)  # spike of 999 lives after t_skip already
    tickers = [_ticker_row(1, "AAA")]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-12)  # NOT 999-driven


# ----- dividend correctness -----


def test_dividend_in_window_raises_total_return_above_price_ratio() -> None:
    """A 5.0 dividend on a flat-100 interior day adds a 1.05 multiplier; with
    the t_skip jump to 110 the TR is 1.05 * 1.10 = 1.155 -> score 0.155,
    above the bare price ratio of 0.10.
    """
    sep = _sep_rows("AAA", 110.0)
    actions = [
        {
            "ticker": "AAA",
            "date": date(2023, 6, 1),
            "action": "dividend",
            "value": 5.0,
        }
    ]
    tickers = [_ticker_row(1, "AAA")]
    pit_view = _make_pit_view(sep, actions, tickers)
    universe = _FakeUniverse([AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores[AssetId(1)] == pytest.approx(0.155, abs=1e-9)


# ----- split correctness (the Plan-reviewer missed this; closeunadj drops) -----


def test_two_for_one_split_in_window_does_not_corrupt_score() -> None:
    """A 2:1 split halves closeunadj on its ex-date. Raw, the TR would read a
    ~-45% loss. With in-window split adjustment the score is the true +0.10
    (100 -> effective 110 in pre-split terms; 50 -> 55 post-split).
    """
    split_date = date(2023, 6, 1)
    # closeunadj: 100 before split, 50 from split to before t_skip, 55 at
    # t_skip (= 110 in pre-split terms), 999 spike after.
    rows: list[dict[str, object]] = []
    for d in _daily_dates(date(2023, 1, 1), date(2024, 1, 14)):
        if d == _T_SKIP:
            close = 55.0
        elif d > _T_SKIP:
            close = _SPIKE
        elif d >= split_date:
            close = 50.0
        else:
            close = 100.0
        rows.append({"ticker": "AAA", "dt": d, "closeunadj": close})
    actions = [
        {
            "ticker": "AAA",
            "date": split_date,
            "action": "split",
            "value": 2.0,
        }
    ]
    tickers = [_ticker_row(1, "AAA")]
    pit_view = _make_pit_view(rows, actions, tickers)
    universe = _FakeUniverse([AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-9)


def test_two_splits_in_window_compound() -> None:
    """A 2:1 then a 3:1 split compound to a factor of 6 before both. As-traded
    600 -> 300 -> 100 -> 110; split-adjusted 100 -> 100 -> 100 -> 110, so the
    true score is +0.10.
    """
    s1 = date(2023, 4, 1)
    s2 = date(2023, 8, 1)
    rows: list[dict[str, object]] = []
    for d in _daily_dates(date(2023, 1, 1), date(2024, 1, 14)):
        if d == _T_SKIP:
            close = 110.0
        elif d > _T_SKIP:
            close = _SPIKE
        elif d >= s2:
            close = 100.0
        elif d >= s1:
            close = 300.0
        else:
            close = 600.0
        rows.append({"ticker": "AAA", "dt": d, "closeunadj": close})
    actions = [
        {"ticker": "AAA", "date": s1, "action": "split", "value": 2.0},
        {"ticker": "AAA", "date": s2, "action": "split", "value": 3.0},
    ]
    pit_view = _make_pit_view(rows, actions, [_ticker_row(1, "AAA")])
    scores = Momentum12_1Signal().compute(
        _FakeUniverse([AssetId(1)]), _DT, pit_view
    )
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-9)


def test_reverse_split_in_window() -> None:
    """A 1-for-2 reverse split (ratio 0.5) doubles the as-traded price. 50 ->
    100 -> 110; split-adjusted 100 -> 100 -> 110; true score +0.10.
    """
    s = date(2023, 6, 1)
    rows: list[dict[str, object]] = []
    for d in _daily_dates(date(2023, 1, 1), date(2024, 1, 14)):
        if d == _T_SKIP:
            close = 110.0
        elif d > _T_SKIP:
            close = _SPIKE
        elif d >= s:
            close = 100.0
        else:
            close = 50.0
        rows.append({"ticker": "AAA", "dt": d, "closeunadj": close})
    actions = [{"ticker": "AAA", "date": s, "action": "split", "value": 0.5}]
    pit_view = _make_pit_view(rows, actions, [_ticker_row(1, "AAA")])
    scores = Momentum12_1Signal().compute(
        _FakeUniverse([AssetId(1)]), _DT, pit_view
    )
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-9)


def test_dividend_then_split_co_adjust() -> None:
    """A $10 dividend (pre-split basis) at a bar before a 2:1 split. Economic:
    1 share @ 100, reinvest $10 -> 1.1 shares, 2:1 split -> 2.2 shares, sold
    @ 55 post-split = 121; TR = 1.21 -> score +0.21. The dividend must be
    scaled to the post-split basis (10/2 = 5) so it co-adjusts with the price.
    """
    div_date = date(2023, 6, 1)
    split_date = date(2023, 8, 1)
    rows: list[dict[str, object]] = []
    for d in _daily_dates(date(2023, 1, 1), date(2024, 1, 14)):
        if d == _T_SKIP:
            close = 55.0
        elif d > _T_SKIP:
            close = _SPIKE
        elif d >= split_date:
            close = 50.0
        else:
            close = 100.0
        rows.append({"ticker": "AAA", "dt": d, "closeunadj": close})
    actions = [
        {"ticker": "AAA", "date": div_date, "action": "dividend", "value": 10.0},
        {"ticker": "AAA", "date": split_date, "action": "split", "value": 2.0},
    ]
    pit_view = _make_pit_view(rows, actions, [_ticker_row(1, "AAA")])
    scores = Momentum12_1Signal().compute(
        _FakeUniverse([AssetId(1)]), _DT, pit_view
    )
    assert scores[AssetId(1)] == pytest.approx(0.21, abs=1e-9)


# ----- omission (never NaN) -----


def test_short_history_asset_is_omitted_not_nan() -> None:
    """An asset whose SEP history starts after t_start_target has no bar at
    the far window edge and is OMITTED from the result keys.
    """
    sep = _sep_rows("AAA", 110.0) + _sep_rows(
        "DDD", 110.0, start=date(2023, 11, 1)
    )
    tickers = [_ticker_row(1, "AAA"), _ticker_row(4, "DDD")]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(1), AssetId(4)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert AssetId(1) in scores
    assert AssetId(4) not in scores  # omitted, not present-with-NaN


# ----- AssetId -> ticker resolution (the crux) -----


def test_resolves_asset_id_to_ticker_via_tickers_view() -> None:
    """SEP is ticker-keyed; the signal must map each universe AssetId to its
    ticker through the tickers view, not assume AssetId == ticker.
    """
    sep = _sep_rows("ZZZ", 120.0) + _sep_rows("AAA", 110.0)
    # permaticker 7 -> ZZZ (score +0.20); permaticker 1 -> AAA (+0.10).
    tickers = [_ticker_row(7, "ZZZ"), _ticker_row(1, "AAA")]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(7), AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores[AssetId(7)] == pytest.approx(0.20, abs=1e-12)
    assert scores[AssetId(1)] == pytest.approx(0.10, abs=1e-12)


def test_member_inactive_at_dt_is_omitted() -> None:
    """A tickers-view interval that does not contain dt omits the asset."""
    sep = _sep_rows("AAA", 110.0)
    # AAA delisted 2023-06-01 (lastpricedate before dt) -> not active at dt.
    tickers = [_ticker_row(1, "AAA", last=date(2023, 6, 1))]
    pit_view = _make_pit_view(sep, [], tickers)
    universe = _FakeUniverse([AssetId(1)])
    scores = Momentum12_1Signal().compute(universe, _DT, pit_view)
    assert scores == {}


# ----- empty universe -----


def test_empty_universe_returns_empty_dict() -> None:
    pit_view = _make_pit_view([], [], [])
    universe = _FakeUniverse([])
    assert Momentum12_1Signal().compute(universe, _DT, pit_view) == {}


def test_required_lookback_days_is_273() -> None:
    assert Momentum12_1Signal().required_lookback_days() == 273
