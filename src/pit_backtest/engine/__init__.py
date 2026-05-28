"""Engine: BarLoop driver, Runner orchestrator, PortfolioState, Backtest entry.

Per ADR 0003: single-process sequential BarLoop; multiprocess Runner for
CPCV paths and parameter sweeps with per-worker POLARS_MAX_THREADS=1 for
determinism.
"""
