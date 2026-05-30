"""LdP chapter 14 scorecard.

User-facing render target; Pydantic per the boundary contract in
docs/methodology/pydantic_polars_boundary.md.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from pit_backtest.analytics.drawdown import DrawdownDurationReport
from pit_backtest.validation.confidence_tier import ConfidenceTier


_SCORECARD_CONFIG = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class GeneralCharacteristics(BaseModel):
    model_config = _SCORECARD_CONFIG
    n_trading_days: int
    n_assets: int
    universe_id: str
    start_dt: str
    end_dt: str


class Performance(BaseModel):
    model_config = _SCORECARD_CONFIG
    total_return: float
    annualized_return: float
    annualized_volatility: float


class RunsAndDrawdowns(BaseModel):
    model_config = _SCORECARD_CONFIG
    max_drawdown: float
    drawdown_duration: DrawdownDurationReport
    longest_winning_run: int
    longest_losing_run: int


class ImplementationShortfall(BaseModel):
    model_config = _SCORECARD_CONFIG
    total_commission: Decimal
    total_slippage_bps: Decimal
    total_temporary_impact_bps: Decimal
    total_permanent_impact_bps: Decimal


class RiskAdjusted(BaseModel):
    model_config = _SCORECARD_CONFIG
    sr_hat: float
    psr: float | None
    dsr: float | None
    min_trl: int | None


class Attribution(BaseModel):
    model_config = _SCORECARD_CONFIG
    # Minimal at v1; expanded in v1.1 with factor decomposition.
    by_year: dict[int, float]


class Scorecard(BaseModel):
    """LdP chapter 14 scorecard.

    Six categories per ADR 0001 decision 4. Rendered as Markdown via
    to_markdown(); persisted as JSON via .model_dump_json().
    """

    model_config = _SCORECARD_CONFIG

    general: GeneralCharacteristics
    performance: Performance
    runs_and_drawdowns: RunsAndDrawdowns
    implementation_shortfall: ImplementationShortfall
    risk_adjusted: RiskAdjusted
    attribution: Attribution

    def to_markdown(self) -> str:
        raise NotImplementedError("M4 deliverable")


class RenderEnforcementError(ValueError):
    """Raised when render is attempted with raw SR alone without an
    accompanying PSR or DSR and the confidence tier is not single_run_pre_specified.
    """


class BacktestResult(BaseModel):
    """User-facing backtest result.

    Per ADR 0003 architecture: the render-path enforcement on raw SR
    without PSR/DSR fires here, in the Pydantic model validator.

    Per ADR 0015: `__lt__` is defined on `sr_hat` so
    `BacktestPathDistribution[BacktestResult].percentiles` can call
    `sorted()` without a `type: ignore`. The ordering key matches the
    LdP 2018 chapter 13 + 14 convention of per-path Sharpe as the
    canonical CPCV path-ranking surface.
    """

    model_config = _SCORECARD_CONFIG

    sr_hat: float
    psr: float | None
    dsr: float | None
    min_trl: int | None
    confidence_tier: ConfidenceTier
    scorecard: Scorecard

    @model_validator(mode="after")
    def enforce_render_path(self) -> "BacktestResult":
        if (
            self.psr is None
            and self.dsr is None
            and self.confidence_tier != ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED
        ):
            raise RenderEnforcementError(
                "BacktestResult with raw SR alone requires "
                "ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED."
            )
        return self

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, BacktestResult):
            return NotImplemented
        return self.sr_hat < other.sr_hat
