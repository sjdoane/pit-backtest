"""Quick diagnostic of what's actually in sharadar_2026-05-29/sep.parquet."""

from __future__ import annotations

import polars as pl


def main() -> None:
    df = pl.read_parquet("data/snapshots/sharadar_2026-05-29/sep.parquet")
    print(f"Total rows: {df.height}")
    print(f"Schema 'date' dtype: {df.schema['date']}")
    print()
    print("Per-ticker counts and date ranges:")
    grouped = (
        df.group_by("ticker")
        .agg(
            pl.len().alias("n_rows"),
            pl.col("date").min().alias("first_dt"),
            pl.col("date").max().alias("last_dt"),
        )
        .sort("ticker")
    )
    for row in grouped.iter_rows(named=True):
        print(
            f"  {row['ticker']}: {row['n_rows']:>6} rows, "
            f"{row['first_dt']} to {row['last_dt']}"
        )
    print()
    print("SPY first 3 rows:")
    spy = df.filter(pl.col("ticker") == "SPY").sort("date")
    print(spy.head(3).select("ticker", "date", "closeunadj"))
    print()
    print("SPY last 3 rows:")
    print(spy.tail(3).select("ticker", "date", "closeunadj"))


if __name__ == "__main__":
    main()
