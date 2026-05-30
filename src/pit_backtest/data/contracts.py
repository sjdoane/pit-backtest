"""Data quality contracts that fire at ingest plus the PIT lookahead gate.

Per ADR 0002 decision 12: invariants that vendor data must satisfy before
the engine accepts a snapshot. Failures surface the offending rows.

Per ADR 0001 decision 9 (dual-timestamp model): every PIT read must gate
on `available_dt <= simulation_dt`. The `LookaheadLeakError` +
`assert_not_lookahead` helper below is the canonical entry point that
every per-row PitDataSource method calls at the top of its body. M3 PR 1
shipped the helper standalone; M3 PRs 2 through 4 wired it through the
per-row paths.

Contracts (M3 PR 5a deliverable):
1. Every TICKERS row has a SEP price within 5 trading days of firstpricedate.
2. No SEP bars exist after the delisted date.
3. SF1 datekey is non-null for ARQ rows after 1990.
4. No duplicate (ticker, datekey) pairs in SF1.
5. Every SP500 event ticker resolves to exactly one TICKERS row whose
   [firstpricedate, lastpricedate] interval contains the event date.

`run_data_quality_contracts` collects all failures across the five
contracts and raises one aggregated `DataQualityError` listing them in
alphabetical order by contract name (operators grep on the name; a
deterministic message is load-bearing for log-pattern alerts).

Freshness check (M3 PR 5a deliverable, per ADR 0003 decision 16):
`check_snapshot_freshness` warns at 30 days and emits a STALE-tagged
warning at 90 days. Both inputs are interpreted as America/New_York close
per ADR 0002 decision 11.

Additional contracts (per ADR 0003 trust boundary #3): user-supplied
additional_data frames must have an available_dt column whose values are
not in the future relative to any period_end_dt in the same frame. v1.1.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import cache as _functools_cache
from typing import TYPE_CHECKING, Callable, Protocol

import pandas_market_calendars as mcal  # type: ignore[import-untyped]
import polars as pl

if TYPE_CHECKING:
    from pit_backtest.data.sources.manifest import SnapshotBundleEntry
    from pit_backtest.data.sources.sharadar import SharadarDataSource


_LOG = logging.getLogger(__name__)


class DataQualityContract(Protocol):
    """A single data-quality invariant.

    Implementations return None on success and raise DataQualityError with
    the offending rows surfaced on failure. The `required_tables` attribute
    lets the runner skip contracts cleanly when a bundle does not ship the
    referenced table (M1 SPY-only demos do not include SP500; M3 PR 5a's
    contracts skip with an INFO log in that case).
    """

    name: str
    required_tables: frozenset[str]

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        """Run the invariant against the loaded frames."""
        ...


class DataQualityError(ValueError):
    """Raised when a data quality contract fails.

    The message includes the contract name and a sample of offending rows
    so the failure is actionable without re-running the check.
    """


class LookaheadLeakError(ValueError):
    """Raised when a PIT read crosses the dual-timestamp boundary.

    Per ADR 0001 decision 9, every PIT-aware adapter method must reject
    a read whose `available_dt` is greater than the `simulation_dt`. This
    is the engine's structural protection against the most common form of
    silent look-ahead bias (using a fundamental field at a date BEFORE the
    filing actually became observable).

    Inherits from ValueError so callers can broad-catch when they wrap a
    pit_view read; named catch is preferred when the recovery path is
    specific to the leak case.
    """


def assert_not_lookahead(
    available_dt: datetime,
    simulation_dt: datetime,
    *,
    context: str,
    period_end_dt: datetime | None = None,
) -> None:
    """Raise LookaheadLeakError when `available_dt > simulation_dt`.

    Per ADR 0001 decision 9 (dual-timestamp model), `available_dt` is when
    the record became observable (SEC submission date for fundamentals;
    bar close for daily price bars). Reading a record with
    `available_dt > simulation_dt` is a look-ahead leak and the engine
    must fail loudly rather than silently return the future value.

    Equal dates (`available_dt == simulation_dt`) are allowed: the record
    became observable at the same moment the simulation is asking for it.

    Per ADR 0002 decision 11, both inputs are interpreted in
    America/New_York; callers must normalize at the adapter boundary
    BEFORE invoking this helper. The helper does NOT inspect tzinfo to
    avoid silently masking a tz mismatch as a date comparison.

    Args:
      available_dt: when the record became observable.
      simulation_dt: the simulation's current time.
      context: a short string naming the call site for the error message
        (for example, `"SharadarDataSource.get_fundamental(asset=42, field='revenue')"`).
        Subsequent M3 PRs pass a context string that surfaces the offending
        asset_id + field + flavor so the failure is actionable.
      period_end_dt: optional, the period_end_dt of the offending row
        (per ADR 0001 decision 9 the dual-timestamp pair). When provided,
        the error message includes both timestamps; a future debug
        session has both halves without re-reading the source frame.

    Raises:
      LookaheadLeakError: when `available_dt > simulation_dt`.

    Usage pattern that future M3 per-row methods follow:

      def get_fundamental(self, asset_id, available_dt, field, flavor):
          assert_not_lookahead(
              available_dt,
              self._simulation_dt,
              context=f"SharadarDataSource.get_fundamental(asset={asset_id}, field={field!r})",
          )
          ...
    """
    if available_dt > simulation_dt:
        period_clause = (
            f" period_end_dt={period_end_dt.isoformat()}"
            if period_end_dt is not None
            else ""
        )
        raise LookaheadLeakError(
            f"lookahead leak in {context}: "
            f"available_dt={available_dt.isoformat()} > "
            f"simulation_dt={simulation_dt.isoformat()}{period_clause}"
        )


# ----- M3 PR 5a: NYSE trading-day helper -----

@_functools_cache
def _nyse_calendar() -> mcal.MarketCalendar:
    """Module-level cached NYSE calendar.

    pandas-market-calendars rebuilds the trading-day index from a packaged
    CSV on each `get_calendar` call; on a cold cache the call costs 50-200
    ms. Caching the calendar object at module level keeps the runner's
    per-construction cost at one rebuild per Python process. The TestClock
    in `execution/clock.py` already caches its own slice of trading days;
    the two caches are independent (no shared mutable state, both immutable
    once built).
    """
    return mcal.get_calendar("NYSE")


def _nth_trading_day_after(d: date, n: int) -> date:
    """Return the n-th NYSE trading day strictly after `d`.

    Used by `FirstPriceWithinFiveDaysContract` to compute the right
    endpoint of the SEP-coverage window `[firstpricedate, cutoff]`.

    The window passed to `valid_days` is `(d, d + max(n * 3, 14)]` in
    calendar days. n=5 needs at most ~7 calendar days through a normal
    week; the n*3 lower bound + 14-day floor covers Thanksgiving and
    Christmas clusters with margin.

    The `valid_days(start_date, end_date)` window is INCLUSIVE per the
    pandas-market-calendars contract. By starting at `d + 1d` the
    returned index has no d-as-first-entry edge case (whether d is itself
    a trading day or not), so `result[n - 1]` is always the n-th
    strictly-after entry.

    Plan-reviewer High 2: the original plan's
    `valid_days(d, d + timedelta(days=14))[4]` undercounts by one when
    `d` is itself a trading day (index 0 is `d`, index 4 is the FOURTH
    trading day after). This implementation uses the strict-after window
    so the off-by-one cannot recur. Pinned by
    `test_nth_trading_day_after_thanksgiving_pins_strict_after_semantics`.

    Args:
      d: anchor date (naive `datetime.date`; may itself be a holiday).
      n: positive integer count of trading days strictly after `d`.

    Returns:
      The n-th trading day strictly after `d`, as a naive `datetime.date`.

    Raises:
      ValueError: when n < 1 or when the NYSE calendar has fewer than n
        trading days in the search window.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1; got {n}")
    window_end = d + timedelta(days=max(n * 3, 14))
    valid = _nyse_calendar().valid_days(
        start_date=d + timedelta(days=1),
        end_date=window_end,
    )
    if len(valid) < n:
        raise ValueError(
            f"NYSE calendar has fewer than {n} trading days in "
            f"({d.isoformat()}, {window_end.isoformat()}]; widen the window"
        )
    # pandas-market-calendars is untyped; the `.date()` call returns
    # `Any` to mypy. Cast through the typed local intermediate to keep
    # the function's `-> date` signature honest.
    result_date: date = valid[n - 1].date()
    return result_date


# ----- M3 PR 5a: shared constants -----

# Cap on offending-row samples surfaced in DataQualityError messages.
# 10 keeps an aggregated five-contract failure under ~150 KB even on a
# fully broken bundle (operator pattern-matches on clusters; unbounded
# breaks log readability).
_MAX_FAILURE_SAMPLE_ROWS = 10

# Per ADR 0003 decision 16: warn at 30 days; warn loudly at 90.
_FRESHNESS_WARN_DAYS = 30
_FRESHNESS_STALE_DAYS = 90


# ----- M3 PR 5a: five concrete contracts -----
#
# Per ADR 0002 decision 12 + docs/methodology/dataset_versioning.md, these
# are the v1 invariants vendor data must satisfy at ingest. Plain classes
# (no attrs) because each contract is stateless and behavior lives entirely
# in the `check` body; the `name` and `required_tables` class attributes
# satisfy the `DataQualityContract` Protocol via Python's normal attribute
# lookup. Instances are stored in `_DEFAULT_CONTRACTS` and constructed once
# at module import.


def _format_violation_message(
    contract_name: str,
    total: int,
    sample: pl.DataFrame,
    detail: str,
) -> str:
    """Build the deterministic error message a contract raises on failure.

    Centralized so all five contracts share the same shape; the aggregated
    runner message at `run_data_quality_contracts` re-formats this for the
    multi-failure case. `detail` is the contract-specific sentence
    explaining what the violation means.
    """
    sample_rows = sample.head(_MAX_FAILURE_SAMPLE_ROWS).to_dicts()
    shown = min(total, _MAX_FAILURE_SAMPLE_ROWS)
    return (
        f"contract {contract_name!r} found {total} {detail}; "
        f"first {shown} sample(s): {sample_rows}"
    )


class FirstPriceWithinFiveDaysContract:
    """Every TICKERS row with a non-NULL firstpricedate has at least one SEP
    bar within 5 trading days of that date (ADR 0002 dec 12 invariant 1).

    Vendor data-quality bug class caught: a ticker is indexed in TICKERS
    but the SEP feed is empty or missing the IPO-window rows. NULL
    firstpricedate rows are skipped (never-traded shells; not a violation).

    The 5-trading-day window is anchored on NYSE via `_nth_trading_day_after`
    so a Friday firstpricedate with a Monday holiday does not produce a
    false positive (the helper counts trading days, not calendar days).
    """

    name = "tickers_first_price_within_five_days"
    required_tables = frozenset({"tickers", "sep"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        tickers = frames["tickers"].with_columns(
            pl.col("firstpricedate").cast(pl.Date),
            pl.col("lastpricedate").cast(pl.Date),
        )
        sep = frames["sep"].with_columns(pl.col("date").cast(pl.Date))

        candidates = tickers.filter(pl.col("firstpricedate").is_not_null())
        if candidates.height == 0:
            return

        # Compute cutoff per distinct firstpricedate once; the trading-day
        # helper is the expensive call, so we want O(distinct firstpricedates)
        # invocations not O(TICKERS rows).
        distinct_firstprice = (
            candidates.get_column("firstpricedate").unique().sort().to_list()
        )
        cutoff_map: dict[date, date] = {
            d: _nth_trading_day_after(d, n=5) for d in distinct_firstprice
        }
        candidates_with_cutoff = candidates.with_columns(
            pl.col("firstpricedate")
            .replace_strict(cutoff_map, return_dtype=pl.Date)
            .alias("_cutoff_date")
        )

        # Left join with SEP; count in-window matches per candidate row.
        # group_by on (permaticker, ticker, firstpricedate) preserves
        # ticker-reuse history (one TICKERS row per interval; the
        # contract checks each interval's first-price coverage).
        joined = candidates_with_cutoff.join(
            sep.select("ticker", "date"),
            on="ticker",
            how="left",
        ).with_columns(
            (
                pl.col("date").is_not_null()
                & (pl.col("date") >= pl.col("firstpricedate"))
                & (pl.col("date") <= pl.col("_cutoff_date"))
            ).alias("_in_window")
        )
        match_counts = joined.group_by(
            ["permaticker", "ticker", "firstpricedate", "lastpricedate"]
        ).agg(pl.col("_in_window").sum().alias("match_count"))
        violations = match_counts.filter(pl.col("match_count") == 0).sort(
            ["permaticker", "firstpricedate"]
        )
        if violations.height > 0:
            sample = violations.select(
                "permaticker", "ticker", "firstpricedate", "lastpricedate"
            )
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    sample,
                    "TICKERS row(s) with no SEP price within 5 trading "
                    "days of firstpricedate",
                )
            )


class NoSepBarsAfterDelistingContract:
    """No SEP bars exist after a TICKERS row's lastpricedate when
    isdelisted == 'Y' (ADR 0002 dec 12 invariant 2).

    Vendor data-quality bug class caught: phantom SEP bars from the
    delisted ticker (most often a vendor reuse of the ticker string that
    the join collapses; in that case the contract surfaces the bar so the
    operator can verify the reuse is legitimate).
    """

    name = "no_sep_bars_after_delisting"
    required_tables = frozenset({"tickers", "sep"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        tickers = frames["tickers"].with_columns(
            pl.col("lastpricedate").cast(pl.Date),
        )
        sep = frames["sep"].with_columns(pl.col("date").cast(pl.Date))

        delisted = tickers.filter(
            (pl.col("isdelisted") == "Y") & pl.col("lastpricedate").is_not_null()
        )
        if delisted.height == 0:
            return

        joined = delisted.join(
            sep.select("ticker", "date", "closeunadj"),
            on="ticker",
            how="left",
        )
        violations = (
            joined.filter(
                pl.col("date").is_not_null()
                & (pl.col("date") > pl.col("lastpricedate"))
            )
            .select(
                "permaticker",
                "ticker",
                "lastpricedate",
                pl.col("date").alias("sep_date"),
                pl.col("closeunadj").cast(pl.Float64),
            )
            .sort(["permaticker", "sep_date"])
        )
        if violations.height > 0:
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    violations,
                    "SEP bar(s) after a TICKERS-reported delisting date",
                )
            )


class Sf1DatekeyNonNullAfter1990Contract:
    """SF1 datekey is non-null for ARQ rows with calendardate >= 1991-01-01
    (ADR 0002 dec 12 invariant 3).

    The "after 1990" qualifier reads as fiscal periods strictly after 1990,
    so the filter is `calendardate >= date(1991, 1, 1)`. Using
    `calendardate` (the fiscal quarter end) is the natural axis because
    the invariant qualifies WHICH rows must have populated datekey, and a
    row's fiscal period is what makes it "for after 1990". Filtering on
    datekey would be a category error: NULL datekey is exactly what we
    are testing for, and NULL comparisons evaluate to NULL/false under
    Polars' SQL-style semantics.

    Plan-reviewer Critical 1 pushed back on this column choice arguing
    that the reader uses datekey as its filter axis. The contract has a
    different rationale (structural invariant vs PIT-query). Documented
    here so a future reader does not silently swap the column.
    """

    name = "sf1_datekey_non_null_after_1990"
    required_tables = frozenset({"sf1"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        sf1 = frames["sf1"].with_columns(
            pl.col("calendardate").cast(pl.Date),
            pl.col("datekey").cast(pl.Date),
        )
        violations = (
            sf1.filter(
                (pl.col("dimension") == "ARQ")
                & (pl.col("calendardate") >= date(1991, 1, 1))
                & pl.col("datekey").is_null()
            )
            .select("ticker", "calendardate", "dimension")
            .sort(["ticker", "calendardate"])
        )
        if violations.height > 0:
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    violations,
                    "SF1 ARQ row(s) with calendardate >= 1991-01-01 and "
                    "NULL datekey",
                )
            )


class NoDuplicateTickerDatekeyInSf1Contract:
    """No duplicate (ticker, datekey, dimension) triples in SF1
    (ADR 0002 dec 12 invariant 4).

    The dimension discriminator is included so ARQ + ART rows that
    legitimately share datekey (they describe different facts at the same
    SEC submission moment) do not register as duplicates. The vendor's
    restatement model is in-place update of the row, so duplicates are a
    schema-change canary.
    """

    name = "no_duplicate_ticker_datekey_in_sf1"
    required_tables = frozenset({"sf1"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        sf1 = frames["sf1"].with_columns(pl.col("datekey").cast(pl.Date))
        counts = sf1.group_by(["ticker", "datekey", "dimension"]).agg(
            pl.len().alias("count")
        )
        violations = counts.filter(pl.col("count") > 1).sort(
            ["ticker", "datekey", "dimension"]
        )
        if violations.height > 0:
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    violations,
                    "(ticker, datekey, dimension) triple(s) with more than "
                    "one SF1 row",
                )
            )


class Sp500EventsResolveToUniqueTickersRowContract:
    """Every SP500 event ticker resolves to exactly one TICKERS row whose
    [firstpricedate, lastpricedate] interval contains the event date
    (ADR 0002 dec 12 invariant 5, tightened per Plan-reviewer High 4).

    The plain-English invariant "every SP500 member has a TICKERS row"
    sounds like ticker-string coverage, but the bug class that actually
    bites is ticker reuse across permatickers: a ticker "FOO" added 1998
    plus a ticker "FOO" added 2015 with TICKERS only carrying the 2015
    permaticker. A naive anti-join on ticker string passes; the universe
    replay state machine then crashes on the 1998 event with
    `UniverseValidationError`. This contract uses the event-date-respecting
    join so the bug class is caught at ingest.

    Violations include both:
    - match_count == 0: no TICKERS row covers the event date (the bug
      class above).
    - match_count > 1: ticker reuse where the event date falls in two
      TICKERS intervals (a vendor data-quality bug we refuse to silently
      resolve to one of the two permatickers).

    Companion contract: `NoDuplicateSp500EventsContract` (PR 5b) covers
    the SP500 (ticker, date, action) uniqueness invariant separately.
    Without that companion a duplicate event row inflates match_count
    here and the resulting failure message wrongly attributes the cause
    to TICKERS-vs-SP500 ambiguity; with the companion, the duplicate
    surfaces as its own named violation. Both contracts run by default
    via `_DEFAULT_CONTRACTS`; alphabetical sort in the runner places
    `no_duplicate_sp500_events` before this contract in aggregated
    messages so the duplicate surfaces first when both fail.
    """

    name = "sp500_events_resolve_to_unique_tickers_row"
    required_tables = frozenset({"sp500", "tickers"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        sp500 = frames["sp500"].with_columns(pl.col("date").cast(pl.Date))
        tickers = frames["tickers"].with_columns(
            pl.col("firstpricedate").cast(pl.Date),
            pl.col("lastpricedate").cast(pl.Date),
        )

        joined = sp500.join(
            tickers.select("ticker", "permaticker", "firstpricedate", "lastpricedate"),
            on="ticker",
            how="left",
        ).with_columns(
            (
                pl.col("permaticker").is_not_null()
                & (
                    pl.col("firstpricedate").is_null()
                    | (pl.col("firstpricedate") <= pl.col("date"))
                )
                & (
                    pl.col("lastpricedate").is_null()
                    | (pl.col("date") <= pl.col("lastpricedate"))
                )
            ).alias("_in_interval")
        )
        match_counts = joined.group_by(["ticker", "date", "action"]).agg(
            pl.col("_in_interval").sum().alias("match_count")
        )
        violations = (
            match_counts.filter(pl.col("match_count") != 1)
            .select(
                "ticker",
                pl.col("date").alias("event_date"),
                "action",
                pl.col("match_count").cast(pl.Int64),
            )
            .sort(["event_date", "ticker", "action"])
        )
        if violations.height > 0:
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    violations,
                    "SP500 event(s) that do not resolve to exactly one "
                    "TICKERS row at the event date (match_count != 1)",
                )
            )


class NoDuplicateSp500EventsContract:
    """No duplicate (ticker, date, action) triples in SP500
    (ADR 0002 dec 12 invariant 6; PR 5b structural fix per PR 5a
    post-impl Medium 2).

    Bug class caught: a vendor pull that double-writes an SP500 event
    row. Without this contract, the duplicate manifests inside
    `Sp500EventsResolveToUniqueTickersRowContract` as a match_count
    of 2+ that the error message frames as ticker reuse, masking the
    real cause. The dedicated contract surfaces the duplicate as its
    own named violation so log-pattern alerts attribute the failure
    to the SP500 table rather than to TICKERS-vs-SP500 ambiguity.

    Alphabetical sort in `run_data_quality_contracts` places this
    contract's name before `sp500_events_resolve_to_unique_tickers_row`
    in any aggregated message, so when both fail the operator sees the
    duplicate-event diagnosis first.
    """

    name = "no_duplicate_sp500_events"
    required_tables = frozenset({"sp500"})

    def check(self, frames: dict[str, pl.DataFrame]) -> None:
        sp500 = frames["sp500"].with_columns(pl.col("date").cast(pl.Date))
        counts = sp500.group_by(["ticker", "date", "action"]).agg(
            pl.len().alias("count")
        )
        violations = counts.filter(pl.col("count") > 1).sort(
            ["ticker", "date", "action"]
        )
        if violations.height > 0:
            raise DataQualityError(
                _format_violation_message(
                    self.name,
                    violations.height,
                    violations,
                    "duplicate (ticker, date, action) triple(s) in SP500",
                )
            )


# Locked iteration order matches the dataset_versioning.md enumeration so
# the canonical contract sequence is grep-able. M3 PR 5b appends the
# NoDuplicateSp500EventsContract last (position 6); aggregated failure
# messages are sorted alphabetically by name at the runner so the tuple
# position only affects per-contract pass/INFO order, not user-visible
# error ordering. Subsequent PRs that want to pass a custom subset call
# `run_data_quality_contracts(source, contracts=(MyContract(),))`.
_DEFAULT_CONTRACTS: tuple[DataQualityContract, ...] = (
    FirstPriceWithinFiveDaysContract(),
    NoSepBarsAfterDelistingContract(),
    Sf1DatekeyNonNullAfter1990Contract(),
    NoDuplicateTickerDatekeyInSf1Contract(),
    Sp500EventsResolveToUniqueTickersRowContract(),
    NoDuplicateSp500EventsContract(),
)


# ----- M3 PR 5a: runner -----


def run_data_quality_contracts(
    source: SharadarDataSource,
    contracts: tuple[DataQualityContract, ...] | None = None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Run every contract that the bundle's tables support; aggregate failures.

    Per ADR 0002 decision 12 the runner is collect-all (not fail-fast):
    an operator pulling a fresh snapshot wants every failure surfaced
    up front, not one at a time. Plan-reviewer Choice B addendum: the
    aggregated message sorts failing contracts alphabetically by name so
    log-pattern alerts are stable.

    Per-table cache: each required table collects exactly once across the
    contracts that need it. Shared-table inventory at v1: TICKERS by
    contracts 1, 2, 5; SEP by 1, 2; SF1 by 3, 4; SP500 by 5 only.

    Args:
      source: a SharadarDataSource (we read `available_tables`,
        `bundle_name`, and `get_table`).
      contracts: optional override of the default contract set. None means
        use `_DEFAULT_CONTRACTS`. The empty tuple is a valid override (the
        runner does nothing and emits an INFO log).
      logger: optional logger override; default is the module's `_LOG`.

    Raises:
      DataQualityError: when one or more contracts fail. The aggregated
        message lists each failing contract's name and offending-row
        sample on its own line; contracts are sorted alphabetically by
        name for determinism.
    """
    log = logger if logger is not None else _LOG
    enabled = contracts if contracts is not None else _DEFAULT_CONTRACTS
    available = source.available_tables
    bundle_name = source.bundle_name

    # Determine which tables we need to materialize. A contract whose
    # required_tables is not a subset of `available` is skipped, so its
    # tables are not added to the materialization set unless another
    # contract also needs them.
    needed: set[str] = set()
    for contract in enabled:
        if contract.required_tables.issubset(available):
            needed.update(contract.required_tables)

    frames: dict[str, pl.DataFrame] = {}
    for table_name in sorted(needed):
        frames[table_name] = source.get_table(table_name).collect()

    failures: list[tuple[str, DataQualityError]] = []
    for contract in enabled:
        missing = contract.required_tables - available
        if missing:
            log.info(
                "skipping data quality contract %r for bundle %r: required "
                "tables not in bundle: %s",
                contract.name,
                bundle_name,
                sorted(missing),
            )
            continue
        try:
            contract.check(frames)
        except DataQualityError as exc:
            log.error(
                "data quality contract %r failed for bundle %r: %s",
                contract.name,
                bundle_name,
                exc,
            )
            failures.append((contract.name, exc))
            continue
        log.info(
            "data quality contract %r passed for bundle %r",
            contract.name,
            bundle_name,
        )

    if failures:
        failures.sort(key=lambda pair: pair[0])
        lines = [
            f"data quality contract(s) failed for bundle {bundle_name!r}:"
        ]
        for name, contract_exc in failures:
            lines.append(f"  - {name}: {contract_exc}")
        raise DataQualityError("\n".join(lines))

    log.info(
        "all %d data quality contracts passed for bundle %r",
        sum(1 for c in enabled if c.required_tables.issubset(available)),
        bundle_name,
    )


# ----- M3 PR 5a: freshness check -----


def check_snapshot_freshness(
    bundle_entry: SnapshotBundleEntry,
    *,
    now: Callable[[], datetime] | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Warn when the bundle's pull_date is older than the freshness threshold.

    Per ADR 0003 decision 16:
    - silent below 30 days
    - WARNING with "consider refreshing" between 30 and 89 days
    - WARNING with "STALE" between 90 days and beyond

    The threshold tiers all use `logging.WARNING` so a single log-level
    subscription catches both states; the "STALE" substring is the loud
    signal for log-pattern alerts.

    Per ADR 0002 decision 11 both timestamps are interpreted as
    America/New_York close (16:00 ET). `pull_date` is a naive
    `datetime.date` from the manifest; the comparison promotes it to a
    naive `datetime` via `datetime.combine(pull_date, time(16, 0))`. The
    helper asserts both inputs are naive so a future caller attaching
    tzinfo to one side does not produce a silent TypeError.

    Args:
      bundle_entry: the manifest entry (already-parsed; ManifestParseError
        upstream guarantees `pull_date` is a `datetime.date` instance).
      now: optional clock injection for deterministic tests. None means
        `datetime.now()`. Tests pass `now=lambda: datetime(2026, 5, 30, 16, 0)`
        to pin the threshold logic.
      logger: optional logger override; default is the module's `_LOG`.
    """
    log = logger if logger is not None else _LOG
    now_fn = now if now is not None else datetime.now
    current_dt = now_fn()
    if current_dt.tzinfo is not None:
        raise ValueError(
            f"check_snapshot_freshness requires a naive datetime per ADR "
            f"0002 decision 11; got tzinfo={current_dt.tzinfo}"
        )
    pull_dt = datetime.combine(bundle_entry.pull_date, time(16, 0))
    delta_days = (current_dt - pull_dt).days
    if delta_days < _FRESHNESS_WARN_DAYS:
        return  # within freshness window; silent
    bundle_label = f"pull_date={bundle_entry.pull_date.isoformat()}"
    if delta_days >= _FRESHNESS_STALE_DAYS:
        log.warning(
            "snapshot is %d days old (%s); STALE, a refresh is strongly "
            "recommended per docs/methodology/dataset_versioning.md",
            delta_days,
            bundle_label,
        )
        return
    log.warning(
        "snapshot is %d days old (%s); consider refreshing per "
        "docs/methodology/dataset_versioning.md",
        delta_days,
        bundle_label,
    )
