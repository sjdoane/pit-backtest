"""Performance benchmark harness for the M2 cost-realism path.

Per ADR 0005 step 16 PR D and ADR 0012, the bench package ships the
SPY 20-year synthetic backtest harness, the median-of-N timing
collector, and the regression-budget comparison tool. The harness
runs on synthetic data per ADR 0005 final lock #11 (the
docs/methodology/dataset_versioning.md CI gap means real Sharadar
data is unavailable in CI).
"""
