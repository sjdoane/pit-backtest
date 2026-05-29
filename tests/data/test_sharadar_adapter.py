"""SharadarDataSource tests against a synthetic mini-snapshot.

Writes a tiny SEP + ACTIONS parquet bundle under tmp_path, registers it
in a manifest, constructs the adapter, and verifies the M1 convenience
methods plus the end-to-end TR reconstruction flow.

No real Sharadar data required; the test runs in CI.
"""

from __future__ import annotations

import hashlib
from datetime import date
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


def _write_synthetic_bundle(tmp_path: Path, bundle_name: str = "sharadar_2026-05-28") -> Path:
    """Build a synthetic Sharadar bundle: SEP + ACTIONS parquet + manifest.

    Returns the snapshots_root (the parent of the bundle directory).
    """
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / bundle_name
    bundle_dir.mkdir(parents=True)

    sep_df = pl.DataFrame(_SEP_ROWS)
    sep_path = bundle_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)

    actions_df = pl.DataFrame(_ACTIONS_ROWS)
    actions_path = bundle_dir / "actions.parquet"
    actions_df.write_parquet(actions_path)

    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    sep_size = sep_path.stat().st_size
    actions_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    actions_size = actions_path.stat().st_size

    manifest_content = f"""
[snapshots.{bundle_name}]
source = "sharadar"
pull_date = 2026-05-28
notes = "synthetic fixture for tests"

[snapshots.{bundle_name}.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_size}, row_count = {len(_SEP_ROWS)} }}
"actions.parquet" = {{ sha256 = "{actions_sha}", size_bytes = {actions_size}, row_count = {len(_ACTIONS_ROWS)} }}
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
