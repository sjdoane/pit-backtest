"""signal_calendar gate tests (M5 PR 3a).

The gate (BarLoop.signal_calendar) is the tractability prerequisite for the real
S&P 500 momentum study: it fires signal.compute (and the per-bar PitView rebuild)
only on calendar bars, skipping the ~95% of bars where the rebalance policy
no-ops. These tests prove it is behavior-preserving (byte-identical equity curve
gate-on vs gate-off), that it actually reduces the compute count to the calendar
bars, and that the load-bearing contract (signal_calendar must cover the policy's
trade calendar) fails loudly when violated.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.engine._cpcv_momentum_factory import (
    MomentumWindowFactory,
    momentum_rebalance_dates,
    write_momentum_bundle,
)
from tests.engine._signal_gate_factory import (
    CountingSignal,
    build_gate_bar_loop,
    gate_asset_ids,
    gate_rebalance_dates,
    write_gate_bundle,
)


def test_signal_gate_is_byte_identical(tmp_path: Path) -> None:
    """gate-on (signal_calendar = the policy's rebalance calendar) produces a
    byte-identical equity curve to gate-off (None, compute every bar). This is
    the behavior-preserving guarantee: the policy no-ops off its calendar, so
    the gated (stale) signal_output is never consumed by an order.
    """
    root, start, end = write_gate_bundle(tmp_path)
    rebals = gate_rebalance_dates(start, end)
    asset_ids = gate_asset_ids()

    ungated = build_gate_bar_loop(
        root, start, end, signal=CountingSignal(asset_ids), signal_calendar=None
    ).run(start_dt=start, end_dt=end)
    gated = build_gate_bar_loop(
        root, start, end, signal=CountingSignal(asset_ids), signal_calendar=rebals
    ).run(start_dt=start, end_dt=end)

    assert gated.equity_curve.equals(ungated.equity_curve)
    assert gated.final_nav == ungated.final_nav
    assert gated.final_pnl == ungated.final_pnl
    assert gated.n_rebalances == ungated.n_rebalances


def test_signal_gate_computes_only_on_calendar_bars(tmp_path: Path) -> None:
    """Gated, signal.compute fires exactly on the calendar bars; ungated, it
    fires on every trading day. This is the structural proof of the speedup.
    """
    root, start, end = write_gate_bundle(tmp_path)
    rebals = gate_rebalance_dates(start, end)
    asset_ids = gate_asset_ids()
    assert len(rebals) >= 2  # the window spans several monthly rebalances

    spy_gated = CountingSignal(asset_ids)
    build_gate_bar_loop(
        root, start, end, signal=spy_gated, signal_calendar=rebals
    ).run(start_dt=start, end_dt=end)
    assert spy_gated.calls == len(rebals)
    assert set(spy_gated.call_dts) == set(rebals)

    spy_ungated = CountingSignal(asset_ids)
    result = build_gate_bar_loop(
        root, start, end, signal=spy_ungated, signal_calendar=None
    ).run(start_dt=start, end_dt=end)
    assert spy_ungated.calls == result.n_trading_days
    assert spy_ungated.calls > len(rebals)  # the gate is a genuine reduction


def test_signal_gate_raises_when_policy_trades_off_calendar(tmp_path: Path) -> None:
    """The load-bearing contract (H1): signal_calendar MUST cover every bar the
    policy can trade on. If it omits a policy rebalance date, the gate skips the
    signal that would drive that trade and the curve would be silently wrong;
    run() must raise loudly instead. Dropping the SECOND rebalance (the first
    still computes a non-empty signal, so the dropped second trades on the stale
    one) triggers the guard.
    """
    root, start, end = write_gate_bundle(tmp_path)
    rebals = sorted(gate_rebalance_dates(start, end))
    assert len(rebals) >= 2
    deficient = frozenset(rebals) - {rebals[1]}
    asset_ids = gate_asset_ids()

    bar_loop = build_gate_bar_loop(
        root, start, end, signal=CountingSignal(asset_ids), signal_calendar=deficient
    )
    with pytest.raises(RuntimeError, match="not in signal_calendar"):
        bar_loop.run(start_dt=start, end_dt=end)


def test_signal_gate_byte_identical_with_real_momentum(tmp_path: Path) -> None:
    """The load-bearing behavior-preservation test on the ACTUAL study strategy:
    the real Momentum12_1Signal (pure function of dt + the PitView, not of call
    count) + the value-sensitive TopQuintileLongPolicy + use_real_pit_view=True.
    Gated vs ungated must be byte-identical: because the signal is pure in dt,
    gated and ungated compute the SAME score at each rebalance bar, and the
    policy no-ops off-calendar, so the gate cannot move the curve. This is the
    representative test the constant-spy case only proves incidentally, and it
    de-risks the PR 3b gated CPCV/study factories.
    """
    root = write_momentum_bundle(tmp_path)
    rebals = momentum_rebalance_dates()
    start, end = date(2011, 1, 3), rebals[-1]

    ungated = (
        MomentumWindowFactory(str(root), rebals, gate=False)(start, end)
        .run(start_dt=start, end_dt=end)
    )
    gated = (
        MomentumWindowFactory(str(root), rebals, gate=True)(start, end)
        .run(start_dt=start, end_dt=end)
    )

    assert gated.n_rebalances == ungated.n_rebalances >= 1
    assert gated.final_nav == ungated.final_nav
    assert gated.equity_curve.equals(ungated.equity_curve)
