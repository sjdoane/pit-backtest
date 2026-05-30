"""Universe protocol and v1 SharadarSP500Universe.

Per ADR 0001 decision 9 and ADR 0003 architecture sketch: typed PIT
membership API with is_member, members_at, and membership_spells. Backed
at v1 by the Sharadar SP500 event log.

Membership date semantics (locked in M3 PR 4):
- `added` event date = FIRST day of membership (inclusive).
- `removed` event date = LAST day of membership (inclusive).
- An "added" with no subsequent "removed" produces an open-ended interval
  encoded as `(start_date, None)`; the Protocol return type allows the
  None to surface honestly rather than via a magic `datetime.max` sentinel.
- Same-date "added" + "removed" pair for the same ticker (rare vendor
  edge) produces a one-day interval `(date, date)` so the engine still
  receives the audit trail rather than silently dropping the pair.

Construction-time validation (`UniverseValidationError`):
- Double-add for the same ticker without an intervening "removed".
- Remove-without-add for the same ticker.
- Unknown `action` string (anything not in {"added", "removed"}).
- `TickerNotFoundError` from the resolver at the event-row date; the
  underlying error chains via `raise ... from exc` so the original
  resolver diagnostic is preserved.

AssetId resolution at interval OPEN date (Plan-reviewer Top 4): the
resolver returns the AssetId that owned the ticker at the open date of
the interval; ticker reuse cases produce two intervals on two different
AssetIds, NOT one interval on the most-recent AssetId. The Universe
stores AssetId at open time so subsequent `is_member` / `members_at`
calls do not re-resolve (a silent-wrong-answer risk).

Cross-references:
- ADR 0003 dec 6 (Universe Protocol shape).
- `docs/methodology/dataset_versioning.md:28` Sharadar SP500 event log.
- `docs/methodology/determinism.md` Requirement 3 (sorted output) +
  Requirement 4 (no set iteration; this module uses dicts + lists only).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING, Protocol

import polars as pl

from pit_backtest.data.records import AssetId
from pit_backtest.data.resolver import (
    IdentifierResolver,
    TickerNotFoundError,
)

if TYPE_CHECKING:
    from pit_backtest.data.sources.sharadar import SharadarDataSource


class Universe(Protocol):
    """Point-in-time universe membership API."""

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        """True if asset_id was a member of this universe at dt."""
        ...

    def members_at(self, dt: datetime) -> list[AssetId]:
        """Every asset_id that was a member at dt. Sorted for determinism."""
        ...

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime | None]]:
        """Every (start_dt, end_dt) interval during which asset_id was a member.

        Per M3 PR 4: the end_dt is `None` for open-ended intervals
        (asset is still a member as of construction). The Protocol return
        type carries the None honestly rather than relying on a magic
        `datetime.max` sentinel.
        """
        ...


class UniverseValidationError(ValueError):
    """Raised when a Universe instance fails its construction-time checks.

    Per M3 PR 4 the four named failure modes are:
    1. Double-add for the same ticker without an intervening removed.
    2. Remove-without-add for the same ticker.
    3. Unknown `action` string in an SP500 event row.
    4. Resolver does not recognize the ticker at the event date (vendor
       data quality bug; the error includes the bundle name and a hint
       that mismatched pull dates between SP500 and TICKERS bundles is a
       common cause).

    Each failure surfaces the (date, ticker) pair in the args so a future
    debug session can grep the offending event without re-reading the
    SP500 parquet.
    """


def _date_to_eod_datetime(d: date) -> datetime:
    """Promote a Sharadar event-row date to America/New_York 16:00 ET.

    Matches the convention `_row_date_to_datetime` from
    `pit_backtest.data.sources.sharadar` (ADR 0002 decision 11). Locally
    defined so the universe module does not depend on a sources-specific
    helper at runtime.
    """
    return datetime.combine(d, time(16, 0))


class SharadarSP500Universe:
    """v1 Universe backed by the Sharadar SP500 event log.

    Reads the SP500 event log at construction, resolves each event's
    ticker to an AssetId at the event date via the data source's lazy
    `_resolver`, and replays the events into per-asset membership
    intervals. The replay state machine raises `UniverseValidationError`
    on double-add, remove-without-add, unknown action, or resolver
    failure (see module docstring for the locked failure-mode list).

    Per Plan-reviewer Critical 1 the test fixtures use a CLOSED interval
    for AGG (not a multi-interval AGG that would conflate SP500
    membership with TICKERS lifecycle); multi-interval testing happens
    in inline bundles with synthetic tickers (e.g., "MULTI") in
    `tests/data/test_universe.py`.
    """

    __slots__ = ("_intervals", "_bundle_name")

    def __init__(self, source: SharadarDataSource) -> None:
        """Construct from a SharadarDataSource so the snapshot SHA256
        commitment in `docs/methodology/dataset_versioning.md` is the
        vintage gate (matches the resolver pattern from M3 PR 1).

        For tests that do not want the parquet + manifest dance, use
        `SharadarSP500Universe.from_lazy_frame(...)`.

        Raises:
          FileNotFoundError: when the bundle does not include sp500.parquet
            (propagates from `source.get_table('sp500')` per the M3 PR 4
            Plan-reviewer gotcha 6).
          UniverseValidationError: per the module docstring failure modes.
        """
        sp500_lf = source.get_table("sp500")
        resolver = source._resolver
        self._bundle_name = source.bundle_name
        self._intervals = self._build_intervals(
            sp500_lf, resolver, self._bundle_name
        )

    @classmethod
    def from_lazy_frame(
        cls,
        sp500_lf: pl.LazyFrame,
        resolver: IdentifierResolver,
        bundle_name: str = "<test>",
    ) -> SharadarSP500Universe:
        """Alternate constructor for tests; caller accepts vintage
        responsibility (no SHA256 gate).

        Per Plan-reviewer Medium 6 the resolver is an explicit parameter,
        not derived from a `SharadarDataSource`, so tests can wire a
        synthetic resolver via `SharadarPermatickerResolver.from_lazy_frame`
        against a synthetic TICKERS LazyFrame.
        """
        instance = cls.__new__(cls)
        instance._bundle_name = bundle_name
        instance._intervals = cls._build_intervals(
            sp500_lf, resolver, bundle_name
        )
        return instance

    @staticmethod
    def _build_intervals(
        sp500_lf: pl.LazyFrame,
        resolver: IdentifierResolver,
        bundle_name: str,
    ) -> dict[AssetId, list[tuple[date, date | None]]]:
        # Cast date to pl.Date BEFORE any other operation (project rule 12;
        # the M1 hotfix at fix/adapter-date-filter-and-pandas-pin).
        # Sort by (date, action, ticker): lexicographic on lowercase action
        # strings puts "added" before "removed" within the same date, which
        # is the locked same-date ordering semantic per the M3 PR 4 plan.
        materialized = (
            sp500_lf.with_columns(pl.col("date").cast(pl.Date))
            .select(pl.col("ticker"), pl.col("date"), pl.col("action"))
            .sort(["date", "action", "ticker"])
            .collect()
        )

        # State machine: per-ticker open-interval tracking. Keyed by TICKER
        # (string), NOT AssetId, because a double-add with the same ticker
        # is the vendor bug to catch; keying on AssetId would mask it if
        # ticker reuse remapped the second add to a different permaticker.
        open_starts: dict[str, date] = {}
        intervals: dict[AssetId, list[tuple[date, date | None]]] = {}

        for row in materialized.iter_rows(named=True):
            ticker = row["ticker"]
            event_date = row["date"]
            action = row["action"]

            if action == "added":
                if ticker in open_starts:
                    raise UniverseValidationError(
                        f"SP500 event log double-add for ticker {ticker!r} "
                        f"at {event_date.isoformat()}; previous open from "
                        f"{open_starts[ticker].isoformat()} "
                        f"(bundle={bundle_name!r})"
                    )
                open_starts[ticker] = event_date
                continue

            if action == "removed":
                if ticker not in open_starts:
                    raise UniverseValidationError(
                        f"SP500 event log remove-without-add for ticker "
                        f"{ticker!r} at {event_date.isoformat()} "
                        f"(bundle={bundle_name!r})"
                    )
                start_date = open_starts.pop(ticker)
                asset_id = _resolve_event_ticker(
                    resolver, ticker, start_date, bundle_name
                )
                intervals.setdefault(asset_id, []).append(
                    (start_date, event_date)
                )
                continue

            raise UniverseValidationError(
                f"SP500 event log unknown action {action!r} for ticker "
                f"{ticker!r} at {event_date.isoformat()}; expected 'added' "
                f"or 'removed' (bundle={bundle_name!r})"
            )

        # Close any still-open intervals as open-ended (end=None).
        for ticker, start_date in open_starts.items():
            asset_id = _resolve_event_ticker(
                resolver, ticker, start_date, bundle_name
            )
            intervals.setdefault(asset_id, []).append((start_date, None))

        # Sort each per-asset interval list by start_date ascending; re-key
        # the dict into sorted-AssetId insertion order for deterministic
        # iteration per Determinism Requirement 3.
        sorted_intervals: dict[AssetId, list[tuple[date, date | None]]] = {}
        for asset_id in sorted(intervals.keys(), key=int):
            asset_intervals = intervals[asset_id]
            asset_intervals.sort(key=lambda pair: pair[0])
            sorted_intervals[asset_id] = asset_intervals
        return sorted_intervals

    def __repr__(self) -> str:
        interval_count = sum(len(ivs) for ivs in self._intervals.values())
        return (
            f"SharadarSP500Universe(bundle={self._bundle_name!r}, "
            f"assets={len(self._intervals)}, intervals={interval_count})"
        )

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        intervals = self._intervals.get(asset_id)
        if intervals is None:
            return False
        for first, last in intervals:
            if first <= lookup_date and (last is None or lookup_date <= last):
                return True
        return False

    def members_at(self, dt: datetime) -> list[AssetId]:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        members: list[AssetId] = []
        for asset_id, intervals in self._intervals.items():
            for first, last in intervals:
                if first <= lookup_date and (last is None or lookup_date <= last):
                    members.append(asset_id)
                    break
        return sorted(members, key=int)

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime | None]]:
        intervals = self._intervals.get(asset_id, [])
        return [
            (
                _date_to_eod_datetime(first),
                _date_to_eod_datetime(last) if last is not None else None,
            )
            for first, last in intervals
        ]


def _resolve_event_ticker(
    resolver: IdentifierResolver,
    ticker: str,
    event_date: date,
    bundle_name: str,
) -> AssetId:
    """Resolve a ticker at an SP500 event date; wrap TickerNotFoundError.

    Per Plan-reviewer Counter on Choice 3: the resolver failure chains
    via `raise ... from exc` so the underlying TickerNotFoundError stays
    in the traceback. Per High 3 the error message names bundle_name and
    hints at SP500/TICKERS pull-date drift.
    """
    try:
        return resolver.resolve_ticker(
            ticker, _date_to_eod_datetime(event_date)
        )
    except TickerNotFoundError as exc:
        raise UniverseValidationError(
            f"SP500 event log references ticker {ticker!r} at "
            f"{event_date.isoformat()} but resolver has no AssetId at "
            f"that date (bundle={bundle_name!r}). Common cause: SP500 "
            f"and TICKERS bundles were pulled at different vintages; "
            f"re-pull both to the same date and verify."
        ) from exc
