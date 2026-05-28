"""Validation layer: CV splitters, trial registry, confidence-tier enforcement.

Per ADR 0001 decision 3, CPCV is primary; walk-forward is a CPCV configuration
with one path. Per ADR 0003 decision 17, WalkForwardSplitter ships alongside
CPCVSplitter as a sanity-check baseline.
"""
