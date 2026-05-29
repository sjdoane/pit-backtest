"""Diagnose the 1y SPY reconciliation drift (ADR 0006 follow-up).

Compares Sharadar ACTIONS dividends vs SSGA distributions for SPY over
[anchor_dt, snapped_as_of] where anchor_dt is the 1y window's
snap-backward and snapped_as_of is SSGA's as_of_date. Prints side-by-
side with deltas so a missing, extra, or mis-amounted dividend is
immediately visible.

Run from the repo root:

    uv run python scripts/diagnose_1y_drift.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl

from pit_backtest.data.adjustments import annualized_return, reconstruct_total_return
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.sources.ssga import SSGASpyReference
from pit_backtest.engine.spy_reconciliation import (
    SPY_EXPENSE_RATIO_SCHEDULE,
    _nyse_trading_days_cached,
    discover_latest_bundle,
    snap_to_anchor,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOTS_ROOT = _REPO_ROOT / "data" / "snapshots"


def main() -> int:
    sharadar_bundle = discover_latest_bundle(_SNAPSHOTS_ROOT, "sharadar")
    ssga_bundle = discover_latest_bundle(_SNAPSHOTS_ROOT, "spy_ssga")
    if sharadar_bundle is None or ssga_bundle is None:
        print("no bundles", file=sys.stderr)
        return 2

    sharadar = SharadarDataSource(sharadar_bundle, _SNAPSHOTS_ROOT)
    ssga = SSGASpyReference(ssga_bundle, _SNAPSHOTS_ROOT)
    as_of = ssga.as_of_date
    if as_of is None:
        print("no as_of_date on SSGA bundle", file=sys.stderr)
        return 2

    trading_days = _nyse_trading_days_cached()
    raw_start = as_of.replace(year=as_of.year - 1)
    anchor_dt = snap_to_anchor(raw_start, trading_days)
    end_dt = snap_to_anchor(as_of, trading_days)
    print(f"1y window: anchor={anchor_dt}, end={end_dt}, as_of={as_of}")
    print()

    # --- Side-by-side dividend table ---
    sharadar_divs = sharadar.read_actions_dividends(
        ticker="SPY", start_dt=anchor_dt, end_dt=end_dt
    )
    ssga_divs_all = ssga.dividends()
    ssga_divs = ssga_divs_all.filter(
        (pl.col("ex_date") >= anchor_dt) & (pl.col("ex_date") <= end_dt)
    ).sort("ex_date")

    def _ascii_rows(frame: pl.DataFrame) -> str:
        rows: list[str] = []
        for row in frame.iter_rows(named=True):
            cells = ", ".join(f"{k}={v}" for k, v in row.items())
            rows.append(f"  {cells}")
        return "\n".join(rows) if rows else "  (empty)"

    print("Sharadar ACTIONS dividends (SPY) in 1y window:")
    print(_ascii_rows(sharadar_divs))
    print()
    print("SSGA distributions (SPY) in 1y window:")
    print(_ascii_rows(ssga_divs))
    print()

    # --- Side-by-side merge ---
    sharadar_for_join = sharadar_divs.rename(
        {"amount_per_share": "sharadar_amt"}
    )
    ssga_for_join = ssga_divs.rename(
        {"amount_per_share": "ssga_amt"}
    )
    merged = sharadar_for_join.join(
        ssga_for_join, on="ex_date", how="full", coalesce=True
    ).sort("ex_date")
    merged = merged.with_columns(
        (pl.col("sharadar_amt") - pl.col("ssga_amt")).alias("delta")
    )
    print("Side-by-side merge (delta = sharadar - ssga):")
    print(_ascii_rows(merged))
    print()
    print(
        f"Totals: sharadar = {sharadar_divs['amount_per_share'].sum():.4f}, "
        f"ssga = {ssga_divs['amount_per_share'].sum():.4f}"
    )
    print()

    # --- Reconstruct TR step by step ---
    prices = sharadar.read_sep_prices(
        ticker="SPY", start_dt=anchor_dt, end_dt=end_dt
    )
    prices_for_tr = prices.select(
        pl.col("dt"), pl.col("closeunadj").alias("close")
    )
    print(
        f"Engine prices: {prices.height} rows, "
        f"first={prices['dt'][0]}, last={prices['dt'][-1]}"
    )
    print(
        f"  closeunadj at anchor: {float(prices_for_tr['close'][0]):.4f}"
    )
    print(
        f"  closeunadj at end:    {float(prices_for_tr['close'][-1]):.4f}"
    )
    print()

    # TR with the schedule.
    tr_with_schedule = reconstruct_total_return(
        prices_for_tr,
        sharadar_divs,
        start_dt=anchor_dt,
        end_dt=end_dt,
        expense_ratio_annual=SPY_EXPENSE_RATIO_SCHEDULE,
    )
    engine_ann_schedule = annualized_return(tr_with_schedule)

    # TR with no expense drag (gross).
    tr_no_drag = reconstruct_total_return(
        prices_for_tr,
        sharadar_divs,
        start_dt=anchor_dt,
        end_dt=end_dt,
        expense_ratio_annual=Decimal("0"),
    )
    engine_ann_gross = annualized_return(tr_no_drag)

    # TR with no dividends (price-only).
    tr_no_div = reconstruct_total_return(
        prices_for_tr,
        pl.DataFrame(
            {"ex_date": [], "amount_per_share": []},
            schema={"ex_date": pl.Date, "amount_per_share": pl.Float64},
        ),
        start_dt=anchor_dt,
        end_dt=end_dt,
        expense_ratio_annual=Decimal("0"),
    )
    engine_ann_price = annualized_return(tr_no_div)

    ssga_ann_1y = ssga.annualized_nav_tr_for_period("1y")
    print(f"Engine annualized (with schedule):   {engine_ann_schedule * 100:.4f}%")
    print(f"Engine annualized (gross, no drag):  {engine_ann_gross * 100:.4f}%")
    print(f"Engine annualized (price-only):      {engine_ann_price * 100:.4f}%")
    print(f"SSGA published 1y NAV TR:            {ssga_ann_1y * 100:.4f}%")
    print()
    print(
        f"Delta (engine_schedule - ssga) = "
        f"{(engine_ann_schedule - ssga_ann_1y) * 10_000:+.2f} bps annualized"
    )
    print(
        f"Delta (engine_gross    - ssga) = "
        f"{(engine_ann_gross - ssga_ann_1y) * 10_000:+.2f} bps annualized"
    )
    print(
        f"Delta (engine_price    - ssga) = "
        f"{(engine_ann_price - ssga_ann_1y) * 10_000:+.2f} bps annualized"
    )
    print()

    # --- Hypothesis: anchor SHIFTED BACK ONE TRADING DAY ---
    # SSGA's "trailing 1y" may anchor at the trading day strictly before
    # raw_start = 2025-04-30, so the period accumulates 252 daily returns
    # starting from 2025-04-30's close. The engine's snap-backward anchors
    # on 2025-04-30 itself when it is a trading day, accumulating only
    # 251 daily returns.
    idx = trading_days.index(anchor_dt)
    shifted_anchor = trading_days[idx - 1]
    print(
        f"Hypothesis: anchor shifted to {shifted_anchor} "
        f"(prior trading day; 1-day-back convention):"
    )

    prices_shifted = sharadar.read_sep_prices(
        ticker="SPY", start_dt=shifted_anchor, end_dt=end_dt
    )
    prices_for_tr_shifted = prices_shifted.select(
        pl.col("dt"), pl.col("closeunadj").alias("close")
    )
    sharadar_divs_shifted = sharadar.read_actions_dividends(
        ticker="SPY", start_dt=shifted_anchor, end_dt=end_dt
    )

    tr_shifted = reconstruct_total_return(
        prices_for_tr_shifted,
        sharadar_divs_shifted,
        start_dt=shifted_anchor,
        end_dt=end_dt,
        expense_ratio_annual=SPY_EXPENSE_RATIO_SCHEDULE,
    )
    engine_ann_shifted = annualized_return(tr_shifted)
    period_return_shifted = float(tr_shifted["tr"][-1]) - 1.0
    print(
        f"  n_trading_days={tr_shifted.height}, "
        f"close[{shifted_anchor}]={float(prices_for_tr_shifted['close'][0]):.4f}, "
        f"close[{end_dt}]={float(prices_for_tr_shifted['close'][-1]):.4f}"
    )
    print(f"  engine_period_return = {period_return_shifted * 100:.4f}%")
    print(f"  engine_annualized    = {engine_ann_shifted * 100:.4f}%")
    print(
        f"  Delta vs SSGA 1y      = "
        f"{(engine_ann_shifted - ssga_ann_1y) * 10_000:+.2f} bps annualized"
    )
    print()

    # Also: pure period-return interpretation (SSGA's 1y as cumulative,
    # no annualization compounding adjustment).
    period_return_orig = float(tr_with_schedule["tr"][-1]) - 1.0
    print(
        f"Hypothesis: SSGA's '1y' is the CUMULATIVE period return "
        f"(no annualization compounding):"
    )
    print(f"  engine_period_return (orig anchor, schedule) = {period_return_orig * 100:.4f}%")
    print(
        f"  Delta vs SSGA 1y                              = "
        f"{(period_return_orig - ssga_ann_1y) * 10_000:+.2f} bps"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
