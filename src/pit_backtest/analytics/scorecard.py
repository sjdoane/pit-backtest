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
    # longest_winning_run / longest_losing_run are the longest consecutive
    # run of positive / negative per-bar returns (a 0.0 return bar breaks
    # both runs). Defined here so v1.1 does not silently redefine them as
    # up-trades or up-days.
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
        """Render the six-section LdP chapter 14 scorecard as Markdown.

        Pure render: returns a `str`, no file IO, no wall-clock. `None`
        risk-adjusted statistics render as `n/a`; the censored-drawdown
        flag is surfaced explicitly per the LdP honesty convention.
        """
        gen = self.general
        perf = self.performance
        rad = self.runs_and_drawdowns
        ddr = rad.drawdown_duration
        shortfall = self.implementation_shortfall
        risk = self.risk_adjusted

        def _pct(value: float) -> str:
            return f"{value * 100:.2f}%"

        def _opt(value: float | int | None, fmt: str) -> str:
            return "n/a" if value is None else format(value, fmt)

        trough = "n/a" if ddr.trough_dt is None else str(ddr.trough_dt)
        censored = (
            " (censored; still underwater at window end)"
            if ddr.is_censored_at_end
            else ""
        )

        lines: list[str] = []
        lines.append("# Backtest Scorecard")
        lines.append("")

        lines.append("## General Characteristics")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Universe | {gen.universe_id} |")
        lines.append(f"| Window | {gen.start_dt} to {gen.end_dt} |")
        lines.append(f"| Trading days | {gen.n_trading_days} |")
        lines.append(f"| Assets | {gen.n_assets} |")
        lines.append("")

        lines.append("## Performance")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Total return | {_pct(perf.total_return)} |")
        lines.append(
            f"| Annualized return | {_pct(perf.annualized_return)} |"
        )
        lines.append(
            f"| Annualized volatility | {_pct(perf.annualized_volatility)} |"
        )
        lines.append("")

        lines.append("## Runs and Drawdowns")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Max drawdown | {_pct(rad.max_drawdown)} |")
        lines.append(
            f"| Longest drawdown | {ddr.days} bars{censored} |"
        )
        lines.append(f"| Peak date | {ddr.peak_dt} |")
        lines.append(f"| Trough date | {trough} |")
        lines.append(
            f"| Longest winning run | {rad.longest_winning_run} bars |"
        )
        lines.append(
            f"| Longest losing run | {rad.longest_losing_run} bars |"
        )
        lines.append("")

        lines.append("## Implementation Shortfall")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(
            f"| Total commission | ${shortfall.total_commission:.2f} |"
        )
        lines.append(
            f"| Total slippage | {shortfall.total_slippage_bps:.2f} bps |"
        )
        lines.append(
            f"| Total temporary impact | "
            f"{shortfall.total_temporary_impact_bps:.2f} bps |"
        )
        lines.append(
            f"| Total permanent impact | "
            f"{shortfall.total_permanent_impact_bps:.2f} bps |"
        )
        lines.append("")

        lines.append("## Risk-Adjusted")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Sharpe (per period) | {risk.sr_hat:.4f} |")
        lines.append(f"| PSR | {_opt(risk.psr, '.4f')} |")
        lines.append(f"| DSR | {_opt(risk.dsr, '.4f')} |")
        lines.append(f"| MinTRL | {_opt(risk.min_trl, 'd')} |")
        lines.append("")

        lines.append("## Attribution")
        lines.append("")
        lines.append("| Year | Return |")
        lines.append("| --- | --- |")
        for year in sorted(self.attribution.by_year):
            lines.append(f"| {year} | {_pct(self.attribution.by_year[year])} |")
        lines.append("")

        return "\n".join(lines)


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
