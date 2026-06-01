"""Pull the full survivorship-bias-free S&P 500 bundle for the M5 study.

The M5 worked study runs single-factor JT1993 12-1 momentum on the PIT
S&P 500. The universe is every ticker that was EVER an S&P 500 member
during the study window (including removed and delisted names), so the
pull derives the universe from SHARADAR/SP500 (the membership event log)
and then pulls prices + corporate actions + fundamentals for that union.

Tables pulled into data/snapshots/sharadar_<YYYY-MM-DD>/ (today's date):
  sp500.parquet    SHARADAR/SP500     membership add/remove event log (full)
  tickers.parquet  SHARADAR/TICKERS   identifier history (filtered to universe)
  sep.parquet      SHARADAR/SEP       daily prices (universe, from --start-date)
  actions.parquet  SHARADAR/ACTIONS   dividends + splits (universe, full history)
  sf1.parquet      SHARADAR/SF1 ARQ   as-reported quarterly fundamentals (universe)

Per docs/methodology/total_return_reconstruction.md the engine consumes
the RAW vendor frames (closeunadj + explicit ACTIONS), so the script
writes the vendor columns verbatim; the readers select what they need.

Reads the API key from NASDAQ_DATA_LINK_API_KEY (preferred) or
SHARADAR_API_KEY (legacy); never accepts it on stdin or as an argument so
it cannot leak into shell history.

After the script completes, commit the SHA256 manifest::

    uv run python -m pit_backtest.data.sources.sharadar_pull `
        --bundle sharadar_<YYYY-MM-DD> --refresh-hashes

Requires the dataops optional dependency group (uv sync --extra dataops).
Documented at docs/vendor/nasdaq-data-link-pull.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import polars as pl


# The 12-1 momentum lookback needs ~12 months before the first rebalance,
# so 2004 gives a one-year buffer for a 2005 study start.
DEFAULT_START = "2004-01-01"
SF1_CALENDAR_START = "2003-01-01"
# Tickers per get_table call. ~100 keeps the request URL well under any
# length limit while keeping the batch count low for a ~1000-name universe.
TICKER_BATCH = 100
# Sanity-check tickers that should still be active with recent SEP data
# (the per-ticker freshness check in pull_m1_data.py would false-positive
# on the many legitimately-delisted names in a survivorship-free universe).
ACTIVE_SENTINELS = ("AAPL", "MSFT")
SENTINEL_RECENT_TOLERANCE_DAYS = 10


def _read_api_key() -> str:
    key = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get(
        "SHARADAR_API_KEY"
    )
    if not key:
        print(
            "ERROR: set NASDAQ_DATA_LINK_API_KEY (preferred) or "
            "SHARADAR_API_KEY in your environment first.\n"
            "  [Environment]::SetEnvironmentVariable("
            '"NASDAQ_DATA_LINK_API_KEY", "<key>", "User")\n'
            "then open a new shell (or in this shell:\n"
            '  $env:NASDAQ_DATA_LINK_API_KEY = '
            '[Environment]::GetEnvironmentVariable('
            '"NASDAQ_DATA_LINK_API_KEY","User") ).',
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def _configure(api_key: str) -> object:
    import nasdaqdatalink

    nasdaqdatalink.ApiConfig.api_key = api_key
    return nasdaqdatalink


def _pull_sp500(ndl: object) -> pl.DataFrame:
    """Full SHARADAR/SP500 membership event log (no date filter)."""
    print("pulling SHARADAR/SP500 (full membership event log)")
    df = ndl.get_table("SHARADAR/SP500", paginate=True)  # type: ignore[attr-defined]
    return pl.from_pandas(df)


def _universe_from_sp500(sp500: pl.DataFrame) -> list[str]:
    """Every ticker that ever appears in the membership log (survivor-free)."""
    tickers = sorted(set(sp500["ticker"].drop_nulls().to_list()))
    print(f"  derived universe of {len(tickers)} ever-member tickers")
    return tickers


def _batched(tickers: list[str], size: int) -> list[list[str]]:
    return [tickers[i : i + size] for i in range(0, len(tickers), size)]


def _pull_table_for_universe(
    ndl: object,
    table: str,
    tickers: list[str],
    *,
    extra_filters: dict[str, object] | None = None,
) -> pl.DataFrame:
    """get_table batched over the universe, concatenated.

    Batches the ticker filter to keep request URLs bounded; paginates
    within each batch. Empty batches (tickers with no rows in the table)
    are skipped.
    """
    extra = extra_filters or {}
    frames: list[pl.DataFrame] = []
    batches = _batched(tickers, TICKER_BATCH)
    for i, batch in enumerate(batches, start=1):
        df = ndl.get_table(  # type: ignore[attr-defined]
            table,
            ticker=",".join(batch),
            paginate=True,
            **extra,
        )
        if len(df) > 0:
            frames.append(pl.from_pandas(df))
        print(
            f"  {table}: batch {i}/{len(batches)} "
            f"({len(batch)} tickers) -> {len(df):,} rows"
        )
    if not frames:
        raise ValueError(
            f"{table} returned zero rows across all {len(batches)} batches; "
            f"check the subscription entitlement for {table}."
        )
    return pl.concat(frames, how="vertical_relaxed")


def _coverage(df: pl.DataFrame, label: str) -> None:
    if "date" in df.columns:
        col = "date"
    elif "datekey" in df.columns:
        col = "datekey"
    else:
        print(f"  {label}: {df.height:,} rows (no date column)")
        return
    dts = df[col].cast(pl.Date, strict=False).drop_nulls()
    lo = dts.min()
    hi = dts.max()
    n_tickers = (
        df["ticker"].n_unique() if "ticker" in df.columns else "n/a"
    )
    print(
        f"  {label}: {df.height:,} rows, {n_tickers} tickers, "
        f"{col} in [{lo}, {hi}]"
    )


def _assert_sentinels_recent(
    sep: pl.DataFrame, requested_end: date, bundle_dir: Path
) -> None:
    """Assert the always-active sentinels have SEP data near requested_end.

    A survivorship-free universe legitimately contains many delisted names
    with old max(dt), so a per-ticker freshness check would false-positive.
    Instead we check that a couple of names that are certainly still active
    (AAPL, MSFT) are fresh, which catches a stale or truncated pull.
    """
    from datetime import timedelta

    threshold = requested_end - timedelta(days=SENTINEL_RECENT_TOLERANCE_DAYS)
    present = set(sep["ticker"].unique().to_list())
    for sentinel in ACTIVE_SENTINELS:
        if sentinel not in present:
            print(
                f"  WARNING: sentinel {sentinel} absent from SEP; the "
                f"universe may not include it (check SP500 coverage)."
            )
            continue
        max_dt = (
            sep.filter(pl.col("ticker") == sentinel)["date"]
            .cast(pl.Date, strict=False)
            .max()
        )
        if max_dt is not None and max_dt < threshold:
            raise ValueError(
                f"sentinel {sentinel} max(date)={max_dt} is more than "
                f"{SENTINEL_RECENT_TOLERANCE_DAYS} days before requested end "
                f"{requested_end}; the pull looks stale or truncated. No "
                f"manifest committed; inspect {bundle_dir} before using it."
            )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pull the full survivorship-bias-free S&P 500 bundle (SP500 + "
            "TICKERS + SEP + ACTIONS + SF1) for the M5 momentum study."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date.fromisoformat(DEFAULT_START),
        help="SEP/ACTIONS window start (inclusive, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=date.today(),
        help="SEP/ACTIONS window end (inclusive, YYYY-MM-DD). Default today().",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api_key = _read_api_key()
    if args.start_date >= args.end_date:
        print(
            f"ERROR: --start-date {args.start_date} must be before "
            f"--end-date {args.end_date}.",
            file=sys.stderr,
        )
        return 2

    ndl = _configure(api_key)
    start_str = args.start_date.isoformat()
    end_str = args.end_date.isoformat()

    bundle_name = f"sharadar_{date.today().isoformat()}"
    repo_root = Path(__file__).resolve().parent.parent
    bundle_dir = repo_root / "data" / "snapshots" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    print(f"writing bundle to {bundle_dir}\n")

    sp500 = _pull_sp500(ndl)
    universe = _universe_from_sp500(sp500)

    print("\npulling SHARADAR/TICKERS for the universe")
    tickers = _pull_table_for_universe(ndl, "SHARADAR/TICKERS", universe)
    # SHARADAR/TICKERS carries one row per (security, vendor table): an
    # equity that appears in both SEP (prices) and SF1 (fundamentals) has a
    # table='SEP' row AND a table='SF1' row with identical permaticker +
    # interval. The equity-price identity is the SEP-table row; keeping all
    # tables would give every ticker a duplicate, which the
    # sp500_snapshot_members_resolve_to_unique_ticker data-quality contract
    # (ADR 0017) correctly flags as an n_permatickers > 1 ambiguity. Filter
    # to the SEP identity so each ticker resolves to exactly one row.
    before = tickers.height
    tickers = tickers.filter(pl.col("table") == "SEP")
    print(f"  filtered TICKERS to table='SEP': {before} -> {tickers.height} rows")

    print(f"\npulling SHARADAR/SEP for the universe ({start_str}..{end_str})")
    sep = _pull_table_for_universe(
        ndl,
        "SHARADAR/SEP",
        universe,
        extra_filters={"date": {"gte": start_str, "lte": end_str}},
    )
    _assert_sentinels_recent(sep, args.end_date, bundle_dir)

    print("\npulling SHARADAR/ACTIONS for the universe (full history)")
    actions = _pull_table_for_universe(ndl, "SHARADAR/ACTIONS", universe)

    print(
        f"\npulling SHARADAR/SF1 ARQ for the universe "
        f"(calendardate >= {SF1_CALENDAR_START})"
    )
    sf1 = _pull_table_for_universe(
        ndl,
        "SHARADAR/SF1",
        universe,
        extra_filters={
            "dimension": "ARQ",
            "calendardate": {"gte": SF1_CALENDAR_START},
        },
    )

    print("\nwriting parquet files:")
    for name, frame in (
        ("sp500", sp500),
        ("tickers", tickers),
        ("sep", sep),
        ("actions", actions),
        ("sf1", sf1),
    ):
        path = bundle_dir / f"{name}.parquet"
        frame.write_parquet(path)
        print(f"  wrote {frame.height:,} rows -> {path.name}")

    print("\ncoverage summary:")
    _coverage(sp500, "sp500")
    _coverage(tickers, "tickers")
    _coverage(sep, "sep")
    _coverage(actions, "actions")
    _coverage(sf1, "sf1")

    print(f"\nfull S&P 500 bundle pulled into {bundle_dir}")
    print("Next steps:")
    print(
        f"  1. uv run python -m pit_backtest.data.sources.sharadar_pull "
        f"--bundle {bundle_name} --refresh-hashes"
    )
    print(
        "  2. confirm it loads contract-clean: construct "
        "SharadarDataSource(bundle, snapshots_root) and check the "
        "data-quality contracts pass."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
