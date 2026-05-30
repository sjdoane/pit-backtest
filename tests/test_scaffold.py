"""Smoke tests verifying the pre-M1 package scaffold imports cleanly.

Every protocol stub raises NotImplementedError on call; these tests only
verify the modules import and the records construct.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest


def test_imports() -> None:
    """Every package in src/pit_backtest/ imports without error."""
    import pit_backtest
    import pit_backtest.analytics.concentration
    import pit_backtest.analytics.distribution
    import pit_backtest.analytics.drawdown
    import pit_backtest.analytics.scorecard
    import pit_backtest.analytics.sharpe
    import pit_backtest.cli.config
    import pit_backtest.cli.main
    import pit_backtest.data.adjustments
    import pit_backtest.data.contracts
    import pit_backtest.data.records
    import pit_backtest.data.resolver
    import pit_backtest.data.sources.base
    import pit_backtest.data.sources.manifest
    import pit_backtest.data.sources.sharadar
    import pit_backtest.data.universe
    import pit_backtest.data.validated
    import pit_backtest.engine.backtest
    import pit_backtest.engine.bar_loop
    import pit_backtest.engine.runner
    import pit_backtest.engine.state
    import pit_backtest.execution.clock
    import pit_backtest.execution.cost.base
    import pit_backtest.execution.cost.commission
    import pit_backtest.execution.cost.impact
    import pit_backtest.execution.matching
    import pit_backtest.execution.orders
    import pit_backtest.policy.base
    import pit_backtest.risk.attribution
    import pit_backtest.signal.base
    import pit_backtest.signal.momentum
    import pit_backtest.utils.frames
    import pit_backtest.utils.logging
    import pit_backtest.utils.timezones
    import pit_backtest.validation.confidence_tier
    import pit_backtest.validation.cv
    import pit_backtest.validation.trial_registry

    assert pit_backtest.__version__ == "0.0.0"


def test_price_record_constructs() -> None:
    """attrs frozen PriceRecord can be constructed and is immutable."""
    from pit_backtest.data.records import AssetId, PriceRecord

    rec = PriceRecord(
        asset_id=AssetId(199059),  # SPY permaticker (illustrative)
        period_end_dt=datetime(2024, 3, 15, 16, 0),
        available_dt=datetime(2024, 3, 15, 16, 0),
        open=Decimal("517.95"),
        high=Decimal("518.43"),
        low=Decimal("510.27"),
        close=Decimal("512.85"),
        volume=92_750_000,
        cumulative_adjustment=Decimal("1.0"),
    )
    assert rec.close == Decimal("512.85")

    with pytest.raises(attrs_frozen_error()):
        rec.close = Decimal("0.0")  # type: ignore[misc]


def attrs_frozen_error() -> type[Exception]:
    """attrs raises FrozenInstanceError on assignment to a frozen instance."""
    import attrs

    return attrs.exceptions.FrozenInstanceError


def test_order_requires_fill_price_model() -> None:
    """Constructing an Order without fill_price_model raises TypeError."""
    from pit_backtest.data.records import AssetId
    from pit_backtest.execution.orders import FillPriceModel, Order

    # Positive case: explicit FillPriceModel works.
    Order(
        order_id="o-0001",
        asset_id=AssetId(199059),
        quantity=Decimal("100"),
        fill_price_model=FillPriceModel.CLOSE,
        submit_dt=datetime(2024, 3, 15, 16, 0),
    )

    # Negative case: omitting fill_price_model raises (attrs enforces
    # required positional arguments).
    with pytest.raises(TypeError):
        Order(  # type: ignore[call-arg]
            order_id="o-0002",
            asset_id=AssetId(199059),
            quantity=Decimal("100"),
            submit_dt=datetime(2024, 3, 15, 16, 0),
        )


def test_no_impact_requires_explicit_flag() -> None:
    """NoImpact construction without unsuitable_for_deployment=True raises."""
    from pit_backtest.execution.cost.impact import NoImpact

    with pytest.raises(ValueError, match="unsuitable_for_deployment"):
        NoImpact(unsuitable_for_deployment=False)  # type: ignore[arg-type]

    # Constructing with the flag emits a warning (handled by filterwarnings).
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        NoImpact(unsuitable_for_deployment=True)


def test_render_enforcement_fires_on_raw_sr() -> None:
    """BacktestResult with raw SR alone and non-single-run tier raises."""
    from datetime import date

    from pit_backtest.analytics.drawdown import DrawdownDurationReport
    from pit_backtest.analytics.scorecard import (
        Attribution,
        BacktestResult,
        GeneralCharacteristics,
        ImplementationShortfall,
        Performance,
        RenderEnforcementError,
        RiskAdjusted,
        RunsAndDrawdowns,
        Scorecard,
    )
    from pit_backtest.validation.confidence_tier import ConfidenceTier

    scorecard = Scorecard(
        general=GeneralCharacteristics(
            n_trading_days=252,
            n_assets=1,
            universe_id="SPY",
            start_dt="2024-01-01",
            end_dt="2024-12-31",
        ),
        performance=Performance(
            total_return=0.10,
            annualized_return=0.10,
            annualized_volatility=0.15,
        ),
        runs_and_drawdowns=RunsAndDrawdowns(
            max_drawdown=0.05,
            drawdown_duration=DrawdownDurationReport(
                days=10,
                is_censored_at_end=False,
                peak_dt=date(2024, 3, 1),
                trough_dt=date(2024, 3, 11),
            ),
            longest_winning_run=5,
            longest_losing_run=3,
        ),
        implementation_shortfall=ImplementationShortfall(
            total_commission=Decimal("0"),
            total_slippage_bps=Decimal("0"),
            total_temporary_impact_bps=Decimal("0"),
            total_permanent_impact_bps=Decimal("0"),
        ),
        risk_adjusted=RiskAdjusted(
            sr_hat=0.67, psr=None, dsr=None, min_trl=None
        ),
        attribution=Attribution(by_year={2024: 0.10}),
    )

    # Forbidden: raw SR alone with CPCV tier and no PSR/DSR.
    with pytest.raises((RenderEnforcementError, ValueError)):
        BacktestResult(
            sr_hat=0.67,
            psr=None,
            dsr=None,
            min_trl=None,
            confidence_tier=ConfidenceTier.CPCV_WITH_DSR_CORRECTION,
            scorecard=scorecard,
        )

    # Allowed: raw SR alone with single_run_pre_specified tier.
    BacktestResult(
        sr_hat=0.67,
        psr=None,
        dsr=None,
        min_trl=None,
        confidence_tier=ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
        scorecard=scorecard,
    )
