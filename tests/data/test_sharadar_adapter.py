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
# M3 PR 5a: IPO-window SEP bars (one per TICKERS firstpricedate) so the
# FirstPriceWithinFiveDaysContract passes when the bundle ships TICKERS.
# Exposed as a module-level constant so inline tests can include them in
# their own bundles without copying.
_IPO_WINDOW_SEP_ROWS: list[dict[str, object]] = [
    {"ticker": "SPY", "date": date(1993, 1, 22), "open": 43.97, "high": 43.97, "low": 43.75, "close": 43.94, "closeunadj": 43.94, "volume": 1_003_200},
    {"ticker": "AGG", "date": date(2003, 9, 26), "open": 100.10, "high": 100.20, "low": 100.05, "close": 100.15, "closeunadj": 100.15, "volume": 50_000},
    {"ticker": "OLDCO", "date": date(2010, 1, 4), "open": 9.50, "high": 9.60, "low": 9.45, "close": 9.55, "closeunadj": 9.55, "volume": 80_000},
    {"ticker": "DLST", "date": date(2015, 1, 5), "open": 25.10, "high": 25.40, "low": 25.00, "close": 25.30, "closeunadj": 25.30, "volume": 50_000},
]


_SEP_ROWS = [
    # M3 PR 5a: IPO-window bars (defined above) prepended so the
    # FirstPriceWithinFiveDaysContract passes on the M3_TABLES superset.
    *_IPO_WINDOW_SEP_ROWS,
    # SPY rows for the Q1 2024 TR demo and the per-row get_price tests.
    {"ticker": "SPY", "date": date(2024, 3, 13), "open": 515.00, "high": 518.00, "low": 514.00, "close": 517.51, "closeunadj": 517.51, "volume": 80_000_000},
    {"ticker": "SPY", "date": date(2024, 3, 14), "open": 517.00, "high": 518.50, "low": 516.50, "close": 517.51, "closeunadj": 517.51, "volume": 70_000_000},
    {"ticker": "SPY", "date": date(2024, 3, 15), "open": 517.95, "high": 518.43, "low": 510.27, "close": 512.85, "closeunadj": 512.85, "volume": 92_750_000},
    {"ticker": "SPY", "date": date(2024, 3, 18), "open": 513.00, "high": 515.00, "low": 511.00, "close": 514.00, "closeunadj": 514.00, "volume": 60_000_000},
    # Non-SPY row to exercise the filter
    {"ticker": "AGG", "date": date(2024, 3, 15), "open": 95.00, "high": 95.50, "low": 94.80, "close": 95.20, "closeunadj": 95.20, "volume": 5_000_000},
    # M3 PR 3: DLST row at its lastpricedate to exercise get_delisting.
    {"ticker": "DLST", "date": date(2018, 6, 30), "open": 12.45, "high": 12.55, "low": 12.40, "close": 12.50, "closeunadj": 12.50, "volume": 100_000},
]

_ACTIONS_ROWS = [
    {"ticker": "SPY", "date": date(2024, 3, 15), "action": "dividend", "value": 1.7715},
    {"ticker": "SPY", "date": date(2023, 12, 15), "action": "dividend", "value": 1.5800},
    {"ticker": "AGG", "date": date(2024, 3, 1), "action": "dividend", "value": 0.2800},
    # M3 PR 3: realistic 2-for-1 forward split (Plan-reviewer High 3:
    # a value=1.0 ratio is a no-op that Sharadar never produces).
    {"ticker": "SPY", "date": date(2024, 3, 15), "action": "split", "value": 2.0},
    # M3 PR 3: spinoff dispatch row (CMW1993 + MO2004 bias note per ADR
    # 0002 dec 14; v1 ships the cash-equivalent approximation).
    {"ticker": "SPY", "date": date(2024, 6, 21), "action": "spinoff", "value": 12.50},
    # M3 PR 3: announce-only codes routed through _SHARADAR_SKIPPED_ACTIONS;
    # placed OUTSIDE the M1 SPY TR window so read_actions_dividends is
    # unaffected.
    {"ticker": "SPY", "date": date(2024, 9, 30), "action": "transfer", "value": 0.0},
    {"ticker": "OLDCO", "date": date(2014, 6, 30), "action": "acquisitionbystock", "value": 0.0},
    # M3 PR 3: unknown action triggers the WARN-and-skip path (Plan-reviewer
    # Counter on Choice 1: vendor schema additions must not crash backtests).
    {"ticker": "SPY", "date": date(2024, 11, 15), "action": "fictitious_action", "value": 0.0},
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
    # M3 PR 3: delisted with a known SEP closeunadj at lastpricedate so
    # get_delisting can exercise the closeunadj-based cash-flow path.
    {
        "permaticker": 400,
        "ticker": "DLST",
        "name": "Delisting Test Co",
        "exchange": "NASDAQ",
        "isdelisted": "Y",
        "firstpricedate": date(2015, 1, 5),
        "lastpricedate": date(2018, 6, 30),
        "firstquarter": date(2015, 3, 31),
        "lastquarter": date(2018, 6, 30),
        "cusip": "DLST00001",
    },
]

# M3 PR 4: Sharadar SP500 event-log rows. Per Plan-reviewer Critical 1
# the shared fixture uses SIMPLE intervals (no add-remove-add) so the
# story does not conflate SP500 membership with TICKERS lifecycle.
# Multi-interval testing happens in inline bundles in test_universe.py.
#
# Schema per docs/methodology/dataset_versioning.md:28 and
# docs/research/sources/methodology-point-in-time.md: (ticker, date, action).
# No name / contraticker pass-throughs at v1 (Plan-reviewer High 1
# corrected the original plan's hallucinated extras).
_SP500_ROWS = [
    # SPY added 1995-09-19 (open-ended; AssetId(100) per _TICKERS_ROWS row 1).
    # Date chosen after SPY's TICKERS firstpricedate of 1993-01-22 so the
    # resolver successfully maps ticker -> AssetId at the event date.
    {"ticker": "SPY", "date": date(1995, 9, 19), "action": "added"},
    # AGG added 2010 removed 2015 (closed interval; AssetId(200) per
    # _TICKERS_ROWS row 2; AGG never delisted from TICKERS so "membership
    # ended in 2015" is purely an SP500 event, not a TICKERS lifecycle event).
    {"ticker": "AGG", "date": date(2010, 6, 15), "action": "added"},
    {"ticker": "AGG", "date": date(2015, 12, 31), "action": "removed"},
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
        "sp500": _SP500_ROWS,
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
    """read_sep_prices returns only the rows for the requested ticker, sorted by dt.

    Per M3 PR 5a the fixture also carries IPO-window SEP rows (one per
    TICKERS firstpricedate) so the FirstPriceWithinFiveDaysContract
    passes at __init__; this test pins the resulting SPY row inventory
    at 5 (4 Q1 2024 rows plus the 1993-01-22 IPO row) and AGG at 2
    (the 2003-09-26 IPO row plus the 2024-03-15 row).
    """
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    spy = adapter.read_sep_prices(ticker="SPY")
    assert spy.height == 5  # 1 IPO row + 4 Q1 2024 rows
    assert spy["dt"].to_list() == [
        date(1993, 1, 22),
        date(2024, 3, 13),
        date(2024, 3, 14),
        date(2024, 3, 15),
        date(2024, 3, 18),
    ]
    assert spy["close"][3] == pytest.approx(512.85)
    assert spy["closeunadj"][3] == pytest.approx(512.85)

    agg = adapter.read_sep_prices(ticker="AGG")
    assert agg.height == 2  # IPO row + 2024-03-15 row
    assert agg["dt"].to_list() == [date(2003, 9, 26), date(2024, 3, 15)]


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

_M3_TABLES = ("sep", "actions", "tickers", "sf1", "sp500")


def test_read_tickers_returns_full_column_set(tmp_path: Path) -> None:
    """read_tickers returns the documented column subset with pl.Date
    dtype on the four date columns.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_tickers()
    # Includes the M3 PR 3 DLST row (permaticker=400) added at fixture time.
    assert df.height == 4
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
    # Includes the M3 PR 3 DLST row (permaticker=400).
    assert df["permaticker"].to_list() == [100, 200, 300, 400]


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


# ============================================================
# M3 PR 2: get_price + get_fundamental tests
# ============================================================

from pit_backtest.data.records import AssetId  # noqa: E402
from pit_backtest.data.resolver import TickerNotFoundError  # noqa: E402
from pit_backtest.data.sources.sharadar import PriceNotFoundError  # noqa: E402


def test_get_price_close_returns_decimal_from_sep_row(tmp_path: Path) -> None:
    """Happy path: SPY 2024-03-15 close = 512.85 returns Decimal."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    price = adapter.get_price(
        AssetId(100), datetime(2024, 3, 15, 16, 0), "close"
    )
    assert price == Decimal("512.85")


def test_get_price_all_price_fields_resolve(tmp_path: Path) -> None:
    """Exercises each PriceField on the SPY 2024-03-15 row."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    dt = datetime(2024, 3, 15, 16, 0)
    assert adapter.get_price(AssetId(100), dt, "open") == Decimal("517.95")
    assert adapter.get_price(AssetId(100), dt, "high") == Decimal("518.43")
    assert adapter.get_price(AssetId(100), dt, "low") == Decimal("510.27")
    assert adapter.get_price(AssetId(100), dt, "close") == Decimal("512.85")
    assert adapter.get_price(AssetId(100), dt, "volume") == Decimal("92750000")


def test_get_price_volume_is_decimal_from_int_lossless_at_high_magnitude(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Medium 8: volume goes through Decimal(int(value))
    directly, NOT float(int(value)). At magnitudes > 2**53 the float path
    would lose precision; the int path is exact.

    The synthetic fixture uses 92_750_000 which is well below 2**53; the
    test pins the contract by asserting the exact value, so a future
    refactor that introduces a float intermediate would fail this test.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    volume = adapter.get_price(
        AssetId(100), datetime(2024, 3, 15, 16, 0), "volume"
    )
    assert volume == Decimal("92750000")
    assert volume == Decimal(92750000)


def test_get_price_no_sep_row_for_date_raises_price_not_found(
    tmp_path: Path,
) -> None:
    """Weekend / holiday: no SEP row at the requested date."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(PriceNotFoundError) as exc_info:
        adapter.get_price(
            AssetId(100), datetime(2024, 3, 16, 16, 0), "close"
        )
    message = str(exc_info.value)
    assert "SPY" in message
    assert "2024-03-16" in message


def test_get_price_unknown_asset_raises_ticker_not_found(
    tmp_path: Path,
) -> None:
    """Asset not in the resolver index propagates TickerNotFoundError."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(TickerNotFoundError):
        adapter.get_price(
            AssetId(999), datetime(2024, 3, 15, 16, 0), "close"
        )


def test_get_price_post_delisting_raises_ticker_not_found_via_resolver(
    tmp_path: Path,
) -> None:
    """OLDCO has lastpricedate=2014-12-31. A 2024-03-15 lookup falls
    outside the resolver's interval; resolver raises TickerNotFoundError
    BEFORE the SEP lookup. This test pins the failure ordering.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(TickerNotFoundError):
        adapter.get_price(
            AssetId(300), datetime(2024, 3, 15, 16, 0), "close"
        )


def test_get_price_unknown_field_raises_value_error(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Medium 9: defensive runtime check guards against
    `cast(PriceField, untrusted_str)` misuse. mypy strict catches static
    typos; this catches the runtime route.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        # Bypass mypy via cast; the runtime check is what we're testing.
        adapter.get_price(
            AssetId(100),
            datetime(2024, 3, 15, 16, 0),
            "bogus_field",  # type: ignore[arg-type]
        )
    assert "not a price field" in str(exc_info.value)


def test_get_price_multi_row_collision_raises_value_error(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Medium 7: SEP duplicate (ticker, date) is a
    vendor bug; the method must refuse to silently pick one row.

    This test constructs its own synthetic bundle inline with a deliberate
    duplicate; the shared `_SEP_ROWS` fixture stays clean for the other
    tests.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_multirow"
    bundle_dir.mkdir(parents=True)

    # M3 PR 5a: prepend IPO-window SEP rows so the
    # FirstPriceWithinFiveDaysContract passes at __init__. The duplicate
    # row remains the focus of the test; the IPO rows are bookkeeping.
    sep_dup_rows = [
        *_IPO_WINDOW_SEP_ROWS,
        {
            "ticker": "SPY", "date": date(2024, 3, 15),
            "open": 517.95, "high": 518.43, "low": 510.27,
            "close": 512.85, "closeunadj": 512.85, "volume": 92_750_000,
        },
        {
            "ticker": "SPY", "date": date(2024, 3, 15),  # DUPLICATE
            "open": 517.95, "high": 518.43, "low": 510.27,
            "close": 513.00, "closeunadj": 513.00, "volume": 92_750_001,
        },
    ]
    sep_df = pl.DataFrame(sep_dup_rows)
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)

    tickers_df = pl.DataFrame(_TICKERS_ROWS)
    tickers_path = bundle_dir / "tickers.parquet"
    tickers_df.write_parquet(tickers_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256(tickers_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_multirow]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_multirow.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_path.stat().st_size}, row_count = {len(sep_dup_rows)} }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {tickers_path.stat().st_size}, row_count = {len(_TICKERS_ROWS)} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_multirow", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        adapter.get_price(
            AssetId(100), datetime(2024, 3, 15, 16, 0), "close"
        )
    assert "expected exactly 1" in str(exc_info.value)
    assert "Vendor data-quality bug" in str(exc_info.value)


def test_get_price_on_sep_only_bundle_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Medium 6: a SEP-only bundle (no tickers.parquet)
    crashes at the first per-row call because the lazy resolver needs
    TICKERS. The failure mode is FileNotFoundError from the table
    lookup; we pin that here so a future user debugging the failure has
    a precedent.
    """
    # Default _write_synthetic_bundle ships SEP + ACTIONS only.
    snapshots_root = _write_synthetic_bundle(tmp_path)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(FileNotFoundError) as exc_info:
        adapter.get_price(
            AssetId(100), datetime(2024, 3, 15, 16, 0), "close"
        )
    assert "tickers.parquet" in str(exc_info.value)


def test_get_price_decimal_precision_round_trip_on_non_clean_float(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer High 5: the precision contract bites only on
    non-cleanly-representable floats. 517.51 has no exact binary
    representation; `Decimal(517.51)` returns the binary expansion
    (~ 'Decimal("517.5099...")`), but `to_boundary_decimal` uses
    `Decimal(repr(517.51))` which yields exactly `Decimal('517.51')`.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    price = adapter.get_price(
        AssetId(100), datetime(2024, 3, 14, 16, 0), "close"
    )
    assert price == Decimal("517.51")
    # The binary expansion would NOT equal Decimal("517.51").
    assert price != Decimal(517.51)


def test_get_fundamental_happy_path_returns_most_recent_arq_revenue(
    tmp_path: Path,
) -> None:
    """available_dt=2024-05-01 sees both ARQ rows; returns the 2024-04-15
    datekey row (revenue=1000.0), not the 2024-01-15 row (revenue=950.0).
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ARQ"
    )
    assert revenue == Decimal("1000.0")


def test_get_fundamental_available_dt_before_any_row_returns_none(
    tmp_path: Path,
) -> None:
    """available_dt=2023-12-31: no SF1 row has datekey <= this. Returns None.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2023, 12, 31, 16, 0), "revenue", "ARQ"
    )
    assert revenue is None


def test_get_fundamental_available_dt_between_rows_returns_earlier_row(
    tmp_path: Path,
) -> None:
    """available_dt=2024-02-01: only the 2024-01-15 datekey row is
    observable; returns its revenue=950.0.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 2, 1, 16, 0), "revenue", "ARQ"
    )
    assert revenue == Decimal("950.0")


def test_get_fundamental_art_flavor_returns_art_value(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ART"
    )
    assert revenue == Decimal("3900.0")


def test_get_fundamental_ary_flavor_returns_ary_value(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ARY"
    )
    assert revenue == Decimal("3850.0")


def test_get_fundamental_mrq_flavor_raises_value_error(tmp_path: Path) -> None:
    """MRQ is the restated dimension; per the PIT contract it is rejected
    at the boundary.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        adapter.get_fundamental(
            AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "MRQ"
        )
    message = str(exc_info.value)
    assert "not PIT" in message
    assert "['ARQ', 'ART', 'ARY']" in message


def test_get_fundamental_unknown_field_raises_value_error_with_columns(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        adapter.get_fundamental(
            AssetId(100), datetime(2024, 5, 1, 16, 0), "bogus_column", "ARQ"
        )
    message = str(exc_info.value)
    assert "bogus_column" in message
    assert "available columns" in message


def test_get_fundamental_unknown_asset_raises_ticker_not_found(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(TickerNotFoundError):
        adapter.get_fundamental(
            AssetId(999), datetime(2024, 5, 1, 16, 0), "revenue", "ARQ"
        )


def test_get_fundamental_does_not_return_row_with_datekey_in_future(
    tmp_path: Path,
) -> None:
    """LOOKAHEAD-LEAK regression. The structural PIT protection.

    Per Plan-reviewer High 4 (NULL field detection order) + the M3 PR 1
    rule 2D mandate: a buggy implementation that uses `<` instead of `<=`
    or omits the datekey filter would return the 2024-04-15 row's revenue
    (1000.0) on a query at available_dt=2024-04-14. The correct behavior
    returns the 2024-01-15 row's revenue (950.0).

    available_dt=2024-04-14 is one calendar day before the 2024-04-15
    datekey; the filter `datekey <= 2024-04-14` excludes 2024-04-15.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 4, 14, 16, 0), "revenue", "ARQ"
    )
    # Must NOT be the future row's value.
    assert revenue != Decimal("1000.0")
    # Must be the most recent observable row.
    assert revenue == Decimal("950.0")


def test_get_fundamental_datekey_equal_to_available_dt_is_observable(
    tmp_path: Path,
) -> None:
    """Boundary case: datekey == available_dt is observable (the gate is
    `<=`, not `<`). A buggy strict-less-than implementation would skip
    the row.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    # The 2024-04-15 ARQ row has datekey == 2024-04-15.
    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 4, 15, 16, 0), "revenue", "ARQ"
    )
    assert revenue == Decimal("1000.0")


def test_get_fundamental_decimal_precision_round_trip_on_non_clean_float(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer High 5: precision contract with non-clean float.
    The synthetic fixture's 1000.0 is a clean float and would not catch
    a `Decimal(float)` regression. This test builds its own SF1 bundle
    inline with revenue=517.51 (the same non-clean value the SEP test
    uses) and asserts the boundary helper's repr-round-trip path.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_sf1_precision"
    bundle_dir.mkdir(parents=True)

    sf1_rows = [
        {
            "ticker": "SPY",
            "dimension": "ARQ",
            "calendardate": date(2024, 3, 31),
            "datekey": date(2024, 4, 15),
            "reportperiod": date(2024, 3, 31),
            "lastupdated": date(2024, 4, 16),
            "revenue": 517.51,  # Non-clean float per Plan-reviewer High 5
            "netinc": 100.0,
        },
    ]
    sf1_df = pl.DataFrame(sf1_rows)
    sf1_path = bundle_dir / "sf1.parquet"
    sf1_df.write_parquet(sf1_path)

    tickers_df = pl.DataFrame(_TICKERS_ROWS)
    tickers_path = bundle_dir / "tickers.parquet"
    tickers_df.write_parquet(tickers_path)

    sf1_sha = hashlib.sha256(sf1_path.read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256(tickers_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_sf1_precision]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_sf1_precision.files]
"sf1.parquet" = {{ sha256 = "{sf1_sha}", size_bytes = {sf1_path.stat().st_size}, row_count = 1 }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {tickers_path.stat().st_size}, row_count = {len(_TICKERS_ROWS)} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_sf1_precision", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ARQ"
    )
    assert revenue == Decimal("517.51")
    assert revenue != Decimal(517.51)  # Binary-expansion path would not equal.


def test_get_fundamental_null_field_returns_none(tmp_path: Path) -> None:
    """Per Plan-reviewer High 4 direction 1: the selected (most recent
    observable) row has NULL in the requested field; returns None even
    though an earlier row exists with a non-null value.

    Inline fixture: one row with revenue=1000.0 datekey=2024-01-15 and
    a more recent row with revenue=NULL datekey=2024-04-15. A query at
    available_dt=2024-05-01 selects the more recent row; its NULL
    revenue collapses to None per the documented semantic.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_null_field"
    bundle_dir.mkdir(parents=True)

    sf1_rows = [
        {
            "ticker": "SPY",
            "dimension": "ARQ",
            "calendardate": date(2023, 12, 31),
            "datekey": date(2024, 1, 15),
            "reportperiod": date(2023, 12, 31),
            "lastupdated": date(2024, 1, 16),
            "revenue": 1000.0,
            "netinc": 100.0,
        },
        {
            "ticker": "SPY",
            "dimension": "ARQ",
            "calendardate": date(2024, 3, 31),
            "datekey": date(2024, 4, 15),
            "reportperiod": date(2024, 3, 31),
            "lastupdated": date(2024, 4, 16),
            "revenue": None,  # NULL in the more recent row
            "netinc": 100.0,
        },
    ]
    sf1_df = pl.DataFrame(sf1_rows)
    sf1_path = bundle_dir / "sf1.parquet"
    sf1_df.write_parquet(sf1_path)

    tickers_df = pl.DataFrame(_TICKERS_ROWS)
    tickers_path = bundle_dir / "tickers.parquet"
    tickers_df.write_parquet(tickers_path)

    sf1_sha = hashlib.sha256(sf1_path.read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256(tickers_path.read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_null_field]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_null_field.files]
"sf1.parquet" = {{ sha256 = "{sf1_sha}", size_bytes = {sf1_path.stat().st_size}, row_count = 2 }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {tickers_path.stat().st_size}, row_count = {len(_TICKERS_ROWS)} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_null_field", snapshots_root)

    revenue = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ARQ"
    )
    # Most recent observable row has NULL revenue; returns None even though
    # the earlier row has revenue=1000.0.
    assert revenue is None
    # The earlier row's netinc IS observable and non-null; confirm the
    # underlying bundle is well-formed (sanity check).
    netinc = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "netinc", "ARQ"
    )
    assert netinc == Decimal("100.0")


def test_get_fundamental_case_insensitive_flavor_input(tmp_path: Path) -> None:
    """Mirrors read_sf1_arq case-normalization contract."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    upper = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "ARQ"
    )
    lower = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "arq"  # type: ignore[arg-type]
    )
    mixed = adapter.get_fundamental(
        AssetId(100), datetime(2024, 5, 1, 16, 0), "revenue", "Arq"  # type: ignore[arg-type]
    )
    assert upper == lower == mixed == Decimal("1000.0")


# ============================================================
# M3 PR 3: corp actions + cash flows + delisting dispatch tests
# ============================================================

from datetime import time  # noqa: E402

from pit_backtest.data.records import (  # noqa: E402
    CashFlow,
    SplitAction,
)
from pit_backtest.data.sources.sharadar import (  # noqa: E402
    DelistingDataQualityError,
    _dispatch_action_row,
)


# ----- Dispatch helper unit tests -----

def test_dispatch_action_row_dividend_returns_cash_flow() -> None:
    row = {
        "ticker": "SPY",
        "date": date(2024, 3, 15),
        "action": "dividend",
        "value": 1.7715,
    }
    result = _dispatch_action_row(row, AssetId(100))
    assert isinstance(result, CashFlow)
    assert result.asset_id == AssetId(100)
    assert result.dt == datetime(2024, 3, 15, 16, 0)
    assert result.flow_type == "cash_dividend"
    assert result.amount == Decimal("1.7715")


def test_dispatch_action_row_split_returns_split_action() -> None:
    row = {
        "ticker": "SPY",
        "date": date(2024, 3, 15),
        "action": "split",
        "value": 2.0,
    }
    result = _dispatch_action_row(row, AssetId(100))
    assert isinstance(result, SplitAction)
    assert result.asset_id == AssetId(100)
    assert result.ex_date == datetime(2024, 3, 15, 16, 0)
    assert result.ratio == Decimal("2.0")


def test_dispatch_action_row_spinoff_returns_cash_flow_spinoff_equivalent() -> None:
    row = {
        "ticker": "SPY",
        "date": date(2024, 6, 21),
        "action": "spinoff",
        "value": 12.50,
    }
    result = _dispatch_action_row(row, AssetId(100))
    assert isinstance(result, CashFlow)
    assert result.flow_type == "spinoff_cash_equivalent"
    assert result.amount == Decimal("12.50")


def test_dispatch_action_row_skipped_action_returns_none() -> None:
    """Announce-only codes (listed, initiated, delisted, transfer,
    tradinghaltresumed) and TICKERS-routed codes (acquisitionby*,
    bankruptcy*) all return None (no warning).
    """
    skipped_actions = [
        "listed", "initiated", "delisted", "transfer",
        "tradinghaltresumed", "acquisitionbystock", "acquisitionbycash",
        "acquisitionunknown", "bankruptcyliquidation",
        "bankruptcyreorganization",
    ]
    for action in skipped_actions:
        row = {
            "ticker": "SPY", "date": date(2024, 3, 15),
            "action": action, "value": 0.0,
        }
        assert _dispatch_action_row(row, AssetId(100)) is None


def test_dispatch_action_row_unknown_action_warns_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per Plan-reviewer Counter on Choice 1: unknown action codes log
    a WARNING and skip. Vendor adding a code mid-2027 must not crash
    backtests.
    """
    row = {
        "ticker": "SPY",
        "date": date(2024, 11, 15),
        "action": "vendor_added_code_xyz",
        "value": 0.0,
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.sources.sharadar"):
        result = _dispatch_action_row(row, AssetId(100))
    assert result is None
    assert any("vendor_added_code_xyz" in record.message for record in caplog.records)


# ----- get_corporate_actions tests -----

def test_get_corporate_actions_happy_path_returns_split_for_spy(
    tmp_path: Path,
) -> None:
    """SPY 2024-03-15 split (value=2.0) in a range covering it returns
    [SplitAction(ratio=Decimal("2.0"), ex_date=2024-03-15 16:00)].
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    actions = adapter.get_corporate_actions(
        AssetId(100),
        datetime(2024, 1, 1, 16, 0),
        datetime(2024, 12, 31, 16, 0),
    )
    assert len(actions) == 1
    assert isinstance(actions[0], SplitAction)
    assert actions[0].ratio == Decimal("2.0")
    assert actions[0].ex_date == datetime(2024, 3, 15, 16, 0)


def test_get_corporate_actions_filters_cash_flows_out(tmp_path: Path) -> None:
    """A range that contains SPY's 2024-03-15 dividend + split returns
    only the SplitAction (the dividend flows through get_cash_flows).
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    actions = adapter.get_corporate_actions(
        AssetId(100),
        datetime(2024, 3, 1, 16, 0),
        datetime(2024, 3, 31, 16, 0),
    )
    assert len(actions) == 1
    assert isinstance(actions[0], SplitAction)


def test_get_corporate_actions_empty_range_returns_empty_list(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    actions = adapter.get_corporate_actions(
        AssetId(100),
        datetime(2020, 1, 1, 16, 0),
        datetime(2020, 12, 31, 16, 0),
    )
    assert actions == []


def test_get_corporate_actions_unknown_asset_raises_ticker_not_found(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(TickerNotFoundError):
        adapter.get_corporate_actions(
            AssetId(999),
            datetime(2024, 1, 1, 16, 0),
            datetime(2024, 12, 31, 16, 0),
        )


# ----- get_cash_flows tests -----

def test_get_cash_flows_happy_path_returns_dividend_for_spy(
    tmp_path: Path,
) -> None:
    """SPY 2024-03-15 dividend (value=1.7715) in narrow range."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(100),
        datetime(2024, 3, 14, 16, 0),
        datetime(2024, 3, 16, 16, 0),
    )
    assert len(flows) == 1
    assert flows[0].flow_type == "cash_dividend"
    assert flows[0].amount == Decimal("1.7715")
    assert flows[0].dt == datetime(2024, 3, 15, 16, 0)


def test_get_cash_flows_spinoff_dispatches_to_spinoff_cash_equivalent(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(100),
        datetime(2024, 6, 1, 16, 0),
        datetime(2024, 6, 30, 16, 0),
    )
    assert len(flows) == 1
    assert flows[0].flow_type == "spinoff_cash_equivalent"
    assert flows[0].amount == Decimal("12.50")


def test_get_cash_flows_skipped_actions_excluded_from_result(
    tmp_path: Path,
) -> None:
    """The transfer row at 2024-09-30 is skipped; the range covering it
    returns only [].
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(100),
        datetime(2024, 9, 1, 16, 0),
        datetime(2024, 9, 30, 16, 0),
    )
    assert flows == []


def test_get_cash_flows_unknown_action_logs_warning_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fictitious_action row at 2024-11-15 logs a warning and is
    skipped. The method does NOT raise.
    """
    import logging
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with caplog.at_level(logging.WARNING, logger="pit_backtest.data.sources.sharadar"):
        flows = adapter.get_cash_flows(
            AssetId(100),
            datetime(2024, 11, 1, 16, 0),
            datetime(2024, 11, 30, 16, 0),
        )
    assert flows == []
    assert any("fictitious_action" in record.message for record in caplog.records)


def test_get_cash_flows_date_range_filter_excludes_rows_outside(
    tmp_path: Path,
) -> None:
    """The PR 2-style structural lookahead-leak analogue for the range query.

    A buggy implementation that drops the start_dt filter would return
    the 2023-12-15 dividend even though the range starts at 2024-03-14.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(100),
        datetime(2024, 3, 14, 16, 0),
        datetime(2024, 3, 16, 16, 0),
    )
    # The 2023-12-15 dividend exists in the bundle but is NOT in this range.
    for flow in flows:
        assert flow.dt >= datetime(2024, 3, 14, 16, 0)
        assert flow.dt <= datetime(2024, 3, 16, 16, 0)


def test_get_cash_flows_includes_delisting_cash_when_lastpricedate_in_range(
    tmp_path: Path,
) -> None:
    """For AssetId(400) (DLST, lastpricedate=2018-06-30, closeunadj=12.50)
    a range covering that date returns a delisting_cash_proceeds CashFlow.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(400),
        datetime(2018, 6, 1, 16, 0),
        datetime(2018, 12, 31, 16, 0),
    )
    delisting_flows = [f for f in flows if f.flow_type == "delisting_cash_proceeds"]
    assert len(delisting_flows) == 1
    assert delisting_flows[0].amount == Decimal("12.50")
    assert delisting_flows[0].dt == datetime(2018, 6, 30, 16, 0)


def test_get_cash_flows_excludes_delisting_cash_when_lastpricedate_outside_range(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(400),
        datetime(2017, 1, 1, 16, 0),
        datetime(2017, 12, 31, 16, 0),
    )
    delisting_flows = [f for f in flows if f.flow_type == "delisting_cash_proceeds"]
    assert delisting_flows == []


def test_get_cash_flows_sort_ordinal_dividend_before_delisting_same_dt(
    tmp_path: Path,
) -> None:
    """Per ADR 0003 dec 13: dividends apply at T (ex-date); delisting
    cash credits at open of T+1. Same-day grouping puts dividends BEFORE
    delisting cash so the explicit ordinal
    (cash_dividend=0, spinoff=1, delisting=2) survives even when v1.1
    adds new flow types that would break alphabetical tiebreaks.

    This test builds an inline bundle with DLST having a dividend AND
    a delisting on the same date.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_sortord"
    bundle_dir.mkdir(parents=True)

    sep_rows = [
        # M3 PR 5a: IPO-window SEP row so the first-price contract passes.
        {
            "ticker": "X", "date": date(2010, 1, 4),
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
            "closeunadj": 10.0, "volume": 5000,
        },
        {
            "ticker": "X", "date": date(2020, 6, 30),
            "open": 5.0, "high": 5.5, "low": 4.8, "close": 5.0,
            "closeunadj": 5.0, "volume": 1000,
        },
    ]
    actions_rows = [
        {"ticker": "X", "date": date(2020, 6, 30), "action": "dividend", "value": 0.25},
    ]
    tickers_rows = [
        {
            "permaticker": 999,
            "ticker": "X",
            "name": "Coincident Delisting Co",
            "exchange": "NASDAQ",
            "isdelisted": "Y",
            "firstpricedate": date(2010, 1, 4),
            "lastpricedate": date(2020, 6, 30),
            "firstquarter": date(2010, 3, 31),
            "lastquarter": date(2020, 6, 30),
            "cusip": "X00000001",
        },
    ]
    for table_name, rows in (
        ("sep", sep_rows), ("actions", actions_rows), ("tickers", tickers_rows)
    ):
        path = bundle_dir / f"{table_name}.parquet"
        pl.DataFrame(rows).write_parquet(path)

    sep_sha = hashlib.sha256((bundle_dir / "sep.parquet").read_bytes()).hexdigest()
    actions_sha = hashlib.sha256((bundle_dir / "actions.parquet").read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256((bundle_dir / "tickers.parquet").read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_sortord]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_sortord.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {(bundle_dir / "sep.parquet").stat().st_size}, row_count = {len(sep_rows)} }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {(bundle_dir / "actions.parquet").stat().st_size}, row_count = 1 }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {(bundle_dir / "tickers.parquet").stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_sortord", snapshots_root)

    flows = adapter.get_cash_flows(
        AssetId(999),
        datetime(2020, 6, 30, 16, 0),
        datetime(2020, 6, 30, 16, 0),
    )
    assert len(flows) == 2
    # cash_dividend (ordinal 0) before delisting_cash_proceeds (ordinal 2)
    assert flows[0].flow_type == "cash_dividend"
    assert flows[1].flow_type == "delisting_cash_proceeds"


# ----- get_delisting tests -----

def test_get_delisting_happy_path_returns_cash_flow_with_closeunadj_amount(
    tmp_path: Path,
) -> None:
    """AssetId(400) (DLST, lastpricedate=2018-06-30, closeunadj=12.50)
    returns CashFlow(flow_type="delisting_cash_proceeds", amount=12.50,
    dt=2018-06-30 16:00).
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    result = adapter.get_delisting(AssetId(400))
    assert isinstance(result, CashFlow)
    assert result.flow_type == "delisting_cash_proceeds"
    assert result.amount == Decimal("12.50")
    assert result.dt == datetime(2018, 6, 30, 16, 0)


def test_get_delisting_active_asset_returns_none(tmp_path: Path) -> None:
    """SPY (AssetId(100)) is still active (isdelisted='N', lastpricedate=None)."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    assert adapter.get_delisting(AssetId(100)) is None


def test_get_delisting_unknown_asset_raises_ticker_not_found(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(TickerNotFoundError):
        adapter.get_delisting(AssetId(999))


def test_get_delisting_missing_sep_row_raises_delisting_data_quality_error(
    tmp_path: Path,
) -> None:
    """A delisted asset whose lastpricedate has no SEP row triggers
    DelistingDataQualityError per the documented vendor-data-quality
    bug contract.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_dqissue"
    bundle_dir.mkdir(parents=True)

    # M3 PR 5a: a single SEP row at firstpricedate so the
    # FirstPriceWithinFiveDaysContract passes at __init__; the test's
    # point is the MISSING row at lastpricedate, not at firstpricedate.
    sep_rows: list[dict[str, object]] = [
        {
            "ticker": "MISSING", "date": date(2010, 1, 4),
            "open": 15.0, "high": 15.5, "low": 14.8, "close": 15.0,
            "closeunadj": 15.0, "volume": 2000,
        },
    ]
    tickers_rows = [
        {
            "permaticker": 500,
            "ticker": "MISSING",
            "name": "Missing Bar Co",
            "exchange": "NASDAQ",
            "isdelisted": "Y",
            "firstpricedate": date(2010, 1, 4),
            "lastpricedate": date(2015, 5, 15),
            "firstquarter": date(2010, 3, 31),
            "lastquarter": date(2015, 6, 30),
            "cusip": "M00000001",
        },
    ]
    pl.DataFrame(sep_rows).write_parquet(bundle_dir / "sep.parquet")
    pl.DataFrame(tickers_rows).write_parquet(bundle_dir / "tickers.parquet")

    sep_sha = hashlib.sha256((bundle_dir / "sep.parquet").read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256((bundle_dir / "tickers.parquet").read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_dqissue]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_dqissue.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {(bundle_dir / "sep.parquet").stat().st_size}, row_count = {len(sep_rows)} }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {(bundle_dir / "tickers.parquet").stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_dqissue", snapshots_root)

    with pytest.raises(DelistingDataQualityError) as exc_info:
        adapter.get_delisting(AssetId(500))
    message = str(exc_info.value)
    assert "MISSING" in message
    assert "2015-05-15" in message


def test_get_delisting_decimal_precision_on_non_clean_closeunadj(
    tmp_path: Path,
) -> None:
    """Decimal precision regression: closeunadj=517.51 round-trips via
    `to_boundary_decimal(repr(...))` to Decimal("517.51"), NOT the
    float64 binary expansion (Plan-reviewer High 5 pattern).
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_precdelisting"
    bundle_dir.mkdir(parents=True)

    sep_rows = [
        # M3 PR 5a: IPO-window SEP row so the first-price contract passes.
        {
            "ticker": "PR",
            "date": date(2010, 1, 4),
            "open": 50.0, "high": 50.5, "low": 49.8, "close": 50.0,
            "closeunadj": 50.0, "volume": 1000,
        },
        {
            "ticker": "PR",
            "date": date(2018, 6, 30),
            "open": 517.00, "high": 518.00, "low": 516.50,
            "close": 517.51, "closeunadj": 517.51,
            "volume": 1000,
        },
    ]
    tickers_rows = [
        {
            "permaticker": 600,
            "ticker": "PR",
            "name": "Precision Co",
            "exchange": "NASDAQ",
            "isdelisted": "Y",
            "firstpricedate": date(2010, 1, 4),
            "lastpricedate": date(2018, 6, 30),
            "firstquarter": date(2010, 3, 31),
            "lastquarter": date(2018, 6, 30),
            "cusip": "PR0000001",
        },
    ]
    pl.DataFrame(sep_rows).write_parquet(bundle_dir / "sep.parquet")
    pl.DataFrame(tickers_rows).write_parquet(bundle_dir / "tickers.parquet")

    sep_sha = hashlib.sha256((bundle_dir / "sep.parquet").read_bytes()).hexdigest()
    tickers_sha = hashlib.sha256((bundle_dir / "tickers.parquet").read_bytes()).hexdigest()
    manifest = f"""
[snapshots.sharadar_precdelisting]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_precdelisting.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {(bundle_dir / "sep.parquet").stat().st_size}, row_count = {len(sep_rows)} }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {(bundle_dir / "tickers.parquet").stat().st_size}, row_count = 1 }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_precdelisting", snapshots_root)

    result = adapter.get_delisting(AssetId(600))
    assert isinstance(result, CashFlow)
    assert result.amount == Decimal("517.51")
    # Binary-expansion path would NOT equal the dec literal.
    assert result.amount != Decimal(517.51)


# ----- read_actions general reader tests -----

def test_read_actions_default_returns_all_codes(tmp_path: Path) -> None:
    """No action_filter means all action codes pass through."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_actions(ticker="SPY")
    actions_seen = set(df["action"].to_list())
    assert "dividend" in actions_seen
    assert "split" in actions_seen
    assert "spinoff" in actions_seen
    assert "transfer" in actions_seen
    assert "fictitious_action" in actions_seen


def test_read_actions_action_filter_drops_non_matching_rows(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_actions(
        ticker="SPY", action_filter=frozenset({"dividend"})
    )
    assert set(df["action"].to_list()) == {"dividend"}


def test_read_actions_date_range_filter_applies(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_actions(
        ticker="SPY",
        start_dt=date(2024, 3, 1),
        end_dt=date(2024, 3, 31),
    )
    # Only the 2024-03-15 dividend + split are in the range.
    assert df.height == 2
    assert set(df["action"].to_list()) == {"dividend", "split"}


# ----- Resolver predicate test (moved to test_resolver.py would be better
# but kept here for proximity to the consumer).

def test_resolver_contains_returns_true_for_indexed_and_false_otherwise(
    tmp_path: Path,
) -> None:
    """Plan-reviewer ratified: public predicate to avoid private-field touch."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    assert adapter._resolver.contains(AssetId(100)) is True
    assert adapter._resolver.contains(AssetId(400)) is True
    assert adapter._resolver.contains(AssetId(999)) is False


# ============================================================
# M3 PR 4: SharadarSP500Universe + members_at tests
# ============================================================


def test_members_at_unknown_universe_id_raises_value_error(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(ValueError) as exc_info:
        adapter.members_at("russell_1000", datetime(2024, 1, 1, 16, 0))
    message = str(exc_info.value)
    assert "russell_1000" in message
    assert "'sp500'" in message


def test_members_at_sp500_returns_sorted_list_of_asset_ids(
    tmp_path: Path,
) -> None:
    """At 2012-01-01 SPY (100) + AGG (200) are both members (AGG in
    first interval 2010-2015). Sorted by int value."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    members = adapter.members_at("sp500", datetime(2012, 1, 1, 16, 0))
    assert members == [AssetId(100), AssetId(200)]


def test_members_at_sp500_after_agg_removed_excludes_agg(
    tmp_path: Path,
) -> None:
    """At 2016-01-01 AGG has been removed; only SPY remains."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    members = adapter.members_at("sp500", datetime(2016, 1, 1, 16, 0))
    assert members == [AssetId(100)]


def test_members_at_sp500_before_any_add_returns_empty_list(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    members = adapter.members_at("sp500", datetime(1989, 12, 31, 16, 0))
    assert members == []


def test_members_at_cached_property_single_construction(
    tmp_path: Path,
) -> None:
    """Two calls to members_at produce the same SharadarSP500Universe
    instance (the cached_property is consulted once per adapter).
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    adapter.members_at("sp500", datetime(2012, 1, 1, 16, 0))
    universe_1 = adapter._sp500_universe
    adapter.members_at("sp500", datetime(2016, 1, 1, 16, 0))
    universe_2 = adapter._sp500_universe
    assert universe_1 is universe_2


def test_members_at_sp500_only_bundle_missing_sp500_propagates_file_not_found(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer gotcha 6: a pre-M3-PR-4-era bundle (no
    sp500.parquet) raises FileNotFoundError, NOT an obscure
    cached_property error.
    """
    snapshots_root = _write_synthetic_bundle(
        tmp_path, tables=("sep", "actions", "tickers")
    )
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    with pytest.raises(FileNotFoundError) as exc_info:
        adapter.members_at("sp500", datetime(2012, 1, 1, 16, 0))
    assert "sp500.parquet" in str(exc_info.value)


def test_sharadar_sp500_universe_is_member_on_added_date_returns_true(
    tmp_path: Path,
) -> None:
    """Added date is the FIRST day of membership."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert universe.is_member(AssetId(200), datetime(2010, 6, 15, 16, 0)) is True


def test_sharadar_sp500_universe_is_member_on_removed_date_returns_true(
    tmp_path: Path,
) -> None:
    """Removed date is the LAST day of membership (inclusive)."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert (
        universe.is_member(AssetId(200), datetime(2015, 12, 31, 16, 0))
        is True
    )


def test_sharadar_sp500_universe_is_member_day_after_removed_returns_false(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert (
        universe.is_member(AssetId(200), datetime(2016, 1, 1, 16, 0))
        is False
    )


def test_sharadar_sp500_universe_is_member_day_before_added_returns_false(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert (
        universe.is_member(AssetId(200), datetime(2010, 6, 14, 16, 0))
        is False
    )


def test_sharadar_sp500_universe_is_member_unknown_asset_returns_false(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert (
        universe.is_member(AssetId(999), datetime(2012, 1, 1, 16, 0))
        is False
    )


def test_sharadar_sp500_universe_membership_spells_open_ended_returns_none(
    tmp_path: Path,
) -> None:
    """SPY added 1995-09-19 with no removed; spells return that pair."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    spells = universe.membership_spells(AssetId(100))
    assert spells == [(datetime(1995, 9, 19, 16, 0), None)]


def test_sharadar_sp500_universe_membership_spells_closed_interval(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    spells = universe.membership_spells(AssetId(200))
    assert spells == [
        (datetime(2010, 6, 15, 16, 0), datetime(2015, 12, 31, 16, 0)),
    ]


def test_sharadar_sp500_universe_membership_spells_unknown_asset_returns_empty(
    tmp_path: Path,
) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    assert universe.membership_spells(AssetId(999)) == []


def test_sharadar_sp500_universe_members_at_sorted_by_int_value(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Low 10: members_at must be sorted by int value
    even when the dict insertion order is perturbed. Defense in depth
    against future construction changes.
    """
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    members = adapter.members_at("sp500", datetime(2012, 1, 1, 16, 0))
    assert members == sorted(members, key=int)


def test_sharadar_sp500_universe_repr_surfaces_index_sizes(
    tmp_path: Path,
) -> None:
    """Per Plan-reviewer Low 9: __repr__ surfaces asset and interval counts."""
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    universe = adapter._sp500_universe
    repr_str = repr(universe)
    # SPY (1 interval) + AGG (1 closed interval) = 2 assets, 2 intervals
    assert "assets=2" in repr_str
    assert "intervals=2" in repr_str
    assert "sharadar_2026-05-28" in repr_str


def test_read_sp500_default_returns_all_events(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sp500()
    assert df.height == 3
    assert df.columns == ["ticker", "date", "action"]
    assert df.schema["date"] == pl.Date


def test_read_sp500_filters_by_ticker(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sp500(ticker="AGG")
    assert df.height == 2
    assert set(df["action"].to_list()) == {"added", "removed"}


def test_read_sp500_filters_by_action(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sp500(action="added")
    assert df.height == 2
    assert set(df["ticker"].to_list()) == {"SPY", "AGG"}


def test_read_sp500_filters_by_date_range(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sp500(
        start_dt=date(2010, 1, 1), end_dt=date(2015, 12, 31)
    )
    # AGG added 2010-06-15 + AGG removed 2015-12-31; SPY 1990 excluded.
    assert df.height == 2
    assert set(df["ticker"].to_list()) == {"AGG"}


def test_read_sp500_sorted_by_date_action_ticker(tmp_path: Path) -> None:
    snapshots_root = _write_synthetic_bundle(tmp_path, tables=_M3_TABLES)
    adapter = SharadarDataSource("sharadar_2026-05-28", snapshots_root)

    df = adapter.read_sp500()
    dates = df["date"].to_list()
    assert dates == [date(1995, 9, 19), date(2010, 6, 15), date(2015, 12, 31)]


def test_members_at_pit_discipline_excludes_future_added_via_inline_bundle(
    tmp_path: Path,
) -> None:
    """Structural PIT regression per project rule 2D.

    Inline bundle with an "added" event in 2026 for a synthetic ticker.
    members_at(2010-06-15) must NOT include the future-added asset;
    is_member at the same date must return False.
    """
    import hashlib

    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_pit"
    bundle_dir.mkdir(parents=True)

    sp500_rows = [
        # SPY added after its TICKERS firstpricedate (1993-01-22).
        {"ticker": "SPY", "date": date(1995, 9, 19), "action": "added"},
        # Future event the engine must NOT honor at 2010.
        {"ticker": "AGG", "date": date(2026, 1, 1), "action": "added"},
    ]
    tickers_rows = list(_TICKERS_ROWS)

    pl.DataFrame(sp500_rows).write_parquet(bundle_dir / "sp500.parquet")
    pl.DataFrame(tickers_rows).write_parquet(bundle_dir / "tickers.parquet")

    sp500_sha = hashlib.sha256(
        (bundle_dir / "sp500.parquet").read_bytes()
    ).hexdigest()
    tickers_sha = hashlib.sha256(
        (bundle_dir / "tickers.parquet").read_bytes()
    ).hexdigest()
    manifest = f"""
[snapshots.sharadar_pit]
source = "sharadar"
pull_date = 2026-05-30

[snapshots.sharadar_pit.files]
"sp500.parquet" = {{ sha256 = "{sp500_sha}", size_bytes = {(bundle_dir / 'sp500.parquet').stat().st_size}, row_count = 2 }}
"tickers.parquet" = {{ sha256 = "{tickers_sha}", size_bytes = {(bundle_dir / 'tickers.parquet').stat().st_size}, row_count = {len(tickers_rows)} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    adapter = SharadarDataSource("sharadar_pit", snapshots_root)

    members_2010 = adapter.members_at("sp500", datetime(2010, 6, 15, 16, 0))
    assert AssetId(100) in members_2010  # SPY (added 1990) IS a member
    assert AssetId(200) not in members_2010  # AGG (added 2026) is NOT
    assert (
        adapter._sp500_universe.is_member(
            AssetId(200), datetime(2010, 6, 15, 16, 0)
        )
        is False
    )
