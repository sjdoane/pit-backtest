"""Lookahead gate tests (M3 PR 1).

Per project rule 2D, PIT data work must ship lookahead-leak tests. This
file covers the LookaheadLeakError + assert_not_lookahead helper. The
canonical usage pattern documented here is what subsequent M3 PRs reuse
when wiring per-row PitDataSource methods (get_fundamental,
get_corporate_actions, etc.) so each new adapter call gates on
`available_dt <= simulation_dt` at its entry.

Pattern that future M3 PRs reuse:

    def test_<method>_raises_on_future_available_dt():
        ...
        with pytest.raises(LookaheadLeakError):
            adapter.<method>(asset_id, available_dt=future, simulation_dt=now)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pit_backtest.data.contracts import (
    LookaheadLeakError,
    assert_not_lookahead,
)


def test_assert_not_lookahead_allows_equal_dates() -> None:
    """available_dt == simulation_dt is the borderline allowed case.

    Per ADR 0001 decision 9 the gate is `available_dt <= simulation_dt`;
    equal dates pass (the record became observable at the same moment
    the simulation is asking for it).
    """
    dt = datetime(2024, 3, 15, 16, 0)
    assert_not_lookahead(dt, dt, context="test_equal")


def test_assert_not_lookahead_allows_past_available_dt() -> None:
    """available_dt strictly in the past returns None (no raise)."""
    available = datetime(2024, 3, 14, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    assert_not_lookahead(available, simulation, context="test_past")


def test_assert_not_lookahead_raises_on_future_available_dt() -> None:
    """available_dt strictly in the future raises LookaheadLeakError."""
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(available, simulation, context="test_future")
    message = str(exc_info.value)
    assert "lookahead leak" in message
    assert "2024-03-16T16:00:00" in message
    assert "2024-03-15T16:00:00" in message
    assert "test_future" in message


def test_lookahead_leak_error_is_value_error() -> None:
    """Callers can broad-catch ValueError when wrapping a pit_view read."""
    assert issubclass(LookaheadLeakError, ValueError)


def test_assert_not_lookahead_message_contains_context_for_diagnostics() -> None:
    """The context string surfaces verbatim so a debug session has the call
    site without a stack trace.
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    context = "SharadarDataSource.get_fundamental(asset=42, field='revenue')"
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(available, simulation, context=context)
    assert context in str(exc_info.value)


def test_assert_not_lookahead_message_includes_period_end_dt_when_provided() -> None:
    """Per ADR 0001 decision 9 the dual-timestamp pair is
    (period_end_dt, available_dt). When the caller provides period_end_dt
    the helper surfaces it so a future debug session has both halves
    without re-reading the source frame.
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    period_end = datetime(2023, 12, 31, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(
            available,
            simulation,
            context="test_with_period_end",
            period_end_dt=period_end,
        )
    message = str(exc_info.value)
    assert "period_end_dt=2023-12-31T16:00:00" in message


def test_assert_not_lookahead_period_end_dt_optional_omits_when_absent() -> None:
    """When period_end_dt is None the message does not mention it; this
    preserves the M3 PR 1 standalone-helper contract for callers that
    do not have period_end_dt at the call site (e.g., get_price reads).
    """
    available = datetime(2024, 3, 16, 16, 0)
    simulation = datetime(2024, 3, 15, 16, 0)
    with pytest.raises(LookaheadLeakError) as exc_info:
        assert_not_lookahead(
            available, simulation, context="test_no_period_end"
        )
    assert "period_end_dt" not in str(exc_info.value)
