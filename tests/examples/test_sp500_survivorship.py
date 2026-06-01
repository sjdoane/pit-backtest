"""Integration tests for `examples/sp500_survivorship.py` (M3 PR 5c).

Builds an inline 3-ticker synthetic bundle so the survivorship math is
hand-computable to 1e-4 precision; pins the four headline numbers
(pit_count, current_count, survivor_count, cagr_delta_bps), the exit
codes, and the Markdown render contract.

Fixture (SURV / DEAD / NEW) per Plan-reviewer Medium 8 worked example:
  - SURV (permaticker 1000): in both 2010 and 2025 SP500. SEP closeunadj
    = 100 at 2010-01-04 and 200 at 2025-01-03. Terminal TR = 2.0.
  - DEAD (permaticker 2000): in 2010 SP500 only. Delisted 2017-06-30
    with closeunadj = 50. Terminal TR = 0.5 held flat to as_of.
  - NEW (permaticker 3000): in 2025 SP500 only. SEP starts 2015-03-01;
    no 2010 price -> SKIPPED from current cohort per Plan-reviewer
    Choice B ratification.

Hand computation:
  - years = (2025-01-03 - 2010-01-04).days / 365.25 = 5478 / 365.25
    = 14.9979 (approx).
  - PIT cohort terminal TR = mean(2.0, 0.5) = 1.25.
  - PIT cohort CAGR = 1.25 ** (1/14.9979) - 1 = 1.499% approx.
  - Current cohort terminal TR = mean(2.0) = 2.0 (NEW skipped).
  - Current cohort CAGR = 2.0 ** (1/14.9979) - 1 = 4.731% approx.
  - CAGR delta = +3.232 pp = +323.2 bps approx.

Membership is expressed via the ADR 0017 snapshot model: a 2009-12-31
`historical` snapshot for the PIT cohort and a 2025-01-03 `current`
snapshot for the current cohort. The data quality contracts run at
`SharadarDataSource.__init__`; the fixture ships sep + actions + tickers +
sp500 (no sf1) so the two SF1 contracts skip; the five others (first-price,
no-sep-after-delisting, snapshot-resolve, no-duplicate-sp500, and the
added/removed cross-check) must pass on the rows below.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from examples.sp500_survivorship import (
    SurvivorshipReport,
    compute_survivorship_report,
    main,
    render_headline_markdown,
    render_verbose_markdown,
)
from pit_backtest.data.records import AssetId
from pit_backtest.data.sources.sharadar import SharadarDataSource


_PIT_DATE = date(2010, 1, 4)
_AS_OF_DATE = date(2025, 1, 3)
_BUNDLE_NAME = "sharadar_survivorship_test"
_BUNDLE_PULL_DATE = date.today() - timedelta(days=2)  # date-stable; never STALEs


_SURV_PERMATICKER = 1000
_DEAD_PERMATICKER = 2000
_NEW_PERMATICKER = 3000


def _build_survivorship_bundle(tmp_path: Path) -> Path:
    """Write the inline 3-ticker survivorship bundle and return snapshots_root."""
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / _BUNDLE_NAME
    bundle_dir.mkdir(parents=True)

    # SEP rows. SURV gets IPO-window + pit + as_of. DEAD gets IPO-window
    # + pit + delisting. NEW gets IPO-window + as_of.
    sep_rows: list[dict[str, object]] = [
        # SURV.
        {
            "ticker": "SURV", "date": date(2009, 9, 1),
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "closeunadj": 100.0, "volume": 1000,
        },
        {
            "ticker": "SURV", "date": _PIT_DATE,
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "closeunadj": 100.0, "volume": 1000,
        },
        {
            "ticker": "SURV", "date": _AS_OF_DATE,
            "open": 200.0, "high": 200.0, "low": 200.0,
            "close": 200.0, "closeunadj": 200.0, "volume": 1000,
        },
        # DEAD.
        {
            "ticker": "DEAD", "date": date(2009, 9, 1),
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "closeunadj": 100.0, "volume": 1000,
        },
        {
            "ticker": "DEAD", "date": _PIT_DATE,
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "closeunadj": 100.0, "volume": 1000,
        },
        {
            "ticker": "DEAD", "date": date(2017, 6, 30),
            "open": 50.0, "high": 50.0, "low": 50.0,
            "close": 50.0, "closeunadj": 50.0, "volume": 1000,
        },
        # NEW.
        {
            "ticker": "NEW", "date": date(2015, 3, 2),
            "open": 50.0, "high": 50.0, "low": 50.0,
            "close": 50.0, "closeunadj": 50.0, "volume": 1000,
        },
        {
            "ticker": "NEW", "date": _AS_OF_DATE,
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "closeunadj": 100.0, "volume": 1000,
        },
    ]

    actions_rows: list[dict[str, object]] = []  # No dividends or splits.

    tickers_rows = [
        {
            "permaticker": _SURV_PERMATICKER, "ticker": "SURV",
            "name": "Survivor Co", "exchange": "NYSE", "isdelisted": "N",
            "firstpricedate": date(2009, 9, 1), "lastpricedate": None,
            "firstquarter": date(2009, 9, 30), "lastquarter": None,
            "cusip": "SURV00001",
        },
        {
            "permaticker": _DEAD_PERMATICKER, "ticker": "DEAD",
            "name": "Delisted Co", "exchange": "NYSE", "isdelisted": "Y",
            "firstpricedate": date(2009, 9, 1),
            "lastpricedate": date(2017, 6, 30),
            "firstquarter": date(2009, 9, 30),
            "lastquarter": date(2017, 6, 30),
            "cusip": "DEAD00001",
        },
        {
            "permaticker": _NEW_PERMATICKER, "ticker": "NEW",
            "name": "Post-2010 IPO Co", "exchange": "NASDAQ",
            "isdelisted": "N",
            "firstpricedate": date(2015, 3, 2), "lastpricedate": None,
            "firstquarter": date(2015, 3, 31), "lastquarter": None,
            "cusip": "NEW000001",
        },
    ]

    # ADR 0017 snapshot model: membership comes from the historical/current
    # snapshots. A 2009-12-31 snapshot holds the PIT cohort (SURV + DEAD); a
    # 2025-01-03 current snapshot holds the current cohort (SURV + NEW; DEAD
    # has dropped). No added/removed events, so the cross-check has nothing
    # to reconcile.
    sp500_rows = [
        {"ticker": "SURV", "date": date(2009, 12, 31), "action": "historical"},
        {"ticker": "DEAD", "date": date(2009, 12, 31), "action": "historical"},
        {"ticker": "SURV", "date": _AS_OF_DATE, "action": "current"},
        {"ticker": "NEW", "date": _AS_OF_DATE, "action": "current"},
    ]

    # Write parquets + manifest. Empty ACTIONS still needs a parquet
    # file because manifest verification requires the file exists; we
    # provide a typed empty schema so Polars writes the file cleanly.
    files: dict[str, tuple[pl.DataFrame, int]] = {
        "sep": (pl.DataFrame(sep_rows), len(sep_rows)),
        "actions": (
            pl.DataFrame(
                actions_rows,
                schema={
                    "ticker": pl.String, "date": pl.Date,
                    "action": pl.String, "value": pl.Float64,
                },
            ),
            len(actions_rows),
        ),
        "tickers": (pl.DataFrame(tickers_rows), len(tickers_rows)),
        "sp500": (pl.DataFrame(sp500_rows), len(sp500_rows)),
    }

    file_lines: list[str] = []
    for table_name, (df, row_count) in files.items():
        path = bundle_dir / f"{table_name}.parquet"
        df.write_parquet(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        file_lines.append(
            f'"{table_name}.parquet" = {{ sha256 = "{sha}", '
            f"size_bytes = {size}, row_count = {row_count} }}"
        )

    files_block = "\n".join(file_lines)
    manifest_content = f"""
[snapshots.{_BUNDLE_NAME}]
source = "sharadar"
pull_date = {_BUNDLE_PULL_DATE.isoformat()}
notes = "synthetic 3-ticker survivorship fixture; M3 PR 5c"

[snapshots.{_BUNDLE_NAME}.files]
{files_block}
"""
    (snapshots_root / "manifest.toml").write_text(
        manifest_content, encoding="utf-8"
    )
    return snapshots_root


def _build_report(tmp_path: Path) -> SurvivorshipReport:
    snapshots_root = _build_survivorship_bundle(tmp_path)
    source = SharadarDataSource(_BUNDLE_NAME, snapshots_root)
    return compute_survivorship_report(source, _PIT_DATE, _AS_OF_DATE)


# ----- Headline-number assertions -----


def test_survivorship_report_pit_count_against_synthetic_bundle(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    # The 2009-12-31 snapshot (the most recent on or before 2010-01-04)
    # holds SURV + DEAD; NEW is not in it.
    assert report.pit_count == 2


def test_survivorship_report_current_count_against_synthetic_bundle(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    # The 2025-01-03 current snapshot holds SURV + NEW; DEAD has dropped.
    assert report.current_count == 2


def test_survivorship_report_survivor_count_against_synthetic_bundle(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    # Only SURV is in both cohorts.
    assert report.survivor_count == 1


def test_survivorship_report_delisted_pit_members_lists_dead(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    assert report.delisted_pit_members == (AssetId(_DEAD_PERMATICKER),)


def test_survivorship_report_skipped_current_lists_new(
    tmp_path: Path,
) -> None:
    """NEW has no 2010 price (TICKERS firstpricedate 2015-03-02 > pit
    date 2010-01-04), so the resolver returns None and NEW is appended
    to the skipped list per Plan-reviewer Choice B ratification.
    """
    report = _build_report(tmp_path)
    assert report.skipped_current_without_2010_price == (
        AssetId(_NEW_PERMATICKER),
    )


def test_survivorship_report_cagr_delta_matches_hand_computation(
    tmp_path: Path,
) -> None:
    """Plan-reviewer Medium 8 worked example. PIT cohort mean TR = 1.25
    (SURV 2.0, DEAD 0.5); current cohort mean TR = 2.0 (SURV only; NEW
    skipped). Years = 5478 / 365.25 = 14.9979. PIT CAGR = 1.499%;
    current CAGR = 4.731%; delta = +323.2 bps. Tolerance is +/- 0.5 bps
    to accommodate float-arithmetic across the years computation.
    """
    report = _build_report(tmp_path)
    assert report.pit_cohort_mean_terminal_tr == pytest.approx(1.25, abs=1e-9)
    assert report.current_cohort_mean_terminal_tr == pytest.approx(2.0, abs=1e-9)
    # Years rounded by float ops; pit_cagr roughly 1.499%, current 4.731%.
    assert report.pit_cohort_cagr == pytest.approx(0.014991, abs=1e-5)
    assert report.current_cohort_cagr == pytest.approx(0.047310, abs=1e-5)
    assert report.cagr_delta_bps == pytest.approx(323.2, abs=0.5)


def test_survivorship_report_years_matches_calendar_day_convention(
    tmp_path: Path,
) -> None:
    """Years = (as_of - pit_date).days / 365.25 per the calendar-day
    convention documented in `_cagr_from_terminal_tr` (Plan-reviewer
    Medium 6).
    """
    report = _build_report(tmp_path)
    expected = (_AS_OF_DATE - _PIT_DATE).days / 365.25
    assert report.years == pytest.approx(expected, abs=1e-9)


# ----- Render assertions -----


def test_render_headline_markdown_contains_four_headline_numbers(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    markdown = render_headline_markdown(report)
    assert "PIT S&P 500 count" in markdown
    assert "Current S&P 500 count" in markdown
    assert "Survivors (in both sets)" in markdown
    assert "CAGR delta (current minus PIT)" in markdown
    # Caveats present per Plan-reviewer High 4.
    assert "Buy-and-hold equal-weight" in markdown
    assert "Zero T-bill accrual" in markdown
    assert "Skipped current members" in markdown


def test_render_verbose_markdown_includes_audit_lists(
    tmp_path: Path,
) -> None:
    report = _build_report(tmp_path)
    verbose = render_verbose_markdown(report)
    assert "Delisted PIT members" in verbose
    assert str(_DEAD_PERMATICKER) in verbose
    assert "Skipped current members" in verbose
    assert str(_NEW_PERMATICKER) in verbose


# ----- CLI main() exit codes -----


def test_cli_missing_bundle_exits_2(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No bundle under snapshots_root; discover returns None; exit 2."""
    snapshots_root = tmp_path / "empty"
    snapshots_root.mkdir()
    with caplog.at_level(logging.ERROR):
        exit_code = main(
            argv=[
                "--bundle-prefix", "doesnotexist",
                "--snapshots-root", str(snapshots_root),
            ]
        )
    assert exit_code == 2


def test_cli_bundle_missing_m3_tables_exits_4(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bundle present but only sep + actions (M1 SPY-only shape);
    sp500.parquet and tickers.parquet missing; exit 4 per Plan-reviewer
    High 3 separation.
    """
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "sharadar_m1_only"
    bundle_dir.mkdir(parents=True)

    sep_rows = [
        {
            "ticker": "SPY", "date": date(2024, 3, 15),
            "open": 500.0, "high": 510.0, "low": 495.0,
            "close": 505.0, "closeunadj": 505.0, "volume": 100000,
        },
    ]
    actions_rows: list[dict[str, object]] = []

    files: dict[str, tuple[pl.DataFrame, int]] = {
        "sep": (pl.DataFrame(sep_rows), len(sep_rows)),
        "actions": (
            pl.DataFrame(
                actions_rows,
                schema={
                    "ticker": pl.String, "date": pl.Date,
                    "action": pl.String, "value": pl.Float64,
                },
            ),
            len(actions_rows),
        ),
    }
    file_lines: list[str] = []
    for table_name, (df, row_count) in files.items():
        path = bundle_dir / f"{table_name}.parquet"
        df.write_parquet(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        file_lines.append(
            f'"{table_name}.parquet" = {{ sha256 = "{sha}", '
            f"size_bytes = {size}, row_count = {row_count} }}"
        )
    files_block = "\n".join(file_lines)
    manifest = f"""
[snapshots.sharadar_m1_only]
source = "sharadar"
pull_date = 2026-05-28

[snapshots.sharadar_m1_only.files]
{files_block}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        exit_code = main(
            argv=[
                "--bundle", "sharadar_m1_only",
                "--snapshots-root", str(snapshots_root),
            ]
        )
    assert exit_code == 4


def test_cli_success_against_synthetic_survivorship_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end main() call on the synthetic bundle; exit 0 + stdout
    has the headline-numbers Markdown.
    """
    snapshots_root = _build_survivorship_bundle(tmp_path)
    exit_code = main(
        argv=[
            "--bundle", _BUNDLE_NAME,
            "--snapshots-root", str(snapshots_root),
            "--pit-date", _PIT_DATE.isoformat(),
            "--as-of", _AS_OF_DATE.isoformat(),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "S&P 500 Survivorship Study" in out
    assert "CAGR delta (current minus PIT)" in out


def test_cli_writes_to_output_path_when_given(
    tmp_path: Path,
) -> None:
    snapshots_root = _build_survivorship_bundle(tmp_path)
    output = tmp_path / "report.md"
    exit_code = main(
        argv=[
            "--bundle", _BUNDLE_NAME,
            "--snapshots-root", str(snapshots_root),
            "--pit-date", _PIT_DATE.isoformat(),
            "--as-of", _AS_OF_DATE.isoformat(),
            "--output", str(output),
        ]
    )
    assert exit_code == 0
    assert output.is_file()
    content = output.read_text(encoding="utf-8")
    assert "PIT S&P 500 count" in content
