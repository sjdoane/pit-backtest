"""M1 demo helpers: ticker-to-AssetId map and FixedTickerUniverse.

M3 replaces these with the real IdentifierResolver (Sharadar TICKERS) and
SharadarSP500Universe. For M1 the constant-weight SPY/AGG/GLD demo uses a
hand-rolled three-ticker map so we do not block on TICKERS adapter work.

Naming: this module is `m1_demo.py` (not `_m1_demo_resolver.py` from the
plan) because it carries the universe too and the underscore prefix
adds nothing in a single-author repo.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Mapping

import attrs

from pit_backtest.data.records import AssetId


# Hand-rolled AssetId mapping for the M1 SPY/AGG/GLD demo. M3 replaces with
# Sharadar TICKERS-derived permatickers via IdentifierResolver.
M1_DEMO_TICKER_TO_ASSET_ID: dict[str, AssetId] = {
    "SPY": AssetId(0),
    "AGG": AssetId(1),
    "GLD": AssetId(2),
}

M1_DEMO_ASSET_ID_TO_TICKER: dict[AssetId, str] = {
    v: k for k, v in M1_DEMO_TICKER_TO_ASSET_ID.items()
}


def ticker_to_asset_id(ticker: str) -> AssetId:
    """Map a ticker string to its M1-demo AssetId. KeyError for unknown."""
    if ticker not in M1_DEMO_TICKER_TO_ASSET_ID:
        raise KeyError(
            f"unknown M1 demo ticker {ticker!r}; "
            f"known: {sorted(M1_DEMO_TICKER_TO_ASSET_ID.keys())}"
        )
    return M1_DEMO_TICKER_TO_ASSET_ID[ticker]


def asset_id_to_ticker(asset_id: AssetId) -> str:
    """Inverse of ticker_to_asset_id. KeyError for unknown."""
    if asset_id not in M1_DEMO_ASSET_ID_TO_TICKER:
        raise KeyError(
            f"unknown M1 demo asset_id {asset_id}; "
            f"known: {sorted(M1_DEMO_ASSET_ID_TO_TICKER.keys())}"
        )
    return M1_DEMO_ASSET_ID_TO_TICKER[asset_id]


@attrs.frozen(slots=True)
class FixedTickerUniverse:
    """Universe that always reports a fixed set of tickers as members.

    M1 substitute for SharadarSP500Universe; M3 replaces with the real
    PIT membership log.
    """

    member_ids: frozenset[AssetId]

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        return asset_id in self.member_ids

    def members_at(self, dt: datetime) -> list[AssetId]:
        return sorted(self.member_ids)

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime | None]]:
        # M1 demo: open-ended membership for any known asset. Per M3 PR 4
        # the Protocol return type changed from
        # `list[tuple[datetime, datetime]]` to
        # `list[tuple[datetime, datetime | None]]`; the open-ended end is
        # now `None` rather than the magic `datetime.max` sentinel.
        if asset_id not in self.member_ids:
            return []
        return [(datetime.min, None)]


def fixed_universe_from_tickers(tickers: Iterable[str]) -> FixedTickerUniverse:
    """Construct a FixedTickerUniverse for a set of M1-demo tickers."""
    member_ids = frozenset(ticker_to_asset_id(t) for t in tickers)
    return FixedTickerUniverse(member_ids=member_ids)
