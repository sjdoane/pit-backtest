"""Adapter: ConstantWeightDemoResult -> BacktestResult (M4 PR 5).

The engine produces a `ConstantWeightDemoResult` carrying an
`equity_curve` (per `engine/constant_weight_result.py:8-9`). This module
computes the LdP chapter 14 analytics from that curve and assembles the
user-facing `BacktestResult` + `Scorecard`.

Key conventions (verified against `analytics/sharpe.py`):
- `sr_hat` is the PER-PERIOD (non-annualized) Sharpe `mean / stdev` of
  the per-bar returns, matching the units the PSR/DSR formulas expect
  (the `sqrt(T-1)` scaling lives inside `psr`). The `Performance`
  section separately reports ANNUALIZED return + volatility, so the
  per-period `RiskAdjusted.sr_hat` and the annualized `Performance`
  figures are intentionally NOT directly reconcilable.
- `gamma_4` is NON-excess kurtosis (normal = 3), obtained via Polars
  `kurtosis(fisher=False)`; the `sigma_sq` Wald form in `psr` wants the
  non-excess moment. `gamma_3` is the biased third standardized moment
  via Polars `skew()` (the LdP realized-skewness convention).

DSR multiple-testing context (per the M4 PR 4 trial registry): this
adapter RECORDS the run's trial in the registry (so its own SR
contributes to the family's `v_sr` / `n_effective`) BEFORE querying
`effective_n_and_sr_variance`. Adapting the same demo twice records two
trials; callers adapt a result once. For a single pre-specified run
against a registry constructed with `naive_effective_n=1`, the query
returns `(1, 0.0)` and `dsr` degenerates to `psr(sr_hat, sr_star=0.0)`
per ADR 0013 decision 5 (no multiple-testing deflation for one trial).

For `naive_effective_n > 1` (a genuine multiple-testing family), a run's
DSR depends on its sibling trials' cross-sectional variance, so the
family must already hold the sibling trials BEFORE this run is adapted:
after this run's `record`, the family needs `>= 2` trials or
`effective_n_and_sr_variance` loud-fails. This is intentional. You
cannot honestly compute a run's DSR before the family it belongs to is
complete; the loud failure surfaces "record the siblings first" rather
than silently deflating against an incomplete family. The first-of-N
adapt therefore raises, which is the correct discipline for the
multi-family case (an M5 concern; M4's SPY acceptance uses
`naive_effective_n=1`).

dataset_fingerprint policy (the derivation the M4 PR 4 registry deferred
to this PR): v1 uses `demo.sharadar_bundle` (the bundle name, e.g.
`sharadar_2026-05-29`) as the registry partition key. It is already the
project's dataset-version identity. A content-hash fingerprint (parquet
checksum) is a v1.1 refinement.
"""

from __future__ import annotations

import math
from decimal import Decimal

import polars as pl

from pit_backtest.analytics.drawdown import (
    drawdown_duration_report,
    max_drawdown,
)
from pit_backtest.analytics.scorecard import (
    Attribution,
    BacktestResult,
    GeneralCharacteristics,
    ImplementationShortfall,
    Performance,
    RiskAdjusted,
    RunsAndDrawdowns,
    Scorecard,
)
from pit_backtest.analytics.sharpe import dsr, min_trl, psr
from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.trial_registry import TrialRegistry


def _zero_implementation_shortfall() -> ImplementationShortfall:
    """All-zero shortfall (the M1 constant-weight demo is zero-cost)."""
    return ImplementationShortfall(
        total_commission=Decimal("0"),
        total_slippage_bps=Decimal("0"),
        total_temporary_impact_bps=Decimal("0"),
        total_permanent_impact_bps=Decimal("0"),
    )


def _longest_runs(returns: list[float]) -> tuple[int, int]:
    """Longest consecutive run of positive returns and of negative returns.

    A zero return breaks BOTH runs (a 0.0 bar is neither winning nor
    losing). Returns (longest_winning_run, longest_losing_run).
    """
    longest_win = longest_lose = 0
    cur_win = cur_lose = 0
    for r in returns:
        if r > 0.0:
            cur_win += 1
            cur_lose = 0
        elif r < 0.0:
            cur_lose += 1
            cur_win = 0
        else:
            cur_win = 0
            cur_lose = 0
        longest_win = max(longest_win, cur_win)
        longest_lose = max(longest_lose, cur_lose)
    return longest_win, longest_lose


def _by_year_returns(equity_curve: pl.DataFrame) -> dict[int, float]:
    """Calendar-year return (year-end nav / year-start nav - 1) per year.

    Sorted by year for deterministic rendering. The first observed nav of
    a calendar year is the denominator; the last is the numerator. These
    are STANDALONE per-year returns: each year uses its own first nav, NOT
    the prior year's close, so the yearly returns intentionally do NOT
    compound to the total return (the turn-of-year move is excluded). This
    is the "each calendar year stands alone" convention.
    """
    with_year = equity_curve.sort("dt").with_columns(
        pl.col("dt").dt.year().alias("year")
    )
    by_year: dict[int, float] = {}
    for year in sorted(with_year["year"].unique().to_list()):
        year_navs = with_year.filter(pl.col("year") == year)["nav"]
        first = float(year_navs[0])
        last = float(year_navs[-1])
        by_year[int(year)] = last / first - 1.0
    return by_year


def to_backtest_result(
    demo: ConstantWeightDemoResult,
    *,
    registry: TrialRegistry,
    strategy_family: str,
    universe_id: str,
    implementation_shortfall: ImplementationShortfall | None = None,
    sr_star: float = 0.0,
    alpha: float = 0.05,
    periods_per_year: int = 252,
) -> BacktestResult:
    """Compute the LdP scorecard analytics and assemble a BacktestResult.

    Records the run's trial in `registry` then queries the family's
    `(n_effective, v_sr)` for the DSR (see the module docstring). The
    `confidence_tier` is read from `demo.confidence_tier`, not invented.

    Raises:
      ValueError: when the equity curve has fewer than two returns, when
        the return series is flat (zero variance makes `sr_hat = 0/0`),
        or when the realized moments are undefined for the sample size.
        Loud-fail per ADR 0013 decision 7.
    """
    equity_curve = demo.equity_curve.sort("dt")
    returns_series = equity_curve["nav"].pct_change().drop_nulls()
    returns = [float(r) for r in returns_series.to_list()]
    t_obs = len(returns)
    if t_obs < 2:
        raise ValueError(
            f"to_backtest_result requires >= 2 return observations; the "
            f"equity curve produced {t_obs}"
        )
    mean_r = returns_series.mean()
    std_r = returns_series.std()  # ddof=1
    # Polars Series.mean()/.std() are typed `float | timedelta | None`;
    # narrow to float for the numeric series (pct_change of nav).
    if not isinstance(mean_r, float) or not isinstance(std_r, float):
        raise ValueError(
            "to_backtest_result expected numeric returns; the equity "
            "curve nav column did not yield float returns"
        )
    if std_r == 0.0:
        raise ValueError(
            "to_backtest_result requires a non-flat equity curve; the "
            "return series has zero variance so sr_hat = 0/0 is undefined"
        )
    sr_hat = mean_r / std_r

    gamma_3 = returns_series.skew()
    gamma_4 = returns_series.kurtosis(fisher=False)  # non-excess (normal=3)
    if gamma_3 is None or gamma_4 is None:
        raise ValueError(
            f"to_backtest_result could not compute realized skewness or "
            f"kurtosis from {t_obs} returns; the moments are undefined for "
            f"this sample"
        )
    gamma_3 = float(gamma_3)
    gamma_4 = float(gamma_4)

    # Record-then-query so this run's SR is in the registry before the
    # DSR multiple-testing context is read (Plan-reviewer High 2).
    dataset_fingerprint = demo.sharadar_bundle
    registry.record(
        dataset_fingerprint=dataset_fingerprint,
        strategy_family=strategy_family,
        sr_hat=sr_hat,
        t_observations=t_obs,
        gamma_3=gamma_3,
        gamma_4=gamma_4,
        metadata={
            "universe_id": universe_id,
            "start_dt": str(demo.start_dt),
            "end_dt": str(demo.end_dt),
            "n_trading_days": demo.n_trading_days,
        },
    )
    n_effective, v_sr = registry.effective_n_and_sr_variance(
        dataset_fingerprint, strategy_family
    )

    psr_val = psr(sr_hat, sr_star, t_obs, gamma_3, gamma_4)
    dsr_val = dsr(sr_hat, t_obs, gamma_3, gamma_4, v_sr, n_effective)
    # Precondition guard before min_trl rather than a broad except: min_trl
    # raises on sr_hat <= sr_star (no finite track-record bound), but also
    # on bad alpha or sigma_sq <= 0, which must propagate loudly
    # (Plan-reviewer Medium 2).
    min_trl_val: int | None
    if sr_hat <= sr_star:
        min_trl_val = None
    else:
        min_trl_val = math.ceil(
            min_trl(sr_hat, sr_star, alpha, gamma_3, gamma_4)
        )

    nav_first = float(equity_curve["nav"][0])
    nav_last = float(equity_curve["nav"][-1])
    # n_periods >= 2 is guaranteed: the t_obs < 2 guard above raises on any
    # curve with fewer than 2 returns (height < 3), so this divisor is safe.
    n_periods = equity_curve.height - 1
    total_return = nav_last / nav_first - 1.0
    annualized_return = (nav_last / nav_first) ** (
        periods_per_year / n_periods
    ) - 1.0
    annualized_volatility = std_r * math.sqrt(periods_per_year)

    longest_win, longest_lose = _longest_runs(returns)

    scorecard = Scorecard(
        general=GeneralCharacteristics(
            n_trading_days=demo.n_trading_days,
            n_assets=len(demo.tickers),
            universe_id=universe_id,
            start_dt=str(demo.start_dt),
            end_dt=str(demo.end_dt),
        ),
        performance=Performance(
            total_return=total_return,
            annualized_return=annualized_return,
            annualized_volatility=annualized_volatility,
        ),
        runs_and_drawdowns=RunsAndDrawdowns(
            max_drawdown=max_drawdown(equity_curve),
            drawdown_duration=drawdown_duration_report(equity_curve),
            longest_winning_run=longest_win,
            longest_losing_run=longest_lose,
        ),
        implementation_shortfall=(
            implementation_shortfall
            if implementation_shortfall is not None
            else _zero_implementation_shortfall()
        ),
        risk_adjusted=RiskAdjusted(
            sr_hat=sr_hat,
            psr=psr_val,
            dsr=dsr_val,
            min_trl=min_trl_val,
        ),
        attribution=Attribution(by_year=_by_year_returns(equity_curve)),
    )

    return BacktestResult(
        sr_hat=sr_hat,
        psr=psr_val,
        dsr=dsr_val,
        min_trl=min_trl_val,
        confidence_tier=demo.confidence_tier,
        scorecard=scorecard,
    )
