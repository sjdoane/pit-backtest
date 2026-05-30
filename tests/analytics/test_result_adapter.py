"""Tests for analytics.result_adapter (M4 PR 5).

The adapter computes the LdP scorecard analytics from a
ConstantWeightDemoResult's equity_curve, records the run's trial in a
TrialRegistry, and assembles a BacktestResult. sr_hat is the per-period
Sharpe (mean/stdev of per-bar returns); the adapter's Polars computation
is checked against stdlib statistics.
"""

from __future__ import annotations

import statistics
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.analytics.result_adapter import to_backtest_result
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.confidence_tier import ConfidenceTier
from pit_backtest.validation.trial_registry import TrialRegistry


def _demo_from_navs(
    navs: list[float],
    *,
    dts: list[date] | None = None,
    bundle: str = "sharadar_2026-05-29",
    tier: ConfidenceTier = ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
) -> ConstantWeightDemoResult:
    if dts is None:
        dts = [date(2024, 1, 1 + i) for i in range(len(navs))]
    equity_curve = pl.DataFrame(
        {"dt": dts, "cash": [0.0] * len(navs), "nav": navs}
    )
    return ConstantWeightDemoResult(
        final_pnl=navs[-1] - navs[0],
        final_nav=navs[-1],
        initial_capital=navs[0],
        equity_curve=equity_curve,
        n_trading_days=len(navs),
        n_rebalances=1,
        tickers=("SPY",),
        start_dt=dts[0],
        end_dt=dts[-1],
        confidence_tier=tier,
        sharadar_bundle=bundle,
    )


def _navs_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    navs = [start]
    for r in returns:
        navs.append(navs[-1] * (1.0 + r))
    return navs


# ----- sr_hat + moments -----


def test_sr_hat_computed_by_adapter(tmp_path: Path) -> None:
    returns = [0.012, -0.008, 0.015, 0.004, -0.011, 0.02, 0.006, -0.003, 0.009]
    navs = _navs_from_returns(returns)
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    realized = [navs[i + 1] / navs[i] - 1.0 for i in range(len(navs) - 1)]
    expected_sr = statistics.mean(realized) / statistics.stdev(realized)
    assert result.sr_hat == pytest.approx(expected_sr, rel=1e-9)


def test_psr_and_dsr_in_unit_interval(tmp_path: Path) -> None:
    navs = _navs_from_returns(
        [0.01, 0.005, 0.012, -0.004, 0.008, 0.011, -0.002, 0.009, 0.006]
    )
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    assert result.psr is not None and 0.0 <= result.psr <= 1.0
    assert result.dsr is not None and 0.0 <= result.dsr <= 1.0


# ----- DSR multiple-testing context (record-then-query; H2) -----


def test_single_trial_naive_one_dsr_degenerates_to_psr(tmp_path: Path) -> None:
    """naive_effective_n=1 + one recorded trial -> (1, 0.0) -> dsr ==
    psr(sr_hat, sr_star=0.0). Since the adapter also uses sr_star=0.0 for
    psr, dsr and psr coincide for a single pre-specified run.
    """
    navs = _navs_from_returns([0.01, 0.005, 0.012, -0.004, 0.008, 0.011])
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    assert result.dsr == pytest.approx(result.psr, abs=1e-12)


def test_adapter_records_trial_under_bundle_fingerprint(tmp_path: Path) -> None:
    """dataset_fingerprint = demo.sharadar_bundle; the adapter records the
    run before querying, so the registry holds exactly one row for the key.
    """
    navs = _navs_from_returns([0.01, 0.005, 0.012, -0.004, 0.008, 0.011])
    demo = _demo_from_navs(navs, bundle="sharadar_2026-05-29")
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    # The fingerprint is the bundle name; one trial recorded -> (1, 0.0).
    n_eff, v_sr = registry.effective_n_and_sr_variance(
        "sharadar_2026-05-29", "spy_bh"
    )
    assert n_eff == 1
    assert v_sr == 0.0


def test_multi_trial_family_flows_nonzero_v_sr(tmp_path: Path) -> None:
    """With naive_effective_n=2 the family must already hold a sibling
    trial before a run is adapted (the adapter records-then-queries; a
    lone first trial loud-fails). Pre-seed one sibling, then adapt a run
    so the query sees a >= 2-row variance feeding dsr.
    """
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=2)
    # Pre-seed a sibling trial (as if another strategy in the family was
    # recorded first).
    registry.record(
        dataset_fingerprint="sharadar_2026-05-29",
        strategy_family="fam",
        sr_hat=0.5,
        t_observations=60,
        gamma_3=0.0,
        gamma_4=3.0,
        metadata={},
    )
    demo_b = _demo_from_navs(
        _navs_from_returns([0.02, -0.01, 0.015, 0.006, -0.003, 0.011])
    )
    result_b = to_backtest_result(
        demo_b, registry=registry, strategy_family="fam", universe_id="SPY"
    )
    n_eff, v_sr = registry.effective_n_and_sr_variance("sharadar_2026-05-29", "fam")
    assert n_eff == 2
    assert v_sr > 0.0
    assert result_b.dsr is not None


def test_multi_family_first_trial_loud_fails(tmp_path: Path) -> None:
    """Adapting the FIRST run of a naive_effective_n=2 family raises: a
    run's DSR cannot be computed before its family is complete (the
    documented multi-family discipline).
    """
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=2)
    demo = _demo_from_navs(
        _navs_from_returns([0.01, 0.005, 0.012, -0.004, 0.008, 0.02])
    )
    with pytest.raises(ValueError, match="single trial"):
        to_backtest_result(
            demo, registry=registry, strategy_family="fam", universe_id="SPY"
        )


# ----- min_trl precondition guard (M2) -----


def test_min_trl_none_when_sr_hat_not_above_sr_star(tmp_path: Path) -> None:
    """A losing strategy (negative sr_hat <= sr_star=0) has no finite
    track-record bound; min_trl renders None rather than raising.
    """
    navs = _navs_from_returns([-0.01, -0.005, -0.012, -0.004, -0.008, -0.011])
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="loser", universe_id="SPY"
    )
    assert result.sr_hat < 0.0
    assert result.min_trl is None


def test_min_trl_positive_int_when_sr_hat_above_sr_star(tmp_path: Path) -> None:
    navs = _navs_from_returns(
        [0.02, 0.018, 0.021, 0.019, 0.022, 0.02, 0.017, 0.023]
    )
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="winner", universe_id="SPY"
    )
    assert isinstance(result.min_trl, int)
    assert result.min_trl >= 1


# ----- confidence_tier passthrough -----


def test_confidence_tier_read_from_demo_not_invented(tmp_path: Path) -> None:
    navs = _navs_from_returns([0.01, 0.005, 0.012, -0.004, 0.008, 0.011])
    demo = _demo_from_navs(navs, tier=ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    assert result.confidence_tier == ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED


# ----- domain violations -----


def test_flat_curve_raises(tmp_path: Path) -> None:
    demo = _demo_from_navs([100.0, 100.0, 100.0, 100.0, 100.0])
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    with pytest.raises(ValueError, match="non-flat"):
        to_backtest_result(
            demo, registry=registry, strategy_family="flat", universe_id="SPY"
        )


def test_too_few_returns_raises(tmp_path: Path) -> None:
    demo = _demo_from_navs([100.0, 101.0])  # one return only
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    with pytest.raises(ValueError, match=">= 2 return"):
        to_backtest_result(
            demo, registry=registry, strategy_family="thin", universe_id="SPY"
        )


# ----- scorecard content -----


def test_scorecard_sections_populated(tmp_path: Path) -> None:
    navs = _navs_from_returns([0.01, 0.005, 0.012, -0.004, 0.008, 0.011])
    demo = _demo_from_navs(navs)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    sc = result.scorecard
    assert sc.general.universe_id == "SPY"
    assert sc.general.n_assets == 1
    assert sc.risk_adjusted.sr_hat == result.sr_hat
    assert sc.runs_and_drawdowns.max_drawdown >= 0.0


def test_by_year_attribution_spans_calendar_years(tmp_path: Path) -> None:
    """A curve crossing a year boundary produces a per-year return map."""
    dts = [
        date(2023, 12, 29),
        date(2023, 12, 30),
        date(2023, 12, 31),
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]
    navs = [100.0, 101.0, 102.0, 103.0, 101.0, 104.0]
    demo = _demo_from_navs(navs, dts=dts)
    registry = TrialRegistry(tmp_path / "r.db", naive_effective_n=1)
    result = to_backtest_result(
        demo, registry=registry, strategy_family="spy_bh", universe_id="SPY"
    )
    by_year = result.scorecard.attribution.by_year
    assert set(by_year.keys()) == {2023, 2024}
    # 2023: first nav 100 -> last nav 102 -> +2%
    assert by_year[2023] == pytest.approx(0.02)
    # 2024: first nav 103 -> last nav 104 -> +0.97%
    assert by_year[2024] == pytest.approx(104.0 / 103.0 - 1.0)
