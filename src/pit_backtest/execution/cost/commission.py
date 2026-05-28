"""Commission models with typed units.

Per ADR 0002 decision 5: the /100.0 regression unit test (the backtrader
silent rescale bug class) is the canonical M2 acceptance criterion for
this module.

Units are explicit at construction: a Commission instance carries either
a per-share rate or a basis-point rate, not a raw number that could be
interpreted as either.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

import attrs


class Commission(Protocol):
    """Per-fill commission lookup."""

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        """Return the commission dollar amount for this fill."""
        ...


@attrs.frozen(slots=True)
class PerShareCommission(Commission):
    """Commission specified as dollars per share.

    Construction: PerShareCommission(rate_per_share=Decimal("0.005"))
    """

    rate_per_share: Decimal

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        raise NotImplementedError("M2 deliverable")


@attrs.frozen(slots=True)
class BasisPointsCommission(Commission):
    """Commission specified as basis points of notional.

    Construction: BasisPointsCommission(rate_bps=Decimal("5"))
    """

    rate_bps: Decimal

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        raise NotImplementedError("M2 deliverable")
