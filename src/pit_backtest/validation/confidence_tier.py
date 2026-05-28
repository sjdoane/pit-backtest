"""ConfidenceTier enum.

Per ADR 0001 decision 4 and ADR 0003 decision: the render-path
enforcement on BacktestResult uses this enum to decide whether raw
SR alone is permissible. See analytics/scorecard.py for the validator.
"""

from __future__ import annotations

from enum import Enum


class ConfidenceTier(Enum):
    """Confidence label attached to every BacktestResult.

    SINGLE_RUN_PRE_SPECIFIED: one pre-registered backtest; raw SR alone is
        the most you can honestly compute, so the render path allows it.
    WALK_FORWARD_VALIDATED: walk-forward result; PSR is the meaningful
        statistic.
    CPCV_WITH_DSR_CORRECTION: CPCV with DSR computed against the trial
        registry; the strongest claim available.
    SWEEP_SELECTED_NO_CORRECTION: pulled from a sweep without DSR; carries
        the deflation warning by default and is not eligible for raw SR
        rendering.
    """

    SINGLE_RUN_PRE_SPECIFIED = "single_run_pre_specified"
    WALK_FORWARD_VALIDATED = "walk_forward_validated"
    CPCV_WITH_DSR_CORRECTION = "cpcv_with_dsr_correction"
    SWEEP_SELECTED_NO_CORRECTION = "sweep_selected_no_correction"
