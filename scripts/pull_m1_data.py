"""Pull Sharadar SEP + ACTIONS for SPY, AGG, GLD over a configurable window.

Per ADR 0006 the script accepts --start-date and --end-date so Sam can
re-pull through SSGA's most recent as_of_date (e.g., 2026-04-30) to
exercise the trailing-period kill gate. The pre-ADR-0006 behavior is
preserved when no flags are passed; default window is 2005-01-01 to
today().

Writes parquet files under data/snapshots/sharadar_<YYYY-MM-DD>/ where
YYYY-MM-DD is today's date (the pull date, not the data end date).
Reads the API key from NASDAQ_DATA_LINK_API_KEY (preferred) or
SHARADAR_API_KEY (legacy) env var; never accepts the key on stdin or
as an argument so it cannot leak into shell history.

After the script completes, run::

    uv run python -m pit_backtest.data.sources.sharadar_pull `
        --bundle sharadar_<YYYY-MM-DD> --refresh-hashes

to commit the SHA256 manifest entries.

Per ADR 0006 the script asserts the pulled SEP frame's max(dt) is at
least end_date - 10 calendar days (about 5 trading days plus weekends).
Same-day pulls and intra-day reruns can otherwise produce a bundle
whose actual coverage is earlier than the requested end_date without
warning, which would cause the kill-gate's trailing-period coverage
check to silently SKIP the most recent windows.

Requires the dataops optional dependency group::

    uv sync --extra dataops

Documented at docs/vendor/nasdaq-data-link-pull.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl


# M1 demo universe per ADR 0002 acceptance criteria. The constant-weight
# demo locks these three tickers; the SPY reconciliation uses just SPY.
M1_TICKERS = ["SPY", "AGG", "GLD"]
M1_DEFAULT_START = "2005-01-01"
# ADR 0006 same-day-pull guard: after pulling, assert the SEP frame's
# max(dt) is at least end_date - this many calendar days. 10 covers
# the worst-case weekend + one holiday and is comfortably wider than
# typical Sharadar publication latency.
PULL_RANGE_TOLERANCE_DAYS = 10


def _read_api_key() -> str:
    """Resolve the Nasdaq Data Link API key from environment.

    The SDK's own convention is NASDAQ_DATA_LINK_API_KEY; the project's
    sharadar_pull.py legacy variable is SHARADAR_API_KEY. Both work;
    NASDAQ_DATA_LINK_API_KEY wins when both are set.
    """
    key = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get(
        "SHARADAR_API_KEY"
    )
    if not key:
        print(
            "ERROR: Set NASDAQ_DATA_LINK_API_KEY (preferred) or "
            "SHARADAR_API_KEY in your environment before running.\n"
            "  [Environment]::SetEnvironmentVariable("
            '"NASDAQ_DATA_LINK_API_KEY", "<key>", "User")\n'
            "then open a new PowerShell window.",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def _pull_sep(api_key: str, start: str, end: str) -> pl.DataFrame:
    """Pull SHARADAR/SEP filtered to the M1 tickers and window.

    SEP is the equity prices table; SFP is the fund prices table. The
    current Sharadar Premium bundle exposes ETFs through both; if SEP
    returns no rows, fall back to SFP.
    """
    import nasdaqdatalink

    nasdaqdatalink.ApiConfig.api_key = api_key

    print(f"pulling SHARADAR/SEP for {M1_TICKERS} from {start} to {end}")
    sep_df = nasdaqdatalink.get_table(
        "SHARADAR/SEP",
        ticker=",".join(M1_TICKERS),
        date={"gte": start, "lte": end},
        paginate=True,
    )
    if len(sep_df) == 0:
        print("  SEP returned empty; falling back to SHARADAR/SFP (fund prices)")
        sep_df = nasdaqdatalink.get_table(
            "SHARADAR/SFP",
            ticker=",".join(M1_TICKERS),
            date={"gte": start, "lte": end},
            paginate=True,
        )
    return pl.from_pandas(sep_df)


def _pull_actions(api_key: str, start: str, end: str) -> pl.DataFrame:
    """Pull SHARADAR/ACTIONS filtered to the M1 tickers and window.

    Covers dividends (action == "dividend"), splits (action == "split"),
    and other corporate events. The engine filters to dividends at
    SharadarDataSource.read_actions_dividends.
    """
    import nasdaqdatalink

    nasdaqdatalink.ApiConfig.api_key = api_key

    print(f"pulling SHARADAR/ACTIONS for {M1_TICKERS} from {start} to {end}")
    actions_df = nasdaqdatalink.get_table(
        "SHARADAR/ACTIONS",
        ticker=",".join(M1_TICKERS),
        date={"gte": start, "lte": end},
        paginate=True,
    )
    return pl.from_pandas(actions_df)


def _coerce_to_date(value: object) -> date:
    """Coerce a Polars max() result to a datetime.date.

    Polars can return `datetime.date` (pl.Date), `datetime.datetime`
    (pl.Datetime), or an ISO-8601 string depending on the dtype.
    """
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(
        f"unexpected max(dt) value type {type(value).__name__}: {value!r}"
    )


def _assert_pulled_range_covers(
    sep_df: pl.DataFrame,
    requested_end: date,
    bundle_dir: Path,
) -> None:
    """Raise if any ticker's max(dt) is too far from requested_end.

    Per ADR 0006: same-day pulls or intra-day re-runs can produce a
    bundle whose actual max(dt) is earlier than the requested end_date
    (Sharadar publishes end-of-day data with a one-day lag; pulling
    today() returns data through today() - 1 at best, often earlier).
    The kill gate would otherwise silently SKIP the most recent windows
    because the coverage check would see a stale max(dt).

    The check is per-ticker because Sharadar can publish one M1 ticker
    earlier than another in the publication-lag window. An aggregate
    max(dt) would silently pass with the most recently published
    ticker's data even when SPY itself is stale.
    """
    if sep_df.height == 0:
        raise ValueError(
            f"pulled SEP frame is empty for tickers {M1_TICKERS}; "
            f"check the API key and the requested window. "
            f"No files have been written; the bundle directory at "
            f"{bundle_dir} was created empty and can be removed."
        )
    threshold = requested_end - timedelta(days=PULL_RANGE_TOLERANCE_DAYS)
    per_ticker = (
        sep_df.group_by("ticker")
        .agg(pl.col("date").max().alias("max_dt"))
        .sort("ticker")
    )
    stale: list[tuple[str, date]] = []
    for row in per_ticker.iter_rows(named=True):
        ticker = row["ticker"]
        max_dt = _coerce_to_date(row["max_dt"])
        if max_dt < threshold:
            stale.append((ticker, max_dt))
    if stale:
        details = "; ".join(f"{t}: max(dt)={d}" for t, d in stale)
        earliest = min(d for _, d in stale)
        raise ValueError(
            f"per-ticker SEP coverage is more than "
            f"{PULL_RANGE_TOLERANCE_DAYS} calendar days before the requested "
            f"end_date = {requested_end} for the following tickers: "
            f"[{details}]. Sharadar publishes with a one-day lag and can "
            f"publish one M1 ticker earlier than another; same-day pulls "
            f"produce stale bundles. Re-run with --end-date {earliest} or "
            f"wait until Sharadar publishes the requested range. No files "
            f"have been written; the bundle directory at {bundle_dir} was "
            f"created empty and can be removed."
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pull Sharadar SEP + ACTIONS for the M1 universe (SPY, AGG, "
            "GLD) over a configurable window. Per ADR 0006 the default "
            "end_date is today() so subsequent runs cover SSGA's latest "
            "as_of_date for the trailing-period kill gate."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date.fromisoformat(M1_DEFAULT_START),
        help="Window start (inclusive, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=date.today(),
        help=(
            "Window end (inclusive, YYYY-MM-DD). Default today(); pass a "
            "past date to reproduce an older pull. The script asserts "
            "the pulled SEP frame's max(dt) is within "
            f"{PULL_RANGE_TOLERANCE_DAYS} calendar days of this value."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api_key = _read_api_key()

    if args.start_date >= args.end_date:
        print(
            f"ERROR: --start-date {args.start_date} must be strictly before "
            f"--end-date {args.end_date}.",
            file=sys.stderr,
        )
        return 2

    start_str = args.start_date.isoformat()
    end_str = args.end_date.isoformat()

    bundle_name = f"sharadar_{date.today().isoformat()}"
    repo_root = Path(__file__).resolve().parent.parent
    bundle_dir = repo_root / "data" / "snapshots" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    sep = _pull_sep(api_key, start_str, end_str)
    _assert_pulled_range_covers(sep, args.end_date, bundle_dir)
    sep_path = bundle_dir / "sep.parquet"
    sep.write_parquet(sep_path)
    print(f"  wrote {sep.height:,} rows to {sep_path}")

    actions = _pull_actions(api_key, start_str, end_str)
    actions_path = bundle_dir / "actions.parquet"
    actions.write_parquet(actions_path)
    print(f"  wrote {actions.height:,} rows to {actions_path}")

    print()
    print(f"M1 data pulled into {bundle_dir}")
    print(f"  window: {start_str} to {end_str}")
    print()
    print("Next steps:")
    print(f"  1. uv run python -m pit_backtest.data.sources.sharadar_pull \\")
    print(f"         --bundle {bundle_name} --refresh-hashes")
    print(f"  2. From the SSGA SPY fund page Document section, download")
    print(f"     spdr-etf-historical-distributions.xlsx and")
    print(f"     spdr-product-data-us-en.xlsx (do NOT rename them)")
    print(f"     into data/snapshots/spy_ssga_{date.today().isoformat()}/")
    print(f"     Page: https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-spy")
    print(f"  3. uv run python -m pit_backtest.data.sources.sharadar_pull \\")
    print(f"         --bundle spy_ssga_{date.today().isoformat()} --refresh-hashes")
    print(f"  4. uv run python -m examples.spy_buy_and_hold --compare-to-ssga")
    print(f"  5. uv run python -m examples.constant_weight_three_names "
          f"--diff-against-reference")
    return 0


if __name__ == "__main__":
    sys.exit(main())
