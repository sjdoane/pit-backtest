"""Commission models with typed units.

Per ADR 0002 decision 5 / ADR 0005 step 10: the /100.0 regression unit
test (the backtrader silent rescale bug class) is the canonical M2
acceptance criterion for this module. The killer assertion lives in
`tests/execution/cost/test_commission.py` on BOTH PerShareCommission and
BasisPointsCommission per ADR 0005's reviewer-pass-corrected guidance.

Convention locked at the M2 PR A reviewer pass:
- `shares` is the SIGNED quantity at the Order boundary (positive=buy,
  negative=sell).
- `notional` is the SIGNED product `shares * fill_price` (negative when
  shares are negative; positive when shares are positive). The matcher
  in PR B will compute this directly from the Fill's shares and
  fill_price.
- Commission is always a POSITIVE dollar cash outflow. Implementations
  use `abs(shares)` or `abs(notional)` as the multiplier so a signed
  input cannot accidentally turn the commission negative.

Units are explicit at construction: a Commission instance carries either
a per-share rate or a basis-point rate, not a raw number that could be
interpreted as either.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

import attrs


# Decimal exact divisor for the basis-points -> fraction conversion.
# Using a Decimal literal avoids the float-to-Decimal coercion path that
# could silently introduce sub-bp imprecision in the per-fill computation.
_BPS_TO_FRACTION_DIVISOR: Decimal = Decimal("10000")


class Commission(Protocol):
    """Per-fill commission lookup.

    `shares` is signed (positive=buy, negative=sell). `notional` is
    signed (`shares * fill_price`). Implementations return a positive
    dollar amount via abs() on whichever of the two inputs they use.
    """

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        """Return the commission dollar amount for this fill.

        Always a positive Decimal regardless of trade direction.
        """
        ...


@attrs.frozen(slots=True)
class PerShareCommission(Commission):
    """Commission specified as dollars per share.

    Construction: PerShareCommission(rate_per_share=Decimal("0.005"))
    means $0.005 per share.

    The /100 silent-rescale bug (backtrader's historical regression
    where 0.005 was silently divided by 100 to yield 0.00005) is
    locked out by `tests/execution/cost/test_commission.py::
    test_per_share_commission_no_silent_rescale`. A meta-test in the
    same file asserts that a deliberately faulty implementation that
    divides by 100 internally would fall in the rejection band, proving
    the killer assert is not dead code.
    """

    rate_per_share: Decimal

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        """`notional` is unused for the per-share class; the protocol
        includes it because `BasisPointsCommission` needs it.
        """
        del notional  # unused for the per-share class
        return abs(shares) * self.rate_per_share


@attrs.frozen(slots=True)
class BasisPointsCommission(Commission):
    """Commission specified as basis points of notional.

    Construction: BasisPointsCommission(rate_bps=Decimal("5")) means
    5 basis points (0.05%) of notional. Same /100 protection as
    PerShareCommission.
    """

    rate_bps: Decimal

    def commission_for(self, shares: Decimal, notional: Decimal) -> Decimal:
        """`shares` is unused for the bps class; the protocol includes
        it because `PerShareCommission` needs it.
        """
        del shares  # unused for the bps class
        return abs(notional) * self.rate_bps / _BPS_TO_FRACTION_DIVISOR
