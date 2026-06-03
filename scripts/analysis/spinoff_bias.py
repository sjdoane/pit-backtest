"""Regenerate the spin-off bias quantification in docs/research/0003-spinoff-bias.md.

Bundle-gated, ``__main__``-guarded reproducible analysis. On the real Sharadar
bundle it reproduces:

  - the spin-family event counts in the momentum 2005-2024 ever-member union and
    the survivorship PIT-2010 cohort (the affected-events list);
  - the GE worked example (ex-date ``closeunadj`` drops vs the as-traded decoded
    spun value);
  - the per-name strict frozen-cash treatment-2 FLOOR and the honest aggregate
    (cohort CAGR lift + survivorship-delta shift).

The floor is treatment 2 (the spun value credited as cash at the ex-date, earning
nothing afterward), computed ADDITIVELY as ``recorded + sum_i W_pre_i * (spun_i /
pre_i)`` where ``W_pre_i`` is the buy-and-hold total return at the bar just before
the ex-date. It is NOT the multiplicative ``recorded * (ex+spun)/ex``, which would
wrongly credit the spun value with the parent's own post-ex return. Treatment 1
(holding the distributed shares) is higher; this is the honest floor.

The recorded cohort means come from the committed figure artifact
(``scripts/figures/study_artifact/survivorship.json``), so the aggregate
regenerates without re-running the full ~900-name cohort build. The
``spinoffdividend`` dollar value carries the same ADR 0018 back-adjustment defect
as the ordinary dividend value and is decoded via ``to_as_traded_dividends``
before any magnitude math.

Run (clean venv, from the repo root):

    python scripts/analysis/spinoff_bias.py
    python scripts/analysis/spinoff_bias.py --bundle sharadar_2026-05-31

Exit codes: 0 success; 2 missing bundle or missing survivorship artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pit_backtest.data.adjustments import SPLIT_LIKE_ACTIONS, to_as_traded_dividends
from pit_backtest.data.sources.sharadar import SharadarDataSource
from pit_backtest.data.universe import SharadarSP500Universe
from pit_backtest.engine.spy_reconciliation import discover_latest_bundle

from examples.sp500_momentum_study import (
    build_asset_id_to_ticker,
    build_ever_member_union,
    momentum_rebalance_dates,
)
from examples.sp500_survivorship import _member_tr_series, _resolve_ticker_at

_SNAPSHOTS_ROOT = REPO_ROOT / "data" / "snapshots"
_SURVIVORSHIP_ARTIFACT = (
    REPO_ROOT / "scripts" / "figures" / "study_artifact" / "survivorship.json"
)
_MOM_START = date(2005, 1, 4)
_MOM_END = date(2024, 12, 31)
_PIT_DATE = date(2010, 1, 4)
_SPIN_CODES = ("spinoff", "spinoffdividend", "spunofffrom")

_EXIT_OK = 0
_EXIT_MISSING = 2


def _to_close(d: date) -> datetime:
    return datetime.combine(d, time(16, 0))


def _pre_close(tsep: pl.DataFrame, ex_date: date) -> float | None:
    """The parent's as-traded closeunadj at the last bar strictly before ex_date."""
    before = tsep.filter(pl.col("date") < ex_date)
    return float(before["closeunadj"][-1]) if before.height else None


def _wealth_before(series: pl.DataFrame, ex_date: date) -> float | None:
    """The buy-and-hold total return (wealth) at the last bar strictly before ex_date."""
    before = series.filter(pl.col("dt") < ex_date)
    return float(before["tr"][-1]) if before.height else None


def _decoded_spin_events(actions: pl.DataFrame) -> pl.DataFrame:
    """Per (ticker, ex_date) spinoffdividend event with the as-traded total spun value.

    The stored value is split-adjusted to the pull date (the ADR 0018 defect), so
    it is decoded via ``to_as_traded_dividends`` (the inverse of the vendor
    back-adjustment) before summing same-day multi-distributions (e.g. UTX ->
    Carrier + Otis on one ex-date) into a single event.
    """
    sdiv = actions.filter(pl.col("action") == "spinoffdividend").select(
        pl.col("ticker"), pl.col("date").alias("ex_date"), pl.col("value")
    )
    splits = actions.filter(
        pl.col("action").is_in(sorted(SPLIT_LIKE_ACTIONS))
    ).select(
        pl.col("ticker"),
        pl.col("date").alias("split_date"),
        pl.col("value").cast(pl.Float64).alias("ratio"),
    )
    decoded = to_as_traded_dividends(sdiv, splits)
    return decoded.group_by(["ticker", "ex_date"]).agg(
        pl.col("value").sum().alias("spun_total")
    )


def _treatment_2_floor(
    source: SharadarDataSource,
    ticker: str,
    events: pl.DataFrame,
    split_dates: set[date],
    lo: date,
    hi: date,
) -> tuple[float, float] | None:
    """(recorded terminal TR, strict frozen-cash treatment-2 floor) for one parent.

    Returns None when the buy-and-hold series is unavailable (fewer than 2 bars).
    """
    series = _member_tr_series(source, ticker, lo, hi)
    if series is None:
        return None
    recorded = float(series["tr"][-1])
    if recorded <= 0.0:
        return None
    prices = source.read_sep_prices(ticker=ticker, start_dt=lo, end_dt=hi)
    tsep = prices.select(
        pl.col("dt").alias("date"), pl.col("closeunadj")
    ).sort("date")
    added = 0.0
    rows = events.filter(
        (pl.col("ticker") == ticker)
        & (pl.col("ex_date") >= lo)
        & (pl.col("ex_date") <= hi)
    ).sort("ex_date")
    for row in rows.iter_rows(named=True):
        ex_date = row["ex_date"]
        if ex_date in split_dates:  # bases align only across a non-split day
            continue
        pre = _pre_close(tsep, ex_date)
        wealth = _wealth_before(series, ex_date)
        spun = row["spun_total"]
        if pre is None or wealth is None or spun is None or pre <= 0.0:
            continue
        added += wealth * (spun / pre)  # frozen cash, earns nothing afterward
    return recorded, recorded + added


def _cagr(mean_terminal: float, years: float) -> float:
    return mean_terminal ** (1.0 / years) - 1.0


def _count_in(
    spin: pl.DataFrame, tickers: set[str], lo: date, hi: date
) -> pl.DataFrame:
    return spin.filter(
        pl.col("ticker").is_in(list(tickers))
        & (pl.col("date") >= lo)
        & (pl.col("date") <= hi)
    )


def run(source: SharadarDataSource, bundle_root: Path) -> str:
    """Build the report string for the loaded source."""
    surv = json.loads(_SURVIVORSHIP_ARTIFACT.read_text(encoding="utf-8"))
    as_of = date.fromisoformat(surv["as_of_date"])
    years = float(surv["years"])

    actions = pl.read_parquet(
        bundle_root / source.bundle_name / "actions.parquet"
    ).with_columns(pl.col("date").cast(pl.Date))
    spin = actions.filter(pl.col("action").is_in(_SPIN_CODES))
    events = _decoded_spin_events(actions)

    # Momentum 2005-2024 ever-member union (date-agnostic tickers).
    uni = SharadarSP500Universe(source)
    rebals = momentum_rebalance_dates(_MOM_START, _MOM_END)
    union = build_ever_member_union(uni, rebals)
    union_tickers = set(build_asset_id_to_ticker(source, union).values())

    # Survivorship PIT-2010 cohort (tickers resolved AT pit_date, as the study does).
    pit_members = source.members_at("sp500", _to_close(_PIT_DATE))
    cohort: dict[str, int] = {}
    for asset_id in pit_members:
        ticker = _resolve_ticker_at(source, asset_id, _PIT_DATE)
        if ticker is not None:
            cohort[ticker] = int(asset_id)

    lines: list[str] = [
        "# Spin-off bias quantification (regenerated)",
        "",
        f"Bundle: {source.bundle_name}; PIT date {_PIT_DATE}; as-of {as_of}; "
        f"years {years:.4f}",
        "",
        "## Affected spin-family events",
        "",
        "| Set | spinoff rows/parents | spinoffdividend rows/parents | spunofffrom rows |",
        "|---|---|---|---|",
    ]
    for label, tickers, lo, hi in [
        ("Momentum union (2005-2024)", union_tickers, _MOM_START, _MOM_END),
        ("Survivorship PIT-2010 cohort", set(cohort), _PIT_DATE, as_of),
    ]:
        cells = []
        for code in ("spinoff", "spinoffdividend", "spunofffrom"):
            sub = _count_in(
                spin.filter(pl.col("action") == code), tickers, lo, hi
            )
            cells.append((sub.height, sub["ticker"].n_unique()))
        lines.append(
            f"| {label} | {cells[0][0]}/{cells[0][1]} | "
            f"{cells[1][0]}/{cells[1][1]} | {cells[2][0]} |"
        )

    # Per-name frozen-cash floor for every cohort spin-off parent + the aggregate.
    def aggregate(
        members: dict[str, int],
        priced_mean: float,
        priced_cagr: float,
        lo: date,
        hi: date,
        label: str,
    ) -> tuple[float, list[tuple[str, float, float]]]:
        parents = sorted(
            set(
                _count_in(
                    spin.filter(pl.col("action") == "spinoffdividend"),
                    set(members),
                    lo,
                    hi,
                )["ticker"].to_list()
            )
        )
        per_name: list[tuple[str, float, float]] = []
        priced = 0
        added_sum = 0.0
        for ticker in members:
            series = _member_tr_series(source, ticker, lo, hi)
            if series is None:
                continue
            priced += 1
        for ticker in parents:
            split_dates = set(
                actions.filter(
                    (pl.col("ticker") == ticker)
                    & pl.col("action").is_in(sorted(SPLIT_LIKE_ACTIONS))
                )["date"].to_list()
            )
            result = _treatment_2_floor(
                source, ticker, events, split_dates, lo, hi
            )
            if result is None:
                continue
            recorded, floor = result
            per_name.append((ticker, recorded, floor))
            added_sum += floor - recorded
        lift = added_sum / priced if priced else 0.0
        new_cagr = _cagr(priced_mean + lift, years)
        shift = (new_cagr - priced_cagr) * 1e4
        lines.append("")
        lines.append(
            f"## {label}: {len(per_name)} spin-off parents, {priced} priced names"
        )
        lines.append(
            f"cohort mean {priced_mean:.3f}x -> {priced_mean + lift:.3f}x; "
            f"CAGR {priced_cagr * 100:.4f}% -> {new_cagr * 100:.4f}% "
            f"({shift:+.2f} bps)"
        )
        return new_cagr, per_name

    pit_new_cagr, pit_per_name = aggregate(
        cohort,
        float(surv["pit_cohort_mean_terminal_tr"]),
        float(surv["pit_cohort_cagr"]),
        _PIT_DATE,
        as_of,
        "PIT-2010 cohort",
    )
    cur_members = source.members_at("sp500", _to_close(as_of))
    cur_cohort: dict[str, int] = {}
    for asset_id in cur_members:
        ticker = _resolve_ticker_at(source, asset_id, _PIT_DATE)
        if ticker is not None:
            cur_cohort[ticker] = int(asset_id)
    cur_new_cagr, _ = aggregate(
        cur_cohort,
        float(surv["current_cohort_mean_terminal_tr"]),
        float(surv["current_cohort_cagr"]),
        _PIT_DATE,
        as_of,
        "Current cohort",
    )
    rec_delta = float(surv["cagr_delta_bps"])
    new_delta = (cur_new_cagr - pit_new_cagr) * 1e4
    lines.append("")
    lines.append(
        f"## Survivorship delta: recorded {rec_delta:.2f} bps -> "
        f"treatment-2 floor {new_delta:.2f} bps ({new_delta - rec_delta:+.2f} bps)"
    )

    # The top per-name understatements (largest floor minus recorded, descending).
    lines.append("")
    lines.append("## Largest per-name understatements (PIT cohort)")
    lines.append("")
    lines.append("| Ticker | Recorded TR | Treatment-2 floor | Understatement |")
    lines.append("|---|---|---|---|")
    for ticker, recorded, floor in sorted(
        pit_per_name, key=lambda r: r[1] - r[2]
    )[:10]:
        pct = (1.0 - recorded / floor) * 100 if floor > 0 else 0.0
        lines.append(
            f"| {ticker} | {recorded:.2f}x | {floor:.2f}x | {pct:.0f}% |"
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=str, default=None)
    parser.add_argument("--bundle-prefix", type=str, default="sharadar")
    parser.add_argument(
        "--snapshots-root", type=Path, default=_SNAPSHOTS_ROOT
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    root: Path = args.snapshots_root.resolve()

    if not _SURVIVORSHIP_ARTIFACT.exists():
        print(
            f"missing survivorship artifact {_SURVIVORSHIP_ARTIFACT}; "
            "run scripts/figures/build_study_artifact.py first.",
            file=sys.stderr,
        )
        return _EXIT_MISSING

    bundle_name = args.bundle or discover_latest_bundle(root, args.bundle_prefix)
    if bundle_name is None:
        print(
            f"no bundle with prefix {args.bundle_prefix!r} under {root}; "
            "pull a Sharadar snapshot per docs/methodology/dataset_versioning.md.",
            file=sys.stderr,
        )
        return _EXIT_MISSING

    source = SharadarDataSource(bundle_name, root)
    print(run(source, root))
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
