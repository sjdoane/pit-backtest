"""Pull Sharadar SEP + ACTIONS for SPY, AGG, GLD over the M1 window.

Writes parquet files under data/snapshots/sharadar_<YYYY-MM-DD>/.
Reads the API key from NASDAQ_DATA_LINK_API_KEY (preferred) or
SHARADAR_API_KEY (legacy) env var; never accepts the key on stdin or
as an argument so it cannot leak into shell history.

After this script completes, run:

    uv run python -m pit_backtest.data.sources.sharadar_pull \
        --bundle sharadar_<YYYY-MM-DD> --refresh-hashes

to commit the SHA256 manifest entries.

Requires the dataops optional dependency group:

    uv sync --extra dataops

Documented at docs/vendor/nasdaq-data-link-pull.md.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import polars as pl


# M1 demo universe per ADR 0002 acceptance criteria. The constant-weight
# demo locks these three tickers; the SPY reconciliation uses just SPY.
M1_TICKERS = ["SPY", "AGG", "GLD"]
M1_START = "2005-01-01"
M1_END = "2024-12-31"


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


def _pull_sep(api_key: str) -> pl.DataFrame:
    """Pull SHARADAR/SEP filtered to the M1 tickers and window.

    SEP is the equity prices table; SFP is the fund prices table. The
    current Sharadar Premium bundle exposes ETFs through both; if SEP
    returns no rows, fall back to SFP.
    """
    import nasdaqdatalink

    nasdaqdatalink.ApiConfig.api_key = api_key

    print(f"pulling SHARADAR/SEP for {M1_TICKERS} from {M1_START} to {M1_END}")
    sep_df = nasdaqdatalink.get_table(
        "SHARADAR/SEP",
        ticker=",".join(M1_TICKERS),
        date={"gte": M1_START, "lte": M1_END},
        paginate=True,
    )
    if len(sep_df) == 0:
        print("  SEP returned empty; falling back to SHARADAR/SFP (fund prices)")
        sep_df = nasdaqdatalink.get_table(
            "SHARADAR/SFP",
            ticker=",".join(M1_TICKERS),
            date={"gte": M1_START, "lte": M1_END},
            paginate=True,
        )
    return pl.from_pandas(sep_df)


def _pull_actions(api_key: str) -> pl.DataFrame:
    """Pull SHARADAR/ACTIONS filtered to the M1 tickers and window.

    Covers dividends (action == "dividend"), splits (action == "split"),
    and other corporate events. The engine filters to dividends at
    SharadarDataSource.read_actions_dividends.
    """
    import nasdaqdatalink

    nasdaqdatalink.ApiConfig.api_key = api_key

    print(f"pulling SHARADAR/ACTIONS for {M1_TICKERS} from {M1_START} to {M1_END}")
    actions_df = nasdaqdatalink.get_table(
        "SHARADAR/ACTIONS",
        ticker=",".join(M1_TICKERS),
        date={"gte": M1_START, "lte": M1_END},
        paginate=True,
    )
    return pl.from_pandas(actions_df)


def main() -> int:
    api_key = _read_api_key()

    bundle_name = f"sharadar_{date.today().isoformat()}"
    repo_root = Path(__file__).resolve().parent.parent
    bundle_dir = repo_root / "data" / "snapshots" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    sep = _pull_sep(api_key)
    sep_path = bundle_dir / "sep.parquet"
    sep.write_parquet(sep_path)
    print(f"  wrote {sep.height:,} rows to {sep_path}")

    actions = _pull_actions(api_key)
    actions_path = bundle_dir / "actions.parquet"
    actions.write_parquet(actions_path)
    print(f"  wrote {actions.height:,} rows to {actions_path}")

    print()
    print(f"M1 data pulled into {bundle_dir}")
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
