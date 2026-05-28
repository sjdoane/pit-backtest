"""Signal layer: cross-sectional signal computation with strict PIT discipline.

Locked by ADR 0003 decision 5 (Signal.compute returns dict[AssetId, float])
and the pit_view strict-less-than contract (available_dt < dt).
"""
