"""SensitivityBand attrs container tests (M2 PR C1).

Per ADR 0010 lock #2, #3, #5 the container validates parameter_values
sorted ascending, central_value in parameter_values, dict keys match,
and confidence_tier is exactly SWEEP_SELECTED_NO_CORRECTION. The
from_run_sweep factory wraps a list of ConstantWeightDemoResult into
a SensitivityBand with cross-result consistency checks (tickers,
window, initial_capital, sharadar_bundle).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from pit_backtest.analytics.sensitivity import SensitivityBand
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.confidence_tier import ConfidenceTier


def _make_equity_curve(final_nav: float, n_bars: int = 252) -> pl.DataFrame:
    """Build a synthetic equity curve with n_bars rows."""
    start = date(2024, 1, 2)
    dts = [date.fromordinal(start.toordinal() + i) for i in range(n_bars)]
    navs = [1_000_000.0 + (final_nav - 1_000_000.0) * (i / (n_bars - 1)) for i in range(n_bars)]
    cash = [final_nav] * n_bars
    return pl.DataFrame({"dt": dts, "cash": cash, "nav": navs})


def _make_result(final_pnl: float) -> ConstantWeightDemoResult:
    return ConstantWeightDemoResult(
        final_pnl=final_pnl,
        final_nav=1_000_000.0 + final_pnl,
        initial_capital=1_000_000.0,
        equity_curve=_make_equity_curve(final_nav=1_000_000.0 + final_pnl),
        n_trading_days=252,
        n_rebalances=12,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        confidence_tier=ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
        sharadar_bundle="sharadar_test",
    )


# ----- Construction validation -----


def test_sensitivity_band_attrs_construction_valid() -> None:
    """Valid construction succeeds and the container is frozen."""
    eta_values = (Decimal("0.05"), Decimal("0.10"), Decimal("0.142"))
    per_param_equity = {v: _make_equity_curve(1_100_000.0) for v in eta_values}
    per_param_pnl = {v: 100_000.0 for v in eta_values}
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.10"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    assert band.parameter_name == "eta"
    assert band.central_value == Decimal("0.10")


def test_sensitivity_band_rejects_non_sweep_confidence_tier() -> None:
    """Per ADR 0010 lock #3 the container rejects any tier other than
    SWEEP_SELECTED_NO_CORRECTION at construction.
    """
    eta_values = (Decimal("0.10"),)
    per_param_equity = {Decimal("0.10"): _make_equity_curve(1_000_000.0)}
    per_param_pnl = {Decimal("0.10"): 0.0}
    for bad_tier in (
        ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
        ConfidenceTier.WALK_FORWARD_VALIDATED,
        ConfidenceTier.CPCV_WITH_DSR_CORRECTION,
    ):
        with pytest.raises(ValueError, match="BacktestPathDistribution"):
            SensitivityBand(
                parameter_name="eta",
                parameter_values=eta_values,
                per_parameter_equity=per_param_equity,
                per_parameter_final_pnl=per_param_pnl,
                central_value=Decimal("0.10"),
                confidence_tier=bad_tier,
                tickers=("SPY",),
                start_dt=date(2024, 1, 2),
                end_dt=date(2024, 12, 31),
                initial_capital=1_000_000.0,
                sharadar_bundle="sharadar_test",
            )


def test_sensitivity_band_requires_central_in_parameter_values() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {v: _make_equity_curve(1_000_000.0) for v in eta_values}
    per_param_pnl = {v: 0.0 for v in eta_values}
    with pytest.raises(ValueError, match="central_value"):
        SensitivityBand(
            parameter_name="eta",
            parameter_values=eta_values,
            per_parameter_equity=per_param_equity,
            per_parameter_final_pnl=per_param_pnl,
            central_value=Decimal("0.142"),  # NOT in parameter_values
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=("SPY",),
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 12, 31),
            initial_capital=1_000_000.0,
            sharadar_bundle="sharadar_test",
        )


def test_sensitivity_band_requires_sorted_parameter_values() -> None:
    eta_values = (Decimal("0.10"), Decimal("0.05"))  # not sorted
    per_param_equity = {v: _make_equity_curve(1_000_000.0) for v in eta_values}
    per_param_pnl = {v: 0.0 for v in eta_values}
    with pytest.raises(ValueError, match="sorted ascending"):
        SensitivityBand(
            parameter_name="eta",
            parameter_values=eta_values,
            per_parameter_equity=per_param_equity,
            per_parameter_final_pnl=per_param_pnl,
            central_value=Decimal("0.05"),
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=("SPY",),
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 12, 31),
            initial_capital=1_000_000.0,
            sharadar_bundle="sharadar_test",
        )


def test_sensitivity_band_rejects_empty_parameter_values() -> None:
    with pytest.raises(ValueError, match="empty"):
        SensitivityBand(
            parameter_name="eta",
            parameter_values=(),
            per_parameter_equity={},
            per_parameter_final_pnl={},
            central_value=Decimal("0.10"),
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=("SPY",),
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 12, 31),
            initial_capital=1_000_000.0,
            sharadar_bundle="sharadar_test",
        )


def test_sensitivity_band_per_parameter_equity_keys_must_match() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {Decimal("0.05"): _make_equity_curve(1_000_000.0)}  # missing 0.10
    per_param_pnl = {v: 0.0 for v in eta_values}
    with pytest.raises(ValueError, match="per_parameter_equity"):
        SensitivityBand(
            parameter_name="eta",
            parameter_values=eta_values,
            per_parameter_equity=per_param_equity,
            per_parameter_final_pnl=per_param_pnl,
            central_value=Decimal("0.10"),
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=("SPY",),
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 12, 31),
            initial_capital=1_000_000.0,
            sharadar_bundle="sharadar_test",
        )


def test_sensitivity_band_per_parameter_final_pnl_keys_must_match() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {v: _make_equity_curve(1_000_000.0) for v in eta_values}
    per_param_pnl = {Decimal("0.05"): 0.0}  # missing 0.10
    with pytest.raises(ValueError, match="per_parameter_final_pnl"):
        SensitivityBand(
            parameter_name="eta",
            parameter_values=eta_values,
            per_parameter_equity=per_param_equity,
            per_parameter_final_pnl=per_param_pnl,
            central_value=Decimal("0.10"),
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=("SPY",),
            start_dt=date(2024, 1, 2),
            end_dt=date(2024, 12, 31),
            initial_capital=1_000_000.0,
            sharadar_bundle="sharadar_test",
        )


# ----- from_run_sweep -----


def test_from_run_sweep_wraps_in_param_grid_order() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"), Decimal("0.142"))
    results = [_make_result(120_000.0), _make_result(100_000.0), _make_result(80_000.0)]
    band = SensitivityBand.from_run_sweep(
        results=results,
        parameter_name="eta",
        parameter_values=eta_values,
        central_value=Decimal("0.10"),
    )
    assert band.per_parameter_final_pnl[Decimal("0.05")] == 120_000.0
    assert band.per_parameter_final_pnl[Decimal("0.10")] == 100_000.0
    assert band.per_parameter_final_pnl[Decimal("0.142")] == 80_000.0
    assert band.confidence_tier == ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION


def test_from_run_sweep_results_count_mismatch_raises() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"), Decimal("0.142"))
    results = [_make_result(120_000.0), _make_result(100_000.0)]  # 2 not 3
    with pytest.raises(ValueError, match="length"):
        SensitivityBand.from_run_sweep(
            results=results,
            parameter_name="eta",
            parameter_values=eta_values,
            central_value=Decimal("0.10"),
        )


def test_from_run_sweep_inconsistent_tickers_raises() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    result_1 = _make_result(120_000.0)
    result_2 = _make_result(100_000.0)
    result_2_other_tickers = result_2.model_copy(update={"tickers": ("AGG",)})
    with pytest.raises(ValueError, match="tickers"):
        SensitivityBand.from_run_sweep(
            results=[result_1, result_2_other_tickers],
            parameter_name="eta",
            parameter_values=eta_values,
            central_value=Decimal("0.10"),
        )


def test_from_run_sweep_inconsistent_window_raises() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    result_1 = _make_result(120_000.0)
    result_2 = _make_result(100_000.0)
    result_2_other_window = result_2.model_copy(
        update={"end_dt": date(2025, 6, 30)}
    )
    with pytest.raises(ValueError, match="window"):
        SensitivityBand.from_run_sweep(
            results=[result_1, result_2_other_window],
            parameter_name="eta",
            parameter_values=eta_values,
            central_value=Decimal("0.10"),
        )


# ----- Accessors and rendering -----


def test_equity_curve_at_returns_correct_frame() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {
        Decimal("0.05"): _make_equity_curve(1_100_000.0),
        Decimal("0.10"): _make_equity_curve(900_000.0),
    }
    per_param_pnl = {Decimal("0.05"): 100_000.0, Decimal("0.10"): -100_000.0}
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.10"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    curve = band.equity_curve_at(Decimal("0.05"))
    assert curve.shape == (252, 3)


def test_equity_curve_at_unknown_value_raises_keyerror() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {v: _make_equity_curve(1_000_000.0) for v in eta_values}
    per_param_pnl = {v: 0.0 for v in eta_values}
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.10"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    with pytest.raises(KeyError, match="not in band"):
        band.equity_curve_at(Decimal("0.99"))


def test_to_plot_frame_shape() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.10"))
    per_param_equity = {v: _make_equity_curve(1_000_000.0, n_bars=10) for v in eta_values}
    per_param_pnl = {v: 0.0 for v in eta_values}
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.10"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    frame = band.to_plot_frame()
    assert frame.shape == (20, 3)  # 2 params * 10 bars
    assert frame.columns == ["parameter_value", "dt", "nav"]
    # Sorted by parameter_value ascending then dt ascending.
    assert float(frame["parameter_value"][0]) == 0.05
    assert float(frame["parameter_value"][10]) == 0.10


def test_render_summary_line_byte_for_byte() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.142"), Decimal("0.30"))
    per_param_equity = {v: _make_equity_curve(1_100_000.0) for v in eta_values}
    per_param_pnl = {
        Decimal("0.05"): 110_000.0,
        Decimal("0.142"): 100_000.0,
        Decimal("0.30"): 90_000.0,
    }
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.142"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    expected = (
        "sensitivity_band: parameter=eta, "
        "values=[0.05, 0.142, 0.30], "
        "central=0.142, "
        "central_pnl=$+100,000.00, "
        "range_pnl=[$+90,000.00, $+110,000.00], "
        "tickers=SPY, "
        "window=2024-01-02..2024-12-31, "
        "snapshot=sharadar_test"
    )
    assert band.render_summary_line() == expected


def test_render_band_table_byte_for_byte() -> None:
    eta_values = (Decimal("0.05"), Decimal("0.142"), Decimal("0.30"))
    per_param_equity = {v: _make_equity_curve(1_100_000.0) for v in eta_values}
    per_param_pnl = {
        Decimal("0.05"): 110_000.0,
        Decimal("0.142"): 100_000.0,
        Decimal("0.30"): 90_000.0,
    }
    band = SensitivityBand(
        parameter_name="eta",
        parameter_values=eta_values,
        per_parameter_equity=per_param_equity,
        per_parameter_final_pnl=per_param_pnl,
        central_value=Decimal("0.142"),
        confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
        tickers=("SPY",),
        start_dt=date(2024, 1, 2),
        end_dt=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        sharadar_bundle="sharadar_test",
    )
    # delta_bps_vs_central: (pnl - central_pnl) / initial_capital * 10000
    # 0.05: (110000 - 100000) / 1000000 * 10000 = +100.00
    # 0.142: 0
    # 0.30: (90000 - 100000) / 1000000 * 10000 = -100.00
    expected = (
        "| eta | final_pnl | delta_bps_vs_central |\n"
        "|---|---|---|\n"
        "| 0.05 | $+110,000.00 | +100.00 |\n"
        "| 0.142 | $+100,000.00 | +0.00 |\n"
        "| 0.30 | $+90,000.00 | -100.00 |"
    )
    assert band.render_band_table() == expected
