"""Almgren 2005 square-root market-impact cost model.

Per ADR 0001 decision 6 and ADR 0005 step 1 the default cost model is
SquareRootImpactCostModel with the Almgren et al. 2005 Risk magazine v18
Section 3 calibration: eta=0.142, beta=0.6, gamma=0.314. The formula is

    tcost_fraction = (1/2) * gamma * sigma_D * (Q/V_D) * (Theta/V_D)^(1/4)
                   + eta * sigma_D * |Q/(V_D*T)|^beta
    tcost_bps      = tcost_fraction * 10_000

where Q is share count (not dollar notional), V_D is the trailing average
daily volume in shares, Theta is shares outstanding (the float-adjusted
turnover proxy), T is the execution horizon as fraction of a trading day
(1.0 at v1; ADR 0005 step 7 fixes one-fill-per-(asset, dt) for daily bars).

LinearImpact and FixedBps stay NotImplementedError pending M2 PR B / later.
NoImpact's bodies are unchanged from pre-PR-A scaffold.

Per ADR 0005 step 8 the per-(asset, dt) sigma_D / V_D / Theta values are
pre-computed at Backtest.__init__ once and injected into the cost model as
a `MarketStateLookup` (attrs-frozen wrapper over a dict). The cost model
itself is constructed once per backtest run, NOT per bar; PR B's BarLoop
reuses the same instance for every estimate / compute call.

Per the M2 PR A reviewer pass:
- Q convention is shares, NOT dollar notional. The fixture's $1M monthly
  rebalance is for the FIM-revision test (ADR 0007) only; that test
  derives shares = abs(dollar_notional / price) at the start of the
  fixture build.
- compute(fill_state) reads (asset_id, dt) where dt is interpreted as
  America/New_York (timezone-naive datetimes are taken as ET).
- `slippage_bps` is 0 at v1 per ADR 0005 step 3 (epsilon_bps default = 0).
- `commission` on the returned CostBreakdown is 0; the matcher (PR B)
  sums the cost-model output with the Commission instance's output.
- The Decimal <-> float boundary uses Decimal(repr(float_value)) for the
  pinned-precision round-trip; getcontext().prec is set at module level to
  the standard 28 so the convention is reproducible.
"""

from __future__ import annotations

import warnings
import zoneinfo
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Literal

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.execution.cost.base import (
    CostBreakdown,
    Direction,
    FillCostComputer,
    FillState,
    PreTradeCostEstimator,
)


# Decimal precision used at the float -> Decimal boundary inside
# `estimate` and `compute`. The conversion is wrapped in `localcontext()`
# so a third-party library that mutates `decimal.getcontext()` cannot
# silently change our boundary precision.
_DECIMAL_BOUNDARY_PREC = 28

# America/New_York timezone for the documented dt convention. Naive
# datetimes are taken as ET; timezone-aware datetimes are converted to
# ET before extracting the calendar date so a UTC `2024-01-03T03:00:00`
# (which IS `2024-01-02T22:00:00` ET) resolves to date(2024, 1, 2).
_ET = zoneinfo.ZoneInfo("America/New_York")


# Default Almgren calibration constants per ADR 0005 step 1.
DEFAULT_ETA = Decimal("0.142")
DEFAULT_BETA = Decimal("0.6")
DEFAULT_GAMMA = Decimal("0.314")
# Single-bar execution horizon. ADR 0005 step 7 fixes the matcher to
# one-fill-per-(asset, dt); intraday slicing is v1.1.
DEFAULT_T = 1.0


def _et_date(dt: datetime) -> date:
    """Return the calendar date in America/New_York.

    Naive datetimes are treated as already-ET (no conversion). Timezone-
    aware datetimes are converted to ET first so cross-zone callers get
    the correct trading-day key for the MarketStateLookup.
    """
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(_ET).date()


def to_boundary_decimal(value: float) -> Decimal:
    """Convert a float to Decimal at the locked boundary precision.

    Per Plan-reviewer High 3 on M3 PR 2: this helper is public (no
    leading underscore) because it is consumed across packages. The
    `data.sources.sharadar` module imports it for per-row PitDataSource
    method returns (get_price + get_fundamental); the `execution.cost`
    and `execution.matching` modules consume it for Order/Fill boundary
    conversion. Single source of truth for the `Decimal(repr(float))`
    semantic per docs/methodology/pydantic_polars_boundary.md.

    Uses `localcontext()` so a third-party library that lowers
    `decimal.getcontext().prec` cannot silently truncate the conversion.
    """
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_BOUNDARY_PREC
        return Decimal(repr(value))


# Backwards-compatible private alias retained for callers that imported
# the M2-era underscore name. The public name `to_boundary_decimal` is the
# canonical surface; this alias may be removed in v1.1.
_to_boundary_decimal = to_boundary_decimal


@attrs.frozen(slots=True)
class MarketStateRow:
    """Per-(asset, date) pre-computed cost-model state.

    sigma_D: daily return standard deviation as a fraction (e.g., 0.012
        for SPY at typical ~1.2%/day vol).
    V_D: trailing average daily volume in SHARES (not dollars).
    Theta: shares outstanding (the float-adjusted turnover proxy that
        appears as the Theta/V factor in the permanent-impact term).
    """

    sigma_D: float
    V_D: float
    Theta: float


@attrs.frozen(slots=True)
class MarketStateLookup:
    """attrs-frozen wrapper over a per-(asset, date) dict.

    The dict-of-tuples shape is chosen over a Polars DataFrame because
    the cost model's `estimate` and `compute` are called up to ~500x per
    bar on a full universe; O(1) dict lookup beats Polars row-extraction
    by orders of magnitude. PR B's `Backtest.__init__` materializes this
    once per backtest run from a Polars frame; the instance is reused for
    every `estimate` / `compute` call across the backtest.

    The wrapper hides the underlying dict so a future ADR could swap the
    storage (e.g., a frozen-numpy-array-keyed-by-(asset_idx, day_idx)
    layout) without changing the cost-model API.
    """

    by_key: dict[tuple[AssetId, date], MarketStateRow]

    def __attrs_post_init__(self) -> None:
        for key, row in self.by_key.items():
            if row.V_D <= 0.0:
                raise ValueError(
                    f"MarketStateRow at {key} has V_D={row.V_D}; "
                    f"daily volume must be strictly positive"
                )
            if row.Theta <= 0.0:
                raise ValueError(
                    f"MarketStateRow at {key} has Theta={row.Theta}; "
                    f"shares outstanding must be strictly positive"
                )
            if row.sigma_D < 0.0:
                raise ValueError(
                    f"MarketStateRow at {key} has sigma_D={row.sigma_D}; "
                    f"daily vol must be non-negative"
                )

    def get(self, asset_id: AssetId, dt: date) -> MarketStateRow:
        """Return the row for (asset_id, dt). Raises KeyError if missing."""
        return self.by_key[(asset_id, dt)]


def _almgren_terms(
    eta: float,
    beta: float,
    gamma: float,
    sigma_D: float,
    V_D: float,
    Theta: float,
    Q: float,
    T: float,
) -> tuple[float, float]:
    """Pure-float Almgren 2005 formula evaluation.

    Returns (permanent_bps, temporary_bps). All floats; Decimal conversion
    happens at the public API boundary in `estimate` and `compute`.

    Q is abs(shares), V_D is daily share volume, Theta is shares
    outstanding. T is the execution horizon as a fraction of a day.
    """
    # Edge case: zero shares = zero cost. Avoid 0**beta numerical noise
    # and skip the entire computation.
    if Q == 0.0:
        return 0.0, 0.0
    participation = Q / V_D
    permanent_fraction = (
        0.5 * gamma * sigma_D * participation * (Theta / V_D) ** 0.25
    )
    # T is the execution horizon as fraction-of-day; |Q / (V_D * T)| is
    # the per-time participation rate. abs() is for safety; Q is already
    # non-negative by the abs(shares) convention at the call site.
    intensity = abs(Q / (V_D * T))
    temporary_fraction = eta * sigma_D * intensity ** beta
    return permanent_fraction * 10_000.0, temporary_fraction * 10_000.0


@attrs.frozen(slots=True)
class SquareRootImpactCostModel(PreTradeCostEstimator, FillCostComputer):
    """Almgren 2005 square-root market-impact model.

    Default parameters (1998-2000 NYSE/Nasdaq calibration):
      eta = 0.142
      beta = 0.6
      gamma = 0.314

    Bouchaud override: beta = 0.5. PR B exposes this via the
    --impact-model=bouchaud CLI flag; PR A's tests instantiate the
    override directly through the constructor.

    `attrs.frozen` enforces the construct-once-per-backtest contract:
    no caller can mutate `market_state` or any calibration parameter
    mid-backtest. The execution horizon T is fixed at v1 to 1.0
    (single-bar fill) per ADR 0005 step 7 and is encoded as a class-
    level constant rather than a field to keep frozen instances small.
    """

    market_state: MarketStateLookup
    eta: Decimal = attrs.field(default=DEFAULT_ETA)
    beta: Decimal = attrs.field(default=DEFAULT_BETA)
    gamma: Decimal = attrs.field(default=DEFAULT_GAMMA)

    _T: float = DEFAULT_T

    def __attrs_post_init__(self) -> None:
        if self.eta <= Decimal("0"):
            raise ValueError(f"eta must be positive; got {self.eta}")
        if self.beta <= Decimal("0"):
            raise ValueError(f"beta must be positive; got {self.beta}")
        if self.gamma < Decimal("0"):
            raise ValueError(f"gamma must be non-negative; got {self.gamma}")

    def estimate(
        self,
        asset_id: AssetId,
        shares: Decimal,
        direction: Direction,  # noqa: ARG002  v2 spread model per ADR 0005 step 3
        dt: datetime,
    ) -> Decimal:
        """Return expected total cost in basis points of notional.

        `direction` is reserved for the v2 spread model per ADR 0005
        step 3 which fixes epsilon_bps=0 at v1; the formula is symmetric
        for buy and sell at v1 so the parameter is unused.

        `dt` is interpreted as America/New_York. Timezone-naive datetimes
        are taken as already-ET (no conversion). Timezone-aware datetimes
        are converted to ET via astimezone() before the calendar-date
        lookup so cross-zone callers do not silently key the wrong
        trading day.
        """
        row = self.market_state.get(asset_id, _et_date(dt))
        Q = float(abs(shares))
        permanent_bps, temporary_bps = _almgren_terms(
            eta=float(self.eta),
            beta=float(self.beta),
            gamma=float(self.gamma),
            sigma_D=row.sigma_D,
            V_D=row.V_D,
            Theta=row.Theta,
            Q=Q,
            T=self._T,
        )
        return _to_boundary_decimal(permanent_bps + temporary_bps)

    def compute(self, fill_state: FillState) -> CostBreakdown:
        """Per-fill cost decomposition.

        Returns a CostBreakdown with:
        - slippage_bps = 0 (ADR 0005 step 3 fixes epsilon_bps=0 at v1).
        - temporary_impact_bps = eta * sigma_D * |Q/(V_D*T)|^beta * 10_000.
        - permanent_impact_bps = (1/2) * gamma * sigma_D * (Q/V_D)
              * (Theta/V_D)^(1/4) * 10_000.
        - commission = 0 (the matcher in PR B sums the cost-model output
          with the Commission instance's output before constructing the
          user-facing CostBreakdown).

        Note: Fill.permanent_impact_per_share is derived by the matcher
        in PR B via Decimal(repr(float(fill_price) *
        float(permanent_impact_bps) / 10_000)). PR A does not modify
        Fill or compute that field.
        """
        row = self.market_state.get(
            fill_state.asset_id, _et_date(fill_state.dt)
        )
        Q = float(abs(fill_state.shares))
        permanent_bps, temporary_bps = _almgren_terms(
            eta=float(self.eta),
            beta=float(self.beta),
            gamma=float(self.gamma),
            sigma_D=row.sigma_D,
            V_D=row.V_D,
            Theta=row.Theta,
            Q=Q,
            T=self._T,
        )
        return CostBreakdown(
            slippage_bps=Decimal("0"),
            temporary_impact_bps=_to_boundary_decimal(temporary_bps),
            permanent_impact_bps=_to_boundary_decimal(permanent_bps),
            commission=Decimal("0"),
        )


class LinearImpact(PreTradeCostEstimator, FillCostComputer):
    """Almgren-Chriss 2000 linear model. Available; not default."""

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        raise NotImplementedError("M2 PR B+ deliverable")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        raise NotImplementedError("M2 PR B+ deliverable")


class FixedBps(PreTradeCostEstimator, FillCostComputer):
    """Single-parameter slippage. Available; not default."""

    def __init__(self, bps: Decimal) -> None:
        raise NotImplementedError("M2 PR B+ deliverable")

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        raise NotImplementedError("M2 PR B+ deliverable")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        raise NotImplementedError("M2 PR B+ deliverable")


class NoImpact(PreTradeCostEstimator, FillCostComputer):
    """Zero-cost cost model.

    Constructable only with unsuitable_for_deployment=True. Emits a
    runtime warning when used. Per ADR 0002 decision 5 / ADR 0005 step 7
    this is the API-level safety belt that prevents accidentally leaving
    zero-cost flags on across a study.
    """

    def __init__(self, unsuitable_for_deployment: Literal[True]) -> None:
        if not unsuitable_for_deployment:
            raise ValueError(
                "NoImpact requires unsuitable_for_deployment=True. "
                "Backtests with zero-cost slippage are not deployment-ready."
            )
        warnings.warn(
            "NoImpact in use; results overstate strategy returns.",
            stacklevel=2,
        )

    def estimate(
        self, asset_id: AssetId, shares: Decimal, direction: Direction, dt: datetime
    ) -> Decimal:
        return Decimal("0")

    def compute(self, fill_state: FillState) -> CostBreakdown:
        return CostBreakdown(
            slippage_bps=Decimal("0"),
            temporary_impact_bps=Decimal("0"),
            permanent_impact_bps=Decimal("0"),
            commission=Decimal("0"),
        )
