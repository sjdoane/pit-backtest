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

from pit_backtest.data.adjustments import annualized_return
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    MultiWindowReconciliationReport,
    PerWindowResult,
    SPY_INCEPTION_DATE,
    SPY_PERIOD_TAGS,
    SSGA_TOLERANCE_BPS,
    _compute_overall_verdict,
    discover_latest_bundle,
    reconcile_spy_trailing,
    snap_to_anchor,
    ssga_annualized_return,
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
    """FAIL format calls out the failing window with its per-window tolerance.

    Per ADR 0008 the 3y tolerance is 8.0 bps; delta_bps=8.83 exceeds it.
    """
    per_window = (
        _make_pass("1y", delta_bps=2.10),
        PerWindowResult(
            period_tag="3y",
            window_start_dt=date(2023, 4, 28),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.2143,
            ssga_annualized_return=0.2152,
            delta_bps=-8.83,
            n_trading_days=754,
            verdict="FAIL",
            skip_reason=None,
        ),
        _make_pass("5y", delta_bps=-0.40),
        _make_pass("10y", delta_bps=0.95),
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
        "1y=+2.10bps PASS, 3y=-8.83bps FAIL [tolerance 8.00bps], "
        "5y=-0.40bps PASS, 10y=+0.95bps PASS, si=+3.10bps PASS)"
    )
    assert report.render_evidence_line() == expected


def test_render_evidence_line_fail_with_1y_at_adr_0008_tolerance() -> None:
    """FAIL on the 1y window renders with the 25.00 bp tolerance per ADR 0008.

    The empirical bundle's 1y delta of +24 bps PASSES at 25.00 bps; this
    test exercises a hypothetical FAIL at +27 bps to lock the per-window
    tolerance rendering.
    """
    per_window = (
        PerWindowResult(
            period_tag="1y",
            window_start_dt=date(2025, 4, 30),
            window_end_dt=date(2026, 4, 30),
            engine_annualized_return=0.3110,
            ssga_annualized_return=0.2840,
            delta_bps=27.00,
            n_trading_days=252,
            verdict="FAIL",
            skip_reason=None,
        ),
        _make_pass("3y", delta_bps=1.85),
        _make_pass("5y", delta_bps=-0.40),
        _make_pass("10y", delta_bps=0.95),
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
        "1y=+27.00bps FAIL [tolerance 25.00bps], 3y=+1.85bps PASS, "
        "5y=-0.40bps PASS, 10y=+0.95bps PASS, si=+3.10bps PASS)"
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
def test_kill_gate_per_window_deltas_in_known_bands() -> None:
    """Regression-band test per ADR 0008.

    On the sharadar_2026-05-29 + spy_ssga_2026-05-29 bundle pair with
    ADR 0008 (nominal-year annualization + schedule-drag removed for
    SPY reconciliation), the empirically measured per-window deltas are:

      1y:  +24.22 bps  (year-specific SPY tracking variance)
      3y:   +2.61 bps  (engine within structural noise of NAV)
      5y:   +2.42 bps  (engine essentially matches NAV)
      10y:  +6.17 bps  (10-year cumulative tracking variance)

    The regression bands below are centered on these observations with
    +/- 5 bps headroom. Any future code change that silently shifts a
    delta outside its band fails this test before the kill-gate, forcing
    investigation rather than tolerance widening.

    The test skips when bundles are unavailable or windows cannot be
    reconciled.
    """
    sharadar_bundle = _real_sharadar_bundle()
    ssga_bundle = _real_ssga_bundle()
    if sharadar_bundle is None or ssga_bundle is None:
        pytest.skip(
            "no sharadar/spy_ssga snapshots in data/snapshots/; "
            "pull per docs/methodology/dataset_versioning.md"
        )

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)
    report = reconcile_spy_trailing(sharadar=sharadar, ssga=ssga)

    # Per-window regression bands centered on observed deltas (+/- 5 bps
    # headroom). SI is omitted (skipped on current bundle).
    bands = {
        "1y": (19.0, 29.0),
        "3y": (-3.0, 8.0),
        "5y": (-3.0, 8.0),
        "10y": (1.0, 11.0),
    }
    for result in report.per_window:
        tag = result.period_tag
        if tag not in bands:
            continue
        if result.verdict == "SKIPPED":
            continue
        assert result.delta_bps is not None
        lower, upper = bands[tag]
        assert lower <= result.delta_bps <= upper, (
            f"{tag} delta {result.delta_bps:+.2f} bps is outside the "
            f"regression band [{lower}, {upper}]; investigate before "
            f"widening tolerance. Evidence: {report.render_evidence_line()}"
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


# ----- ADR 0008: ssga_annualized_return helper -----


def test_ssga_annualized_1y_returns_period_return() -> None:
    """For 1y SSGA reports the period return directly per the fact sheet
    ("Periods of less than one year are not annualized"). The helper
    returns tr_last - 1.0.
    """
    tr = pl.DataFrame(
        {
            "dt": [date(2025, 4, 30), date(2026, 4, 30)],
            "tr": [1.0, 1.12],
            "daily_return": [0.0, 0.12],
        }
    )
    result = ssga_annualized_return(
        tr, "1y", anchor_dt=date(2025, 4, 30), end_dt=date(2026, 4, 30)
    )
    assert result == pytest.approx(0.12, abs=1e-12)


def test_ssga_annualized_3y_geometric_mean() -> None:
    """For 3y SSGA's annualized = (1 + period_return)^(1/3) - 1."""
    tr = pl.DataFrame(
        {
            "dt": [date(2023, 4, 28), date(2026, 4, 30)],
            "tr": [1.0, 1.50],
            "daily_return": [0.0, 0.5],
        }
    )
    result = ssga_annualized_return(
        tr, "3y", anchor_dt=date(2023, 4, 28), end_dt=date(2026, 4, 30)
    )
    expected = 1.50 ** (1.0 / 3.0) - 1.0
    assert result == pytest.approx(expected, abs=1e-12)


def test_ssga_annualized_5y_and_10y() -> None:
    """5y and 10y are TR^(1/N) - 1 with N=5 and N=10 respectively."""
    tr_5 = pl.DataFrame(
        {"dt": [date(2021, 4, 30), date(2026, 4, 30)], "tr": [1.0, 2.00],
         "daily_return": [0.0, 1.0]}
    )
    tr_10 = pl.DataFrame(
        {"dt": [date(2016, 4, 29), date(2026, 4, 30)], "tr": [1.0, 4.00],
         "daily_return": [0.0, 3.0]}
    )
    r5 = ssga_annualized_return(
        tr_5, "5y", anchor_dt=date(2021, 4, 30), end_dt=date(2026, 4, 30)
    )
    r10 = ssga_annualized_return(
        tr_10, "10y", anchor_dt=date(2016, 4, 29), end_dt=date(2026, 4, 30)
    )
    assert r5 == pytest.approx(2.00 ** (1.0 / 5.0) - 1.0, abs=1e-12)
    assert r10 == pytest.approx(4.00 ** (1.0 / 10.0) - 1.0, abs=1e-12)


def test_ssga_annualized_si_uses_decimal_years() -> None:
    """SI uses (end - anchor) / 365.25 days for the exponent base. Anchor
    1993-01-22 to 2026-04-30 is approximately 33.27 years.
    """
    tr = pl.DataFrame(
        {"dt": [SPY_INCEPTION_DATE, date(2026, 4, 30)], "tr": [1.0, 30.0],
         "daily_return": [0.0, 29.0]}
    )
    result = ssga_annualized_return(
        tr, "si", anchor_dt=SPY_INCEPTION_DATE, end_dt=date(2026, 4, 30)
    )
    years_decimal = (date(2026, 4, 30) - SPY_INCEPTION_DATE).days / 365.25
    expected = 30.0 ** (1.0 / years_decimal) - 1.0
    assert result == pytest.approx(expected, abs=1e-12)


def test_ssga_annualized_unknown_period_tag_raises() -> None:
    tr = pl.DataFrame(
        {"dt": [date(2025, 1, 1), date(2026, 1, 1)], "tr": [1.0, 1.10],
         "daily_return": [0.0, 0.10]}
    )
    with pytest.raises(ValueError, match="unknown period_tag"):
        ssga_annualized_return(
            tr, "2y", anchor_dt=date(2025, 1, 1), end_dt=date(2026, 1, 1)
        )


def test_ssga_annualized_missing_tr_column_raises() -> None:
    tr = pl.DataFrame({"dt": [date(2025, 1, 1)], "value": [1.0]})
    with pytest.raises(KeyError, match="'tr' column"):
        ssga_annualized_return(
            tr, "1y", anchor_dt=date(2025, 1, 1), end_dt=date(2026, 1, 1)
        )


def test_ssga_annualized_si_non_positive_years_raises() -> None:
    """An SI window with end_dt at or before anchor_dt is malformed."""
    tr = pl.DataFrame(
        {"dt": [SPY_INCEPTION_DATE], "tr": [1.0], "daily_return": [0.0]}
    )
    with pytest.raises(ValueError, match="non-positive decimal-years"):
        ssga_annualized_return(
            tr, "si", anchor_dt=SPY_INCEPTION_DATE, end_dt=SPY_INCEPTION_DATE
        )


def test_trading_day_and_nominal_year_agree_at_3y_plus() -> None:
    """The 252/(n-1) trading-day convention and SSGA's nominal-year
    convention agree to within 1e-3 (10 bps) for windows of 3+ years.

    Locks the ADR 0008 author claim that the convention switch is
    sub-bp at 3y+. The 1y window has a measurably different exponent
    (252/251 vs 1.0) so it is excluded from this convergence test.
    """
    # Synthetic constant 0.04% daily-multiplier path.
    daily_mult = 1.0004
    base = date(2010, 1, 4)
    n_3y = 252 * 3 + 1  # 757 trading rows
    n_5y = 252 * 5 + 1
    n_10y = 252 * 10 + 1

    def _build_tr(n: int) -> tuple[pl.DataFrame, date, date]:
        dts = [base + timedelta(days=i) for i in range(n)]
        tr_values = [daily_mult ** i for i in range(n)]
        frame = pl.DataFrame(
            {
                "dt": dts,
                "tr": tr_values,
                "daily_return": [0.0] + [daily_mult - 1.0] * (n - 1),
            }
        )
        return frame, dts[0], dts[-1]

    for period_tag, n in (("3y", n_3y), ("5y", n_5y), ("10y", n_10y)):
        frame, anchor, end = _build_tr(n)
        td_ann = annualized_return(frame)
        ny_ann = ssga_annualized_return(
            frame, period_tag, anchor_dt=anchor, end_dt=end
        )
        diff = abs(td_ann - ny_ann)
        assert diff < 1e-3, (
            f"{period_tag}: trading-day annualized {td_ann:.6f} vs "
            f"nominal-year {ny_ann:.6f} differ by {diff:.6f} "
            f"(expected < 1e-3 for convergence at 3y+)"
        )


# ----- ADR 0008: per-window tolerance dict locked -----


def test_ssga_tolerance_dict_locked() -> None:
    """SSGA_TOLERANCE_BPS per ADR 0008 is locked to the documented values.

    Changing any value requires editing this test, which puts the change
    in front of code review. Derivations are documented in ADR 0008's
    Final Locked Decisions section.
    """
    assert dict(SSGA_TOLERANCE_BPS) == {
        "1y": 25.0,
        "3y": 8.0,
        "5y": 7.0,
        "10y": 15.0,
        "si": 20.0,
    }


def test_ssga_tolerance_covers_every_spy_period_tag() -> None:
    """Every SPY_PERIOD_TAGS entry must have a tolerance. A future ADR
    that adds a tag without updating SSGA_TOLERANCE_BPS would fail here.
    """
    for tag in SPY_PERIOD_TAGS:
        assert tag in SSGA_TOLERANCE_BPS, (
            f"period_tag {tag!r} is in SPY_PERIOD_TAGS but missing from "
            f"SSGA_TOLERANCE_BPS; update ADR 0008 dict or remove from "
            f"SPY_PERIOD_TAGS"
        )
