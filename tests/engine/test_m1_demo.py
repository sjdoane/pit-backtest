"""M1 demo helpers tests (ticker map + FixedTickerUniverse)."""

from __future__ import annotations

from datetime import datetime

import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.engine.m1_demo import (
    M1_DEMO_ASSET_ID_TO_TICKER,
    M1_DEMO_TICKER_TO_ASSET_ID,
    asset_id_to_ticker,
    fixed_universe_from_tickers,
    ticker_to_asset_id,
)


def test_ticker_round_trip() -> None:
    for ticker in ("SPY", "AGG", "GLD"):
        assert asset_id_to_ticker(ticker_to_asset_id(ticker)) == ticker


def test_asset_id_round_trip() -> None:
    for asset_id in (AssetId(0), AssetId(1), AssetId(2)):
        assert ticker_to_asset_id(asset_id_to_ticker(asset_id)) == asset_id


def test_unknown_ticker_raises() -> None:
    with pytest.raises(KeyError, match="unknown M1 demo ticker"):
        ticker_to_asset_id("AAPL")


def test_unknown_asset_id_raises() -> None:
    with pytest.raises(KeyError, match="unknown M1 demo asset_id"):
        asset_id_to_ticker(AssetId(999))


def test_demo_map_consistency() -> None:
    """Forward and reverse maps are inverses."""
    for ticker, aid in M1_DEMO_TICKER_TO_ASSET_ID.items():
        assert M1_DEMO_ASSET_ID_TO_TICKER[aid] == ticker


def test_fixed_universe_membership() -> None:
    u = fixed_universe_from_tickers(("SPY", "AGG"))
    assert u.is_member(ticker_to_asset_id("SPY"), datetime(2024, 3, 15))
    assert u.is_member(ticker_to_asset_id("AGG"), datetime(2024, 3, 15))
    assert not u.is_member(ticker_to_asset_id("GLD"), datetime(2024, 3, 15))


def test_fixed_universe_members_at_returns_sorted() -> None:
    u = fixed_universe_from_tickers(("GLD", "SPY", "AGG"))
    result = u.members_at(datetime(2024, 3, 15))
    assert result == sorted(result)
    assert len(result) == 3
