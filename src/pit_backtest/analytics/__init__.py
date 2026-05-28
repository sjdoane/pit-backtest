"""Analytics layer: LdP chapter 14 scorecard.

PSR, DSR, MinTRL, HHI, drawdown, and the Markdown scorecard renderer. Per
ADR 0001 decision 4, raw Sharpe shown alone is a configuration error; the
render path enforces this via the ConfidenceTier check on BacktestResult.
"""
