"""Data sources: vendor adapters that load parquet snapshots into typed records.

Per ADR 0001 decision 10, the v1 inventory is Sharadar SF1 ARQ + SEP + TICKERS
+ SP500. Per ADR 0003 decision 9, PitDataSource exposes get_table as the
forward-compatibility seam for v1.1 alternative-data adapters.
"""
