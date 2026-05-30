"""Tests for Scorecard.to_markdown (M4 PR 5).

Covers the six-section render, the censored-drawdown marker, Decimal
shortfall formatting (no float drift), and None risk-adjusted rendering.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pit_backtest.analytics.drawdown import DrawdownDurationReport
from pit_backtest.analytics.scorecard import (
    Attribution,
    GeneralCharacteristics,
    ImplementationShortfall,
    Performance,
    RiskAdjusted,
    RunsAndDrawdowns,
    Scorecard,
)


def _scorecard(
    *,
    is_censored: bool = False,
    trough_dt: date | None = date(2024, 6, 1),
    psr: float | None = 0.8123,
    dsr: float | None = 0.7656,
    min_trl: int | None = 42,
) -> Scorecard:
    return Scorecard(
        general=GeneralCharacteristics(
            n_trading_days=252,
            n_assets=1,
            universe_id="SPY",
            start_dt="2024-01-01",
            end_dt="2024-12-31",
        ),
        performance=Performance(
            total_return=0.1234,
            annualized_return=0.1100,
            annualized_volatility=0.1550,
        ),
        runs_and_drawdowns=RunsAndDrawdowns(
            max_drawdown=0.0727,
            drawdown_duration=DrawdownDurationReport(
                days=10,
                is_censored_at_end=is_censored,
                peak_dt=date(2024, 5, 1),
                trough_dt=trough_dt,
            ),
            longest_winning_run=5,
            longest_losing_run=3,
        ),
        implementation_shortfall=ImplementationShortfall(
            total_commission=Decimal("12.34"),
            total_slippage_bps=Decimal("1.50"),
            total_temporary_impact_bps=Decimal("0.75"),
            total_permanent_impact_bps=Decimal("0.25"),
        ),
        risk_adjusted=RiskAdjusted(
            sr_hat=0.0671, psr=psr, dsr=dsr, min_trl=min_trl
        ),
        attribution=Attribution(by_year={2024: 0.1234, 2023: 0.05}),
    )


def test_to_markdown_renders_all_six_sections() -> None:
    md = _scorecard().to_markdown()
    assert "# Backtest Scorecard" in md
    assert "## General Characteristics" in md
    assert "## Performance" in md
    assert "## Runs and Drawdowns" in md
    assert "## Implementation Shortfall" in md
    assert "## Risk-Adjusted" in md
    assert "## Attribution" in md


def test_to_markdown_formats_percentages() -> None:
    md = _scorecard().to_markdown()
    assert "12.34%" in md  # total_return
    assert "7.27%" in md  # max_drawdown


def test_to_markdown_censored_marker_present_when_censored() -> None:
    md = _scorecard(is_censored=True).to_markdown()
    assert "censored; still underwater at window end" in md


def test_to_markdown_no_censored_marker_when_not_censored() -> None:
    md = _scorecard(is_censored=False).to_markdown()
    assert "censored" not in md


def test_to_markdown_trough_none_renders_na() -> None:
    md = _scorecard(trough_dt=None).to_markdown()
    assert "| Trough date | n/a |" in md


def test_to_markdown_decimal_shortfall_no_float_drift() -> None:
    md = _scorecard().to_markdown()
    assert "$12.34" in md
    assert "1.50 bps" in md
    assert "0.75 bps" in md
    assert "0.25 bps" in md


def test_to_markdown_none_risk_adjusted_renders_na() -> None:
    md = _scorecard(psr=None, dsr=None, min_trl=None).to_markdown()
    assert "| PSR | n/a |" in md
    assert "| DSR | n/a |" in md
    assert "| MinTRL | n/a |" in md


def test_to_markdown_populated_risk_adjusted_formats() -> None:
    md = _scorecard().to_markdown()
    assert "0.8123" in md  # psr
    assert "0.7656" in md  # dsr
    assert "| MinTRL | 42 |" in md


def test_to_markdown_attribution_sorted_by_year() -> None:
    md = _scorecard().to_markdown()
    idx_2023 = md.index("| 2023 |")
    idx_2024 = md.index("| 2024 |")
    assert idx_2023 < idx_2024  # deterministic ascending year order


def test_to_markdown_is_pure_string_no_trailing_none() -> None:
    md = _scorecard().to_markdown()
    assert isinstance(md, str)
    assert "None" not in md  # None values render as n/a, never str(None)
