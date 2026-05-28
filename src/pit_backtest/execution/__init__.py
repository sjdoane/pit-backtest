"""Execution layer: orders, fills, matching, clock, cost model.

Locked by ADR 0003 decisions 4 (cost protocols split), 6 (MatchingEngine.submit
returns list[Fill]), 7 (Clock includes is_market_open and next_bar), 14 (slippage
vs impact split via CostBreakdown).
"""
