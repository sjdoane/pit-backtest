"""PortfolioState: positions, cash, P&L.

The mutable form is used inside the BarLoop; the immutable snapshot
(frozen attrs) is what crosses boundaries to analytics and to the
BacktestResult render path.

Per the M1-day-3 skeptical reviewer (captured in the constant-weight PR
description): the inner-loop arithmetic uses float64 (not Decimal) to
keep the 1e-10 reference-equivalence test achievable. The Decimal fields
on Order/Fill at the boundary are populated via Decimal(repr(float_value));
reads back to float are bit-stable via float(Decimal('...')). The
positions field is named `positions` (not `shares`) so it stays consistent
with TargetPositions.targets and any v1.1 asset class that has a
non-shares unit (futures contracts, options).
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

import attrs

from pit_backtest.data.records import AssetId


class MissingPriceError(KeyError):
    """Raised when mark_to_market is asked to price a position with no current quote.

    M1 raises rather than carries forward. M3 data quality contracts will
    distinguish "vendor gap" (carry forward + warn) from "missing required
    bar" (raise) once the SP500 PIT universe lands.
    """


@attrs.define(slots=True)
class PortfolioState:
    """Mutable per-bar portfolio state. Lives inside the BarLoop.

    For external consumers, .snapshot() returns a frozen PortfolioSnapshot
    that is safe to retain across bars.
    """

    cash: float
    positions: dict[AssetId, float]
    initial_capital: float = 0.0
    realized_pnl: float = 0.0

    def mark_to_market(self, prices: Mapping[AssetId, float]) -> float:
        """Compute total NAV at the given prices.

        Iterates positions in sorted AssetId order per docs/methodology/
        determinism.md Requirement 3 (sorted output frames at every step,
        applied here to the sum order). Float addition is not associative;
        the sorted iteration is what makes engine and reference match to
        1e-10.

        Skips positions with zero shares. Raises MissingPriceError if any
        nonzero position lacks a price (M1 semantic; M3 will soften to
        carry-forward via the data quality contracts).
        """
        total = self.cash
        for asset_id in sorted(self.positions):
            shares = self.positions[asset_id]
            if shares == 0.0:
                continue
            if asset_id not in prices:
                raise MissingPriceError(
                    f"position in asset_id={asset_id} has shares={shares} but "
                    f"no price at this bar; engine cannot mark to market"
                )
            total += shares * prices[asset_id]
        return total

    def snapshot(
        self, dt: datetime, prices: Mapping[AssetId, float]
    ) -> "PortfolioSnapshot":
        """Frozen point-in-time copy. Copies the positions dict so future
        mutation of the live PortfolioState does not affect the snapshot.
        """
        total = self.mark_to_market(prices)
        return PortfolioSnapshot(
            dt=dt,
            cash=self.cash,
            positions=dict(self.positions),
            realized_pnl=self.realized_pnl,
            total_value=total,
            prices_at_snapshot=dict(prices),
        )


@attrs.frozen(slots=True)
class PortfolioSnapshot:
    """Immutable point-in-time portfolio state.

    Returned by PortfolioState.snapshot(). Safe to retain; safe to compare
    across bars; safe to feed into the analytics layer. Carries the prices
    used at snapshot time so downstream consumers can reconstruct without
    re-querying the data feed.
    """

    dt: datetime
    cash: float
    positions: dict[AssetId, float]
    realized_pnl: float
    total_value: float
    prices_at_snapshot: dict[AssetId, float]
