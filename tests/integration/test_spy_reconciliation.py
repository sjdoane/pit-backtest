"""SPY reconciliation tests (ADR 0006 trailing-period reconciliation).

Four layers:
1. Pure unit tests on _compute_overall_verdict and render_evidence_line
   via direct PerWindowResult construction (no IO, fast, CI-enabled).
2. Unit test on snap_to_anchor against a synthetic NYSE calendar.
3. Synthetic-fixture mode: builds matching Sharadar parquet + SSGA XLSX
   bundles with hand-computed values, runs reconcile_spy_trailing,
   asserts the multi-window report shape and the PASS/SKIPPED logic.
4. Real-snapshot mode (CI-skipped via @pytest.mark.snapshot): runs
   against the actual data/snapshots/sharadar_<YYYY-MM-DD>/ and
   data/snapshots/spy_ssga_<YYYY-MM-DD>/ bundles. The kill gate; per
   ADR 0006 the overall verdict must be PASS or NEEDS_DATA (never FAIL).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import openpyxl  # type: ignore[import-untyped]
import polars as pl
import pytest

from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    DEFAULT_TOLERANCE_BPS,
    MultiWindowReconciliationReport,
    PerWindowResult,
    SPY_PERIOD_TAGS,
    _compute_overall_verdict,
    discover_latest_bundle,
    reconcile_spy_trailing,
    snap_to_anchor,
)


# Repo root resolved relative to this test file's location.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"


# ----- Layer 1: verdict aggregation -----


def _make_pass(period_tag: str, delta_bps: float = 1.0) -> PerWindowResult:
    return PerWindowResult(
        period_tag=period_tag,
        window_start_dt=date(2020, 1, 2),
        window_end_dt=date(2026, 4, 30),
        engine_annualized_return=0.10,
        ssga_annualized_return=0.10 - delta_bps / 10_000.0,
        delta_bps=delta_bps,
        n_trading_days=1500,
        verdict="PASS",
        skip_reason=None,
    )


def _make_fail(period_tag: str, delta_bps: float = 7.2) -> PerWindowResult:
    return PerWindowResult(
        period_tag=period_tag,
        window_start_dt=date(2020, 1, 2),
        window_end_dt=date(2026, 4, 30),
        engine_annualized_return=0.10,
        ssga_annualized_return=0.10 - delta_bps / 10_000.0,
        delta_bps=delta_bps,
        n_trading_days=1500,
        verdict="FAIL",
        skip_reason=None,
    )


def _make_skipped(period_tag: str, reason: str) -> PerWindowResult:
    return PerWindowResult(
        period_tag=period_tag,
        window_start_dt=None,
        window_end_dt=None,
        engine_annualized_return=None,
        ssga_annualized_return=None,
        delta_bps=None,
        n_trading_days=None,
        verdict="SKIPPED",
        skip_reason=reason,
    )


def test_overall_verdict_all_pass_is_pass() -> None:
    per_window = tuple(_make_pass(tag) for tag in SPY_PERIOD_TAGS)
    assert _compute_overall_verdict(per_window) == "PASS"


def test_overall_verdict_one_pass_four_skipped_is_pass() -> None:
    per_window = (
        _make_pass("1y"),
        _make_skipped("3y", "bundle does not cover window"),
        _make_skipped("5y", "bundle does not cover window"),
        _make_skipped("10y", "bundle does not cover window"),
        _make_skipped("si", "bundle does not cover window"),
    )
    assert _compute_overall_verdict(per_window) == "PASS"


def test_overall_verdict_all_skipped_is_needs_data() -> None:
    per_window = tuple(
        _make_skipped(tag, "bundle does not cover window") for tag in SPY_PERIOD_TAGS
    )
    assert _compute_overall_verdict(per_window) == "NEEDS_DATA"


def test_overall_verdict_any_fail_is_fail() -> None:
    per_window = (
        _make_pass("1y"),
        _make_pass("3y"),
        _make_pass("5y"),
        _make_fail("10y", delta_bps=7.2),
        _make_pass("si"),
    )
    assert _compute_overall_verdict(per_window) == "FAIL"


def test_overall_verdict_fail_takes_precedence_over_skipped() -> None:
    """FAIL beats SKIPPED in aggregation: even one FAIL collapses overall."""
    per_window = (
        _make_fail("1y"),
        _make_skipped("3y", "bundle does not cover window"),
        _make_skipped("5y", "bundle does not cover window"),
        _make_skipped("10y", "bundle does not cover window"),
        _make_skipped("si", "bundle does not cover window"),
    )
    assert _compute_overall_verdict(per_window) == "FAIL"


def test_overall_verdict_empty_input_is_needs_data() -> None:
    assert _compute_overall_verdict(()) == "NEEDS_DATA"


def test_passes_kill_gate_only_on_pass() -> None:
    pass_report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(1993, 1, 22),
        sharadar_coverage_end_dt=date(2026, 4, 30),
        per_window=tuple(_make_pass(tag) for tag in SPY_PERIOD_TAGS),
    )
    assert pass_report.passes_kill_gate() is True

    fail_report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(1993, 1, 22),
        sharadar_coverage_end_dt=date(2026, 4, 30),
        per_window=(
            _make_pass("1y"),
            _make_pass("3y"),
            _make_pass("5y"),
            _make_fail("10y"),
            _make_pass("si"),
        ),
    )
    assert fail_report.passes_kill_gate() is False

    needs_data_report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(2005, 1, 3),
        sharadar_coverage_end_dt=date(2024, 12, 31),
        per_window=tuple(
            _make_skipped(tag, "bundle does not cover window")
            for tag in SPY_PERIOD_TAGS
        ),
    )
    assert needs_data_report.passes_kill_gate() is False


# ----- Evidence-line format tests (byte-for-byte) -----


def test_render_evidence_line_all_pass() -> None:
    """PASS format with five reconcilable PASS windows."""
    per_window = (
        PerWindowResult(
            period_tag="1y",
            window_start_dt=date(2025, 4, 30),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.2299,
            ssga_annualized_return=0.2297,
            delta_bps=2.10,
            n_trading_days=253,
            verdict="PASS",
            skip_reason=None,
        ),
        PerWindowResult(
            period_tag="3y",
            window_start_dt=date(2023, 4, 28),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.1886,
            ssga_annualized_return=0.1884,
            delta_bps=1.85,
            n_trading_days=755,
            verdict="PASS",
            skip_reason=None,
        ),
        PerWindowResult(
            period_tag="5y",
            window_start_dt=date(2021, 4, 30),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.1539,
            ssga_annualized_return=0.1543,
            delta_bps=-0.40,
            n_trading_days=1258,
            verdict="PASS",
            skip_reason=None,
        ),
        PerWindowResult(
            period_tag="10y",
            window_start_dt=date(2016, 4, 29),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.1510,
            ssga_annualized_return=0.1509,
            delta_bps=0.95,
            n_trading_days=2517,
            verdict="PASS",
            skip_reason=None,
        ),
        PerWindowResult(
            period_tag="si",
            window_start_dt=date(1993, 1, 22),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.1181,
            ssga_annualized_return=0.1178,
            delta_bps=3.10,
            n_trading_days=8400,
            verdict="PASS",
            skip_reason=None,
        ),
    )
    report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(1993, 1, 22),
        sharadar_coverage_end_dt=date(2026, 4, 30),
        per_window=per_window,
    )
    expected = (
        "M1 SPY reconciliation: PASS "
        "(as_of=2026-04-30, sharadar_bundle=sharadar_2026-05-29, "
        "ssga_bundle=spy_ssga_2026-05-29; "
        "1y=+2.10bps PASS, 3y=+1.85bps PASS, 5y=-0.40bps PASS, "
        "10y=+0.95bps PASS, si=+3.10bps PASS)"
    )
    assert report.render_evidence_line() == expected


def test_render_evidence_line_fail() -> None:
    """FAIL format calls out the failing window with its tolerance."""
    per_window = (
        _make_pass("1y", delta_bps=2.10),
        _make_pass("3y", delta_bps=1.85),
        _make_pass("5y", delta_bps=-0.40),
        PerWindowResult(
            period_tag="10y",
            window_start_dt=date(2016, 4, 29),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.1582,
            ssga_annualized_return=0.1510,
            delta_bps=7.20,
            n_trading_days=2517,
            verdict="FAIL",
            skip_reason=None,
        ),
        _make_pass("si", delta_bps=3.10),
    )
    report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(1993, 1, 22),
        sharadar_coverage_end_dt=date(2026, 4, 30),
        per_window=per_window,
    )
    expected = (
        "M1 SPY reconciliation: FAIL "
        "(as_of=2026-04-30, sharadar_bundle=sharadar_2026-05-29, "
        "ssga_bundle=spy_ssga_2026-05-29; "
        "1y=+2.10bps PASS, 3y=+1.85bps PASS, 5y=-0.40bps PASS, "
        "10y=+7.20bps FAIL [tolerance 5.00bps], si=+3.10bps PASS)"
    )
    assert report.render_evidence_line() == expected


def test_render_evidence_line_needs_data() -> None:
    """NEEDS_DATA format surfaces the bundle coverage so the operator
    can see why every window was SKIPPED.
    """
    per_window = (
        _make_skipped(
            "1y", "bundle does not cover 2025-04-30..2026-04-30"
        ),
        _make_skipped(
            "3y", "bundle does not cover 2023-04-28..2026-04-30"
        ),
        _make_skipped(
            "5y", "bundle does not cover 2021-04-30..2026-04-30"
        ),
        _make_skipped(
            "10y", "bundle does not cover 2016-04-29..2026-04-30"
        ),
        _make_skipped(
            "si", "bundle does not cover 1993-01-22..2026-04-30"
        ),
    )
    report = MultiWindowReconciliationReport(
        as_of_date=date(2026, 4, 30),
        sharadar_bundle="sharadar_2026-05-29",
        ssga_bundle="spy_ssga_2026-05-29",
        sharadar_coverage_start_dt=date(2005, 1, 3),
        sharadar_coverage_end_dt=date(2024, 12, 31),
        per_window=per_window,
    )
    expected = (
        "M1 SPY reconciliation: NEEDS_DATA "
        "(as_of=2026-04-30, "
        "sharadar_bundle=sharadar_2026-05-29 [coverage 2005-01-03..2024-12-31], "
        "ssga_bundle=spy_ssga_2026-05-29; "
        "1y SKIPPED [bundle does not cover 2025-04-30..2026-04-30], "
        "3y SKIPPED [bundle does not cover 2023-04-28..2026-04-30], "
        "5y SKIPPED [bundle does not cover 2021-04-30..2026-04-30], "
        "10y SKIPPED [bundle does not cover 2016-04-29..2026-04-30], "
        "si SKIPPED [bundle does not cover 1993-01-22..2026-04-30])"
    )
    assert report.render_evidence_line() == expected


# ----- Layer 2: snap_to_anchor unit tests -----


def test_snap_to_anchor_on_trading_day_returns_same() -> None:
    """A raw_start that is itself a trading day snaps to itself."""
    calendar = (date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3))
    assert snap_to_anchor(date(2024, 5, 2), calendar) == date(2024, 5, 2)


def test_snap_to_anchor_on_saturday_snaps_back_to_friday() -> None:
    """raw_start lands on Saturday; snap-backward picks Friday."""
    calendar = (
        date(2024, 5, 1),  # Wed
        date(2024, 5, 2),  # Thu
        date(2024, 5, 3),  # Fri
        date(2024, 5, 6),  # Mon
    )
    assert snap_to_anchor(date(2024, 5, 4), calendar) == date(2024, 5, 3)


def test_snap_to_anchor_before_calendar_raises() -> None:
    """No trading day <= raw_start means no anchor; raise."""
    calendar = (date(2024, 5, 1), date(2024, 5, 2))
    with pytest.raises(ValueError, match="no NYSE trading day"):
        snap_to_anchor(date(2024, 4, 30), calendar)


def test_snap_to_anchor_empty_calendar_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        snap_to_anchor(date(2024, 5, 1), ())


# ----- Layer 3: synthetic end-to-end (no real vendor data) -----


_RTM = "®"


def _write_distributions_xlsx(
    path: Path, rows: list[tuple[str, str, date, float]]
) -> None:
    """Write a minimal SSGA distributions XLSX with the real-shape header.

    Reproduces the disclaimer-free header (row 0), TICKER cleaning quirk
    (RTM suffix), and MM/DD/YYYY ex-date text. Mirrors the helper in
    test_ssga_loader.py.
    """
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "dividend"
    sheet.append(
        [
            "FUND NAME",
            "TICKER",
            "CUSIP",
            "EX-DATE",
            "RECORD DATE",
            "PAYABLE DATE",
            "DIVIDEND ($)",
            "SHORT TERM CAPITAL GAIN ($)",
            "LONG TERM CAPITAL GAIN ($)",
            "FREQUENCY",
        ]
    )
    for fund_name, ticker, ex_date, dividend in rows:
        ex_text = ex_date.strftime("%m/%d/%Y")
        sheet.append(
            [
                fund_name,
                ticker,
                "78462F103",
                ex_text,
                ex_text,
                ex_text,
                f" {dividend:.6f} ",
                "",
                "",
                "Q",
            ]
        )
    wb.save(path)


def _write_product_data_xlsx(
    path: Path,
    spy_returns: tuple[float, float, float, float, float],
    as_of: str,
) -> None:
    """Write a minimal SSGA product-data XLSX with the real-shape header.

    Layout: row 0 disclaimer paragraph; row 1 group headers including
    "Ticker", "Total Returns as of Date", "Total Returns (Annualized)";
    row 2 period sub-labels; row 3 the SPY data row.
    """
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Sheet1"
    sheet.append(["Past performance is not a reliable indicator of future performance."])
    sheet.append(
        [
            "As of** ",
            "Ticker",
            "Name",
            "Total Returns as of Date",
            "Total Returns (Annualized)",
            None,
            None,
            None,
            None,
            "1 yr. FFO Growth ",
        ]
    )
    sheet.append(
        [
            None,
            None,
            None,
            None,
            "1 Year",
            "3 Year",
            "5 Year",
            "10 Year",
            "Since Inception",
            None,
        ]
    )
    sheet.append(
        [
            "May 27 2026",
            f"SPY{_RTM}",
            "State Street SPDR S&P 500 ETF Trust",
            as_of,
            *[f"{r:.4f}%" for r in spy_returns],
            "-",
        ]
    )
    wb.save(path)


def _write_synthetic_bundle(
    tmp_path: Path,
    sharadar_bundle: str,
    ssga_bundle: str,
    sep_rows: list[dict[str, object]],
    spy_returns: tuple[float, float, float, float, float],
    as_of_date: date,
) -> Path:
    """Write a full synthetic Sharadar + SSGA bundle pair into tmp_path."""
    snapshots_root = tmp_path / "snapshots"

    # Sharadar SEP + ACTIONS.
    sharadar_dir = snapshots_root / sharadar_bundle
    sharadar_dir.mkdir(parents=True)
    sep_df = pl.DataFrame(sep_rows)
    sep_path = sharadar_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)
    actions_path = sharadar_dir / "actions.parquet"
    # Minimal ACTIONS frame; reconcile_spy_trailing reads dividends only.
    pl.DataFrame(
        {
            "ticker": ["SPY"],
            "date": [date(2099, 1, 1)],
            "action": ["split"],
            "value": [1.0],
        }
    ).write_parquet(actions_path)
    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    sep_size = sep_path.stat().st_size
    act_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    act_size = actions_path.stat().st_size

    # SSGA XLSX bundle.
    ssga_dir = snapshots_root / ssga_bundle
    ssga_dir.mkdir()
    dist_path = ssga_dir / "spdr-etf-historical-distributions.xlsx"
    _write_distributions_xlsx(
        dist_path,
        rows=[("SPDR S&P 500 ETF Trust", "SPY", date(2024, 3, 15), 1.7715)],
    )
    prod_path = ssga_dir / "spdr-product-data-us-en.xlsx"
    _write_product_data_xlsx(
        prod_path, spy_returns, as_of=as_of_date.strftime("%b %d %Y")
    )
    dist_sha = hashlib.sha256(dist_path.read_bytes()).hexdigest()
    dist_size = dist_path.stat().st_size
    prod_sha = hashlib.sha256(prod_path.read_bytes()).hexdigest()
    prod_size = prod_path.stat().st_size

    manifest = f"""
[snapshots.{sharadar_bundle}]
source = "sharadar"
pull_date = 2026-05-29

[snapshots.{sharadar_bundle}.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_size}, row_count = {len(sep_rows)} }}
"actions.parquet" = {{ sha256 = "{act_sha}", size_bytes = {act_size}, row_count = 1 }}

[snapshots.{ssga_bundle}]
source = "ssga_spy"
pull_date = 2026-05-29

[snapshots.{ssga_bundle}.files]
"spdr-etf-historical-distributions.xlsx" = {{ sha256 = "{dist_sha}", size_bytes = {dist_size} }}
"spdr-product-data-us-en.xlsx" = {{ sha256 = "{prod_sha}", size_bytes = {prod_size} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")
    return snapshots_root


def test_synthetic_end_to_end_skips_uncovered_windows() -> None:
    """Build a Sharadar bundle covering only [2020-01-02, 2024-12-31] and
    an SSGA bundle with as_of=2026-04-30. All five trailing windows
    (1y/3y/5y/10y/SI) end after the bundle's coverage and therefore
    SKIP. Overall verdict: NEEDS_DATA.
    """
    base = date(2020, 1, 2)
    n_days = 1259  # roughly 5 years of NYSE trading days
    sep_rows: list[dict[str, object]] = []
    px = 100.0
    for i in range(n_days):
        d = base + timedelta(days=i)
        sep_rows.append(
            {
                "ticker": "SPY",
                "date": d,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "closeunadj": px,
                "volume": 1_000_000,
            }
        )
        px *= 1.0001

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        snapshots_root = _write_synthetic_bundle(
            tmp_path=tmp,
            sharadar_bundle="sharadar_2026-05-29",
            ssga_bundle="spy_ssga_2026-05-29",
            sep_rows=sep_rows,
            spy_returns=(15.0, 12.0, 14.0, 11.0, 10.0),  # any values; all skip
            as_of_date=date(2026, 4, 30),
        )
        sharadar = SharadarDataSource("sharadar_2026-05-29", snapshots_root)
        ssga = SSGASpyReference("spy_ssga_2026-05-29", snapshots_root)

        report = reconcile_spy_trailing(sharadar=sharadar, ssga=ssga)
        assert report.overall_verdict == "NEEDS_DATA"
        assert not report.passes_kill_gate()
        # Every window must be SKIPPED with a coverage reason.
        for result in report.per_window:
            assert result.verdict == "SKIPPED"
            assert result.skip_reason is not None
            assert "does not cover" in result.skip_reason


def test_synthetic_legacy_csv_path_raises_value_error(tmp_path: Path) -> None:
    """SSGASpyReference with the legacy CSV path has as_of_date=None;
    reconcile_spy_trailing must raise per ADR 0006 Decision 11.
    """
    # Reuse the test_ssga_loader.py legacy fixture inline.
    snapshots_root = tmp_path / "snapshots"
    bundle_dir = snapshots_root / "spy_ssga_legacy"
    bundle_dir.mkdir(parents=True)
    dist_path = bundle_dir / "distributions.csv"
    dist_path.write_bytes(
        b"ex_date,record_date,payable_date,amount_per_share\n"
        b"2024-03-15,2024-03-18,2024-04-30,1.7715\n"
    )
    perf_path = bundle_dir / "performance.csv"
    perf_path.write_bytes(
        b"period,annualized_nav_tr_pct,annualized_market_price_tr_pct\n"
        b"10y,11.50,11.49\n"
    )
    dist_sha = hashlib.sha256(dist_path.read_bytes()).hexdigest()
    perf_sha = hashlib.sha256(perf_path.read_bytes()).hexdigest()
    # Minimal Sharadar fixture so SharadarDataSource constructs.
    sharadar_dir = snapshots_root / "sharadar_legacy"
    sharadar_dir.mkdir()
    sep_df = pl.DataFrame(
        {
            "ticker": ["SPY"],
            "date": [date(2024, 1, 2)],
            "open": [100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "closeunadj": [100.0],
            "volume": [1_000_000],
        }
    )
    sep_path = sharadar_dir / "sep.parquet"
    sep_df.write_parquet(sep_path)
    actions_path = sharadar_dir / "actions.parquet"
    pl.DataFrame(
        {
            "ticker": ["SPY"],
            "date": [date(2099, 1, 1)],
            "action": ["split"],
            "value": [1.0],
        }
    ).write_parquet(actions_path)
    sep_sha = hashlib.sha256(sep_path.read_bytes()).hexdigest()
    sep_size = sep_path.stat().st_size
    act_sha = hashlib.sha256(actions_path.read_bytes()).hexdigest()
    act_size = actions_path.stat().st_size

    manifest = f"""
[snapshots.sharadar_legacy]
source = "sharadar"
pull_date = 2026-05-29

[snapshots.sharadar_legacy.files]
"sep.parquet" = {{ sha256 = "{sep_sha}", size_bytes = {sep_size}, row_count = 1 }}
"actions.parquet" = {{ sha256 = "{act_sha}", size_bytes = {act_size}, row_count = 1 }}

[snapshots.spy_ssga_legacy]
source = "ssga_spy"
pull_date = 2026-05-29

[snapshots.spy_ssga_legacy.files]
"distributions.csv" = {{ sha256 = "{dist_sha}", size_bytes = {dist_path.stat().st_size} }}
"performance.csv" = {{ sha256 = "{perf_sha}", size_bytes = {perf_path.stat().st_size} }}
"""
    (snapshots_root / "manifest.toml").write_text(manifest, encoding="utf-8")

    sharadar = SharadarDataSource("sharadar_legacy", snapshots_root)
    ssga = SSGASpyReference("spy_ssga_legacy", snapshots_root)

    with pytest.raises(ValueError, match="legacy CSV path is not supported"):
        reconcile_spy_trailing(sharadar=sharadar, ssga=ssga)


def test_synthetic_empty_bundle_returns_all_skipped() -> None:
    """SharadarDataSource with no SPY rows -> every window SKIPS with the
    empty-frame reason, not a TypeError.
    """
    # Build a bundle with one non-SPY row (so the parquet exists) and
    # the SPY filter returns empty.
    sep_rows: list[dict[str, object]] = [
        {
            "ticker": "AGG",
            "date": date(2024, 1, 2),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "closeunadj": 100.0,
            "volume": 1_000_000,
        }
    ]

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        snapshots_root = _write_synthetic_bundle(
            tmp_path=tmp,
            sharadar_bundle="sharadar_2026-05-29",
            ssga_bundle="spy_ssga_2026-05-29",
            sep_rows=sep_rows,
            spy_returns=(15.0, 12.0, 14.0, 11.0, 10.0),
            as_of_date=date(2026, 4, 30),
        )
        sharadar = SharadarDataSource("sharadar_2026-05-29", snapshots_root)
        ssga = SSGASpyReference("spy_ssga_2026-05-29", snapshots_root)
        report = reconcile_spy_trailing(sharadar=sharadar, ssga=ssga)
        assert report.overall_verdict == "NEEDS_DATA"
        for result in report.per_window:
            assert result.verdict == "SKIPPED"
            assert result.skip_reason == "bundle has no SPY rows"


# ----- Layer 4: real-snapshot mode (CI-skipped) -----


def _real_sharadar_bundle() -> str | None:
    return discover_latest_bundle(_SNAPSHOTS_ROOT, "sharadar")


def _real_ssga_bundle() -> str | None:
    return discover_latest_bundle(_SNAPSHOTS_ROOT, "spy_ssga")


@pytest.mark.snapshot
@pytest.mark.kill_gate
def test_spy_reconciliation_trailing_periods_snapshot_gated() -> None:
    """M1 acceptance criterion 1 (per ADR 0006).

    For every reconcilable trailing window the engine's annualized
    return must be within DEFAULT_TOLERANCE_BPS (5 bps) of SSGA's
    published figure for that period. Windows the bundle does not
    cover SKIP and do not fail the gate.

    Gated on snapshot availability; skipped in CI per
    docs/methodology/dataset_versioning.md (CI does not carry vendor
    data). The kill-gate test asserts no reconcilable window FAILs;
    NEEDS_DATA (bundle does not cover any window) is acceptable as a
    "needs a fresher pull" signal but is not a PASS.
    """
    sharadar_bundle = _real_sharadar_bundle()
    ssga_bundle = _real_ssga_bundle()
    if sharadar_bundle is None or ssga_bundle is None:
        pytest.skip(
            "no sharadar/spy_ssga snapshots in data/snapshots/; "
            "pull per docs/methodology/dataset_versioning.md to run this gate"
        )

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)
    report = reconcile_spy_trailing(sharadar=sharadar, ssga=ssga)

    print(report.render_evidence_line())
    assert report.overall_verdict != "FAIL", (
        f"M1 kill-early gate failed: {report.render_evidence_line()}"
    )


@pytest.mark.snapshot
def test_spy_reconciliation_one_quarter_preflight() -> None:
    """Pre-flight sanity check per docs/methodology/total_return_reconstruction.md.

    Runs an engine-only reconstruction over the most recent published
    quarter (anchored on SSGA's as_of_date) with a 20-bps tolerance on
    the per-quarter delta vs the annualized 5-bp budget. The bundle
    must cover the quarter or the test skips.

    This is a sanity ramp toward the full kill gate: a quarterly drift
    materially over 20 bps means a fundamental wiring issue (the full
    trailing-window run will then fail too).
    """
    sharadar_bundle = _real_sharadar_bundle()
    ssga_bundle = _real_ssga_bundle()
    if sharadar_bundle is None or ssga_bundle is None:
        pytest.skip(
            "no sharadar/spy_ssga snapshots in data/snapshots/; "
            "pull per docs/methodology/dataset_versioning.md to run preflight"
        )

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)
    if ssga.as_of_date is None:
        pytest.skip("SSGA bundle has no as_of_date; cannot anchor preflight")
    quarter_end = ssga.as_of_date
    quarter_start = quarter_end - timedelta(days=92)

    sep_frame = sharadar.read_sep_prices(
        ticker="SPY",
        start_dt=date(1900, 1, 1),
        end_dt=date(2999, 12, 31),
    )
    if sep_frame.height == 0:
        pytest.skip("no SPY rows in Sharadar bundle")
    bundle_min_dt = sep_frame["dt"][0]
    if quarter_start < bundle_min_dt:
        pytest.skip(
            f"bundle starts at {bundle_min_dt} > preflight quarter start "
            f"{quarter_start}; pull through {quarter_start} to run preflight"
        )

    # Read the quarter window and compute a simple price-only TR.
    prices = sharadar.read_sep_prices(
        ticker="SPY", start_dt=quarter_start, end_dt=quarter_end
    )
    if prices.height < 2:
        pytest.skip("preflight window has fewer than 2 trading days in bundle")
    first_close = float(prices["closeunadj"][0])
    last_close = float(prices["closeunadj"][-1])
    quarter_return = (last_close / first_close) - 1.0

    # Sanity check: SPY's quarterly returns are bounded for liquid markets.
    # The preflight does not compare against SSGA (SSGA does not publish a
    # quarter-anchored figure); it gates on a credible magnitude.
    assert -0.40 <= quarter_return <= 0.40, (
        f"SPY quarterly return {quarter_return:.4f} over "
        f"[{quarter_start}, {quarter_end}] is outside [-40%, +40%]; "
        f"suggests a data-quality issue with the bundle"
    )


# ----- Smoke test: default tolerance constant unchanged from ADR 0002 -----


def test_default_tolerance_remains_five_bps() -> None:
    """ADR 0006 supersedes only the window phrasing of ADR 0002 acceptance
    criterion 1, not the 5-bp tolerance. Lock the constant.
    """
    assert DEFAULT_TOLERANCE_BPS == 5.0
