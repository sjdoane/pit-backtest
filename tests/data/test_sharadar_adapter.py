"""SharadarDataSource tests against a synthetic mini-snapshot.

Writes a tiny SEP + ACTIONS parquet bundle under tmp_path, registers it
in a manifest, constructs the adapter, and verifies the M1 convenience
methods plus the end-to-end TR reconstruction flow.

No real Sharadar data required; the test runs in CI.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from pit_backtest.data.adjustments import reconstruct_total_return
from pit_backtest.data.sources.sharadar import SharadarDataSource


# Synthetic SPY rows for the test. Prices are illustrative; the dividend
# value mirrors the docs/methodology/total_return_reconstruction.md Worked
# Example B (SPY Q1 2024 ex-dividend $1.7715 on 2024-03-15). The non-SPY
# row exercises the per-ticker filter.
_SEP_ROWS = [
    # SPY rows
    {"ticker": "SPY", "date": date(2024, 3, 13), "open": 515.00, "high": 518.00, "low": 514.00, "close": 517.51, "closeunadj": 517.51, "volume": 80_000_000},
    {"ticker": "SPY", "date": date(2024, 3, 14), "open": 517.00, "high": 518.50, "low": 516.50, "close": 517.51, "closeunadj": 517.51, "volume": 70_000_000},
    {"ticker": "SPY", "date": date(2024, 3, 15), "open": 517.95, "high": 518.43, "low": 510.27, "close": 512.85, "closeunadj": 512.85, "volume": 92_750_000},
    {"ticker": "SPY", "date": date(2024, 3, 18), "open": 513.00, "high": 515.00, "low": 511.00, "close": 514.00, "closeunadj": 514.00, "volume": 60_000_000},
    # Non-SPY row to exercise the filter
    {"ticker": "AGG", "date": date(2024, 3, 15), "open": 95.00, "high": 95.50, "low": 94.80, "close": 95.20, "closeunadj": 95.20, "volume": 5_000_000},
]

_ACTIONS_ROWS = [
    {"ticker": "SPY", "date": date(2024, 3, 15), "action": "dividend", "value": 1.7715},
    {"ticker": "SPY", "date": date(2023, 12, 15), "action": "dividend", "value": 1.5800},
    {"ticker": "AGG", "date": date(2024, 3, 1), "action": "dividend", "value": 0.2800},
    # Non-dividend action that should be filtered out by read_actions_dividends
    {"ticker": "SPY", "date": date(2024, 3, 15), "action": "split", "value": 1.0},
]

# M3 PR 1: synthetic TICKERS rows covering the resolver edge cases:
# permaticker=100 SPY active-through-now; permaticker=200 AGG active-through-now;
# permaticker=300 OLDCO delisted 2014-12-31.
_TICKERS_ROWS = [
    {
        "permaticker": 100,
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "exchange": "NYSEARCA",
        "isdelisted": "N",
        "firstpricedate": date(1993, 1, 22),
        "lastpricedate": None,
        "firstquarter": date(1993, 3, 31),
        "lastquarter": date(2026, 3, 31),
        "cusip": "78462F103",
    },
    {
        "permaticker": 200,
        "ticker": "AGG",
        "name": "iShares Core US Aggregate Bond ETF",
        "exchange": "NYSEARCA",
        "isdelisted": "N",
        "firstpricedate": date(2003, 9, 22),
        "lastpricedate": None,
        "firstquarter": date(2003, 9, 30),
        "lastquarter": date(2026, 3, 31),
        "cusip": "464287226",
    },
    {
        "permaticker": 300,
        "ticker": "OLDCO",
        "name": "Old Company Inc",
        "exchange": "NASDAQ",
        "isdelisted": "Y",
        "firstpricedate": date(2010, 1, 4),
        "lastpricedate": date(2014, 12, 31),
        "firstquarter": date(2010, 3, 31),
        "lastquarter": date(2014, 12, 31),
        "cusip": "OLDC00001",
    },
]

# M3 PR 1: synthetic SF1 rows covering ARQ, ART, ARY (PIT) plus MRQ
# (restated, must be rejected). The reader filters by `dimension` column;
# the rejection test passes `dimension="MRQ"` at the read call.
_SF1_ROWS = [
    {
        "ticker": "SPY",
        "dimension": "ARQ",
        "calendardate": date(2024, 3, 31),
        "datekey": date(2024, 4, 15),
        "reportperiod": date(2024, 3, 31),
        "lastupdated": date(2024, 4, 16),
        "revenue": 1000.0,
        "netinc": 100.0,
    },
    {
        "ticker": "SPY",
        "dimension": "ARQ",
        "calendardate": date(2023, 12, 31),
        "datekey": date(2024, 1, 15),
        "reportperiod": date(2023, 12, 31),
        "lastupdated": date(2024, 1, 16),
        "revenue": 950.0,
        "netinc": 95.0,
    },
    {
        "ticker": "SPY",
        "dimension": "ART",
        "calendardate": date(2024, 3, 31),
        "datekey": date(2024, 4, 15),
        "reportperiod": date(2024, 3, 31),
        "lastupdated": date(2024, 4, 16),
        "revenue": 3900.0,
        "netinc": 390.0,
    },
    {
        "ticker": "SPY",
        "dimension": "ARY",
        "calendardate": date(2023, 12, 31),
        "datekey": date(2024, 3, 1),
        "reportperiod": date(2023, 12, 31),
        "lastupdated": date(2024, 3, 2),
        "revenue": 3850.0,
        "netinc": 380.0,
    },
    {
        "ticker": "SPY",
        "dimension": "MRQ",
        "calendardate": date(2024, 3, 31),
        "datekey": date(2024, 4, 15),
        "reportperiod": date(2024, 3, 31),
        "lastupdated": date(2026, 5, 1),
        "revenue": 1005.0,  # restated value; differs from ARQ
        "netinc": 102.0,
    },
]


def _write_synthetic_bundle(
    tmp_path: Path,
    bundle_name: str = "sharadar_2026-05-28",
    tables: tuple[str, ...] = ("sep", "actions"),
) -> Path:
    """Build a synthetic Sharadar bundle and the manifest entry.

    Returns the snapshots_root (the parent of the bundle directory).

    Per Plan-reviewer Medium 8: M1 tests pass the default ("sep","actions")
    so their existing assertions about manifest contents continue to hold;
    M3 tests pass ("sep","actions","tickers","sf1") so the new readers
    have parquet files with manifest-verified SHA256s. The manifest only
    lists files this helper wrote; `verify_bundle` does not check for
    extra files in the bundle dir, so a four-table bundle is a strict
    superset that the two-table M1 tests can still load.
    """
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    table_data: dict[str, list[dict[str, object]]] = {
        "sep": _SEP_ROWS,
        "actions": _ACTIONS_ROWS,
        "tickers": _TICKERS_ROWS,
        "sf1": _SF1_ROWS,
    }

    file_lines: list[str] = []
    for table in tables:
        if table not in table_data:
            raise ValueError(
                f"unknown table {table!r}; available: {sorted(table_data)}"
            )
        rows = table_data[table]
        df = pl.DataFrame(rows)
        path = bundle_dir / f"{table}.parquet"
        df.write_parquet(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        file_lines.append(
            f'"{table}.parquet" = {{ sha256 = "{sha}", '
            f"size_bytes = {size}, row_count = {len(rows)} }}"
        )

    files_block = "\n".join(file_lines)
    manifest_content = f"""
[snapshots.{bundle_name}]
source = "sharadar"
pull_date = 2026-05-28
notes = "synthetic fixture for tests"

[snapshots.{bundle_name}.files]
{files_block}
"""
    (snapshots_root / "manifest.toml").write_text(manifest_content, encoding="utf-8")

    return snapshots_root


def test_adapter_construction_verifies_manifest(tmp_path: Path) -> None:
    """Construction succeeds when SHA256s match; bundle metadata exposed."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    assert adapter.bundle_name == "sharadar_2026-05-28"
    assert adapter.bundle_entry.source == "sharadar"
    assert adapter.bundle_entry.pull_date == date(2026, 5, 28)


def test_adapter_construction_fails_on_tampered_file(tmp_path: Path) -> None:
    """If the parquet on disk differs from the manifest SHA256, construction
    raises SnapshotMismatchError (per dataset_versioning.md).
    """
    from pit_backtest.data.sources.manifest import SnapshotMismatchError

    snapshots_root = _write_synthetic_bundle(tmp_path)
    # Tamper with the SEP parquet after the manifest was written.
    sep_path = snapshots_root / "sharadar_2026-05-28" / "sep.parquet"
    sep_path.write_bytes(b"tampered")

    with pytest.raises(SnapshotMismatchError, match="SHA256 mismatch"):
        SharadarDataSource("sharadar_2026-05-28", snapshots_root)


def test_read_sep_prices_filters_by_ticker(tmp_path: Path) -> None:
    """read_sep_prices returns only the rows for the requested ticker, sorted by dt."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    spy = adapter.read_sep_prices(ticker="SPY")
    assert spy.height == 4  # 4 SPY rows
    assert spy["dt"].to_list() == [
        date(2024, 3, 13),
        date(2024, 3, 14),
        date(2024, 3, 15),
        date(2024, 3, 18),
    ]
    assert spy["close"][2] == pytest.approx(512.85)
    assert spy["closeunadj"][2] == pytest.approx(512.85)

    agg = adapter.read_sep_prices(ticker="AGG")
    assert agg.height == 1
    assert agg["dt"][0] == date(2024, 3, 15)


def test_read_sep_prices_filters_by_date_range(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    rows = adapter.read_sep_prices(
        ticker="SPY", start_dt=date(2024, 3, 14), end_dt=date(2024, 3, 15)
    )
    assert rows["dt"].to_list() == [date(2024, 3, 14), date(2024, 3, 15)]


def test_read_actions_dividends_filters_to_dividend_rows(tmp_path: Path) -> None:
    """Splits and other non-dividend actions are filtered out."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    spy_divs = adapter.read_actions_dividends(ticker="SPY")
    assert spy_divs.height == 2  # 2 dividends; the split row is excluded
    assert spy_divs["ex_date"].to_list() == [date(2023, 12, 15), date(2024, 3, 15)]
    assert spy_divs["amount_per_share"][1] == pytest.approx(1.7715)


def test_read_actions_dividends_date_range_filter(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    rows = adapter.read_actions_dividends(
        ticker="SPY", start_dt=date(2024, 1, 1), end_dt=date(2024, 12, 31)
    )
    assert rows.height == 1
    assert rows["ex_date"][0] == date(2024, 3, 15)


def test_get_table_dispatches_by_name(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    sep_lf = adapter.get_table("sep")
    assert isinstance(sep_lf, pl.LazyFrame)
    actions_lf = adapter.get_table("actions")
    assert isinstance(actions_lf, pl.LazyFrame)

    with pytest.raises(KeyError, match="unknown Sharadar table"):
        adapter.get_table("bogus")


def test_get_table_caches_lazy_frame(tmp_path: Path) -> None:
    """Repeated calls to get_table return the same LazyFrame instance."""
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    lf1 = adapter.get_table("sep")
    lf2 = adapter.get_table("sep")
    assert lf1 is lf2


def test_get_table_raises_on_undeclared_parquet(tmp_path: Path) -> None:
    """If the bundle did not include sf1.parquet, asking for it raises
    FileNotFoundError. The manifest verification at __init__ would catch
    a manifest-declared-but-missing file; this fires for tables the bundle
    legitimately omitted.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)
    with pytest.raises(FileNotFoundError, match="missing sf1.parquet"):
        adapter.get_table("sf1")


def test_end_to_end_tr_reconstruction(tmp_path: Path) -> None:
    """Read SPY prices + dividends from the adapter and reconstruct the TR
    series. With zero expense ratio (synthetic fixture, not real SPY), the
    TR is exactly (close_t + div_t) / close_{t-1} compounded.

    Verifies the full M1 day 1 data path: adapter -> reconstruction -> TR.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    prices = adapter.read_sep_prices(
        ticker="SPY", start_dt=date(2024, 3, 13), end_dt=date(2024, 3, 18)
    ).select(pl.col("dt"), pl.col("closeunadj").alias("close"))

    dividends = adapter.read_actions_dividends(
        ticker="SPY", start_dt=date(2024, 3, 13), end_dt=date(2024, 3, 18)
    )

    tr = reconstruct_total_return(
        prices,
        dividends,
        start_dt=date(2024, 3, 13),
        end_dt=date(2024, 3, 18),
        expense_ratio_annual=Decimal("0"),
    )

    tr_values = tr["tr"].to_list()
    # Day 0 (2024-03-13): reference, TR = 1.0
    assert tr_values[0] == pytest.approx(1.0, abs=1e-12)
    # Day 1 (2024-03-14): price flat at 517.51, multiplier = 1.0
    assert tr_values[1] == pytest.approx(1.0, abs=1e-12)
    # Day 2 (2024-03-15): price 512.85 + dividend 1.7715, multiplier =
    #   (512.85 + 1.7715) / 517.51 = 0.9943808 (approx)
    expected_mult_day2 = (512.85 + 1.7715) / 517.51
    assert tr_values[2] == pytest.approx(expected_mult_day2, abs=1e-10)
    # Day 3 (2024-03-18): price 514.00, multiplier = 514.00 / 512.85
    expected_mult_day3 = 514.00 / 512.85
    assert tr_values[3] == pytest.approx(
        expected_mult_day2 * expected_mult_day3, abs=1e-10
    )


def test_read_sep_prices_returns_pl_date_column_even_from_datetime_source(
    tmp_path: Path,
) -> None:
    """Regression: nasdaq-data-link's SDK returns pandas datetime64[ns] for
    the date column; pl.from_pandas converts to pl.Datetime; write_parquet
    preserves it. If read_sep_prices does not cast back to pl.Date, the
    downstream (asset_id, dt) price-index keys become (int, datetime) while
    lookups use date(), and every lookup silently returns None. The
    constant-weight demo then runs 3774 bars, never rebalances, and stays
    100% in cash with the reference reproducing the same wrong number
    (engine == reference: PASS on $0 vs $0).

    This test pins the cast so the failure mode cannot recur.
    """
    from datetime import datetime

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_dt_regression"
    bundle_dir.mkdir(parents=True)

    # Use pl.Datetime explicitly to mirror what pl.from_pandas produces.
    sep_df = pl.DataFrame(
        {
            "ticker": ["SPY", "SPY"],
            "date": [datetime(2024, 3, 14), datetime(2024, 3, 15)],
            "open": [517.0, 517.95],
            "high": [517.5, 518.43],
            "low": [516.0, 510.27],
            "close": [517.51, 512.85],
            "closeunadj": [517.51, 512.85],
            "volume": [70_000_000, 92_750_000],
        },
        schema_overrides={"date": pl.Datetime},
    )
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)

    actions_df = pl.DataFrame(
        {
            "ticker": ["SPY"],
            "date": [datetime(2024, 3, 15)],
            "action": ["dividend"],
            "value": [1.7715],
        },
        schema_overrides={"date": pl.Datetime},
    )
    actions_path = bundle_dir / "actions.parquet"
    actions_df.write_parquet(actions_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_dt_regression]
source = "sharadar"
pull_date = 2026-05-29

[snapshots.sharadar_dt_regression.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = 2 }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_path.stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    adapter = SharadarDataSource("sharadar_dt_regression", snapshots_root)

    prices = adapter.read_sep_prices(ticker="SPY")
    assert prices.schema["dt"] == pl.Date, (
        f"read_sep_prices must return pl.Date for 'dt'; got {prices.schema['dt']}. "
        f"Without the cast, downstream BarLoop price-index keys mismatch and "
        f"the constant-weight demo silently never rebalances."
    )
    # Iterating must yield python date objects, not datetime
    first_dt = prices["dt"][0]
    assert isinstance(first_dt, date) and not isinstance(first_dt, datetime), (
        f"prices['dt'][0] must be a date, not a datetime; got {type(first_dt)}"
    )

    dividends = adapter.read_actions_dividends(ticker="SPY")
    assert dividends.schema["ex_date"] == pl.Date, (
        f"read_actions_dividends must return pl.Date for 'ex_date'; got "
        f"{dividends.schema['ex_date']}"
    )


def test_date_range_filter_works_on_datetime_typed_input(tmp_path: Path) -> None:
    """Regression: the date-range filter must return non-empty rows when
    the underlying parquet has date column dtype `Datetime[ns]`.

    The previous regression test
    (test_read_sep_prices_returns_pl_date_column_even_from_datetime_source)
    asserted that read_sep_prices RETURNS pl.Date, but it called the
    method WITHOUT a date filter; the cast happened after the filter
    block and the filter pattern was untested against Datetime input.

    The 2026-05-29 hotfix surfaced this when Sam re-pulled with pinned
    pandas 2.2.3: pandas datetime64[ns] -> pl.Datetime[ns]. The real
    failure mode under Polars 1.41.1 is the UPPER bound: a Python
    `date(2999, 12, 31)` literal OVERFLOWS Datetime[ns]'s i64-ns-since-
    epoch representable range (~1677-2262); the literal silently
    saturates and the comparison `pl.col('date') <= date(2999, 12, 31)`
    yields zero rows. Narrow-window queries within the representable
    range (e.g. `<= date(2026, 4, 30)`) still worked, masking the bug
    from any test that did not exercise the wide-open coverage probe
    that reconcile_spy_trailing uses (`pl.col('date') <= date(2999, 12, 31)`).

    This test exercises:
      1. A narrow-window date-range filter on Datetime[ns]-typed input
         (pre-fix path: PASS because the upper bound is within range).
      2. The wide-open coverage probe (pre-fix path: FAIL with 0 rows
         because date(2999, 12, 31) overflows Datetime[ns]).
      3. Same regression on read_actions_dividends.

    Critical: `schema_overrides={"date": pl.Datetime(time_unit="ns")}`
    is explicit; bare `pl.Datetime` defaults to `time_unit="us"` which
    does NOT overflow at date(2999, 12, 31) and would silently neutralize
    this test. The on-disk dtype is asserted post-write to lock the
    invariant against a future Polars default change.
    """
    from datetime import datetime

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_dt_filter_regression"
    bundle_dir.mkdir(parents=True)

    sep_df = pl.DataFrame(
        {
            "ticker": ["SPY", "SPY", "SPY"],
            "date": [
                datetime(2024, 3, 14),
                datetime(2024, 3, 15),
                datetime(2024, 3, 18),
            ],
            "open": [517.0, 517.95, 513.0],
            "high": [517.5, 518.43, 515.0],
            "low": [516.0, 510.27, 511.0],
            "close": [517.51, 512.85, 514.0],
            "closeunadj": [517.51, 512.85, 514.0],
            "volume": [70_000_000, 92_750_000, 60_000_000],
        },
        schema_overrides={"date": pl.Datetime(time_unit="ns")},
    )
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)

    actions_df = pl.DataFrame(
        {
            "ticker": ["SPY"],
            "date": [datetime(2024, 3, 15)],
            "action": ["dividend"],
            "value": [1.7715],
        },
        schema_overrides={"date": pl.Datetime(time_unit="ns")},
    )
    actions_path = bundle_dir / "actions.parquet"
    actions_df.write_parquet(actions_path)

    # Lock the on-disk dtype against a future Polars default change.
    # If write_parquet ever promotes Datetime[ns] to Datetime[us] (or
    # vice versa) the bug class shifts and this test must be revisited
    # rather than silently passing on the wrong dtype.
    on_disk_dtype = pl.scan_parquet(sep_path).collect_schema()["date"]
    assert on_disk_dtype == pl.Datetime(time_unit="ns"), (
        f"on-disk dtype is {on_disk_dtype}; expected Datetime[ns] so this "
        f"regression test actually exercises the i64-ns-since-epoch "
        f"overflow class. Update the test if Polars changes defaults."
    )

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_dt_filter_regression]
source = "sharadar"
pull_date = 2026-05-29

[snapshots.sharadar_dt_filter_regression.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = 3 }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_path.stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    adapter = SharadarDataSource("sharadar_dt_filter_regression", snapshots_root)

    # Narrow-window query: both bounds within Datetime[ns] representable range.
    # Pre-fix path: PASS at 3 rows because the narrow window does not overflow.
    # Post-fix path: PASS at 3 rows because the cast normalizes the column.
    prices_narrow = adapter.read_sep_prices(
        ticker="SPY",
        start_dt=date(2024, 3, 14),
        end_dt=date(2024, 3, 18),
    )
    assert prices_narrow.height == 3, (
        f"narrow-window query returned {prices_narrow.height} rows; "
        f"expected 3"
    )

    # The wide-open coverage probe that reconcile_spy_trailing uses.
    # Pre-fix path: FAIL at 0 rows because date(2999, 12, 31) overflows
    # Datetime[ns] and the <= comparison silently saturates.
    # Post-fix path: PASS at 3 rows because the cast normalizes to pl.Date
    # which has a wider representable range and accepts the literal.
    coverage = adapter.read_sep_prices(
        ticker="SPY",
        start_dt=date(1900, 1, 1),
        end_dt=date(2999, 12, 31),
    )
    assert coverage.height == 3, (
        f"wide-open coverage query returned {coverage.height} rows; "
        f"expected 3. The pre-fix path overflowed Datetime[ns] at "
        f"date(2999, 12, 31); the post-fix path casts to pl.Date first."
    )

    # Same regression on dividends (wide-open + narrow-window).
    dividends_wide = adapter.read_actions_dividends(
        ticker="SPY",
        start_dt=date(1900, 1, 1),
        end_dt=date(2999, 12, 31),
    )
    assert dividends_wide.height == 1, (
        f"actions wide-open coverage query returned "
        f"{dividends_wide.height} rows; expected 1. Same overflow class."
    )

    dividends_narrow = adapter.read_actions_dividends(
        ticker="SPY",
        start_dt=date(2024, 3, 14),
        end_dt=date(2024, 3, 18),
    )
    assert dividends_narrow.height == 1, (
        f"actions narrow-window query returned {dividends_narrow.height} "
        f"rows; expected 1"
    )


# ============================================================
# M3 PR 1: TICKERS and SF1 ARQ reader tests
# ============================================================

_M3_TABLES = ("sep", "actions", "tickers", "sf1")


def test_read_tickers_returns_full_column_set(tmp_path: Path) -> None:
    """read_tickers returns the documented column subset with pl.Date
    dtype on the four date columns.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_tickers()
    assert df.height == 3
    assert df.columns == [
        "permaticker",
        "ticker",
        "name",
        "exchange",
        "isdelisted",
        "firstpricedate",
        "lastpricedate",
        "firstquarter",
        "lastquarter",
        "cusip",
    ]
    assert df.schema["firstpricedate"] == pl.Date
    assert df.schema["lastpricedate"] == pl.Date
    assert df.schema["firstquarter"] == pl.Date
    assert df.schema["lastquarter"] == pl.Date


def test_read_tickers_filters_by_ticker(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    spy = adapter.read_tickers(ticker="SPY")
    assert spy.height == 1
    assert spy["permaticker"][0] == 100


def test_read_tickers_filters_by_permaticker(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    oldco = adapter.read_tickers(permaticker=300)
    assert oldco.height == 1
    assert oldco["ticker"][0] == "OLDCO"


def test_read_tickers_filters_by_active_at_includes_null_lastpricedate(
    tmp_path: Path,
) -> None:
    """active_at=2026-01-01: SPY (NULL lastpricedate, active) and AGG
    (NULL lastpricedate, active) are included; OLDCO (lastpricedate
    2014-12-31) is excluded.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    active_2026 = adapter.read_tickers(active_at=date(2026, 1, 1))
    tickers = sorted(active_2026["ticker"].to_list())
    assert tickers == ["AGG", "SPY"]


def test_read_tickers_active_at_includes_delisted_within_interval(
    tmp_path: Path,
) -> None:
    """active_at=2012-06-01 includes OLDCO (interval 2010-01-04 to
    2014-12-31) and excludes neither SPY nor AGG.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    active_2012 = adapter.read_tickers(active_at=date(2012, 6, 1))
    tickers = sorted(active_2012["ticker"].to_list())
    assert tickers == ["AGG", "OLDCO", "SPY"]


def test_read_tickers_sorted_by_permaticker_then_firstpricedate(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_tickers()
    assert df["permaticker"].to_list() == [100, 200, 300]


def test_read_sf1_arq_filters_to_arq_dimension_by_default(tmp_path: Path) -> None:
    """The default dimension is ARQ; ART, ARY, MRQ rows are excluded."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sf1_arq(ticker="SPY")
    assert df.height == 2
    assert set(df["dimension"].to_list()) == {"ARQ"}


def test_read_sf1_arq_explicit_art_dimension_returns_art_rows(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sf1_arq(ticker="SPY", dimension="ART")
    assert df.height == 1
    assert df["dimension"][0] == "ART"


def test_read_sf1_arq_explicit_ary_dimension_returns_ary_rows(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sf1_arq(ticker="SPY", dimension="ARY")
    assert df.height == 1
    assert df["dimension"][0] == "ARY"


def test_read_sf1_arq_dimension_input_is_case_normalized(tmp_path: Path) -> None:
    """Per Plan-reviewer High 5: dimension input is normalized to
    uppercase before membership check. 'arq', 'Arq', 'ARQ' all behave
    identically.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    lower = adapter.read_sf1_arq(ticker="SPY", dimension="arq")
    mixed = adapter.read_sf1_arq(ticker="SPY", dimension="Arq")
    upper = adapter.read_sf1_arq(ticker="SPY", dimension="ARQ")
    assert lower.height == upper.height == mixed.height == 2


def test_read_sf1_arq_rejects_mrq_dimension(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        adapter.read_sf1_arq(ticker="SPY", dimension="MRQ")
    assert "not PIT" in str(exc_info.value)
    assert "['ARQ', 'ART', 'ARY']" in str(exc_info.value)


def test_read_sf1_arq_rejects_mrt_dimension(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError):
        adapter.read_sf1_arq(ticker="SPY", dimension="MRT")


def test_read_sf1_arq_rejects_mry_dimension(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError):
        adapter.read_sf1_arq(ticker="SPY", dimension="MRY")


def test_read_sf1_arq_rejects_unknown_dimension(tmp_path: Path) -> None:
    """A typo like 'ARTM' is rejected with the accepted set surfaced."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError):
        adapter.read_sf1_arq(ticker="SPY", dimension="ARTM")


def test_read_sf1_arq_filters_by_datekey_range(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    # Only the 2024-04-15 ARQ row is within the range.
    df = adapter.read_sf1_arq(
        ticker="SPY",
        datekey_start=date(2024, 4, 1),
        datekey_end=date(2024, 4, 30),
    )
    assert df.height == 1
    assert df["datekey"][0] == date(2024, 4, 15)
    assert df["calendardate"][0] == date(2024, 3, 31)


def test_read_sf1_arq_returns_pl_date_columns(tmp_path: Path) -> None:
    """Per project rule 12 the cast-before-filter contract: SF1's three
    date columns (calendardate, datekey, reportperiod) come back as pl.Date.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sf1_arq(ticker="SPY")
    assert df.schema["calendardate"] == pl.Date
    assert df.schema["datekey"] == pl.Date
    assert df.schema["reportperiod"] == pl.Date


def test_read_sf1_arq_sorted_by_ticker_then_datekey_then_calendardate(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sf1_arq(ticker="SPY")
    # ARQ rows only; sorted by datekey ascending.
    datekeys = df["datekey"].to_list()
    assert datekeys == [date(2024, 1, 15), date(2024, 4, 15)]


def test_resolver_from_sharadar_data_source_uses_manifest_verified_tickers(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Critical 1: production resolver path constructs
    from a SharadarDataSource so the snapshot SHA256 commitment in
    dataset_versioning.md is the vintage gate. This test exercises that
    code path end-to-end.
    """
    from pit_backtest.data.records import AssetId
    from pit_backtest.data.resolver import SharadarPermatickerResolver

    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    resolver = SharadarPermatickerResolver(adapter)
    assert resolver.resolve_ticker("SPY", datetime(2024, 1, 1, 16, 0)) == AssetId(100)
    assert resolver.resolve_ticker("AGG", datetime(2024, 1, 1, 16, 0)) == AssetId(200)
    assert resolver.resolve_ticker("OLDCO", datetime(2012, 6, 1, 16, 0)) == AssetId(300)
    assert resolver.get_ticker(AssetId(100), datetime(2024, 1, 1, 16, 0)) == "SPY"
