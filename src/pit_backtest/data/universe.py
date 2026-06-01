"""Universe protocol and v1 SharadarSP500Universe (snapshot-based).

Per ADR 0001 decision 9 and ADR 0003 decision 6: typed PIT membership API
with is_member, members_at, and membership_spells. Backed at v1 by the
Sharadar SP500 quarterly membership snapshots.

Snapshot model (ADR 0017, supersedes the M3 PR 4 event-replay model):
The real Sharadar SP500 table publishes point-in-time membership DIRECTLY
as quarterly snapshots, not only as an add/drop event log. It carries four
`action` types: `historical` (a membership snapshot at each quarter-end,
~500 rows per quarter-end), `current` (the latest roster as of the pull),
plus `added`/`removed` (the effective-date event log). The snapshots are
the primary membership source; the event log is validated separately as a
cross-check (`Sp500AddedRemovedCrossCheckContract`).

`members_at(t)` returns the membership of the most-recent `historical` or
`current` snapshot whose date is `<= t`. Before the first snapshot the
universe is empty. Membership therefore updates at quarterly resolution:
a name added intra-quarter does not appear until the next quarter-end
snapshot (the staleness lag is 0 to ~92 days). This is deliberately
conservative; the model lags reality and never leads it, so it cannot leak
look-ahead (every quarter-end roster was effective on or before its date;
ADR 0017 records the empirical confirmation).

AssetId resolution (ADR 0017 decision): each snapshot member ticker is
resolved to an AssetId via the resolver's date-agnostic
`resolve_ticker_unique`, NOT the date-gated `resolve_ticker`. A handful of
snapshot quarter-ends sit one to five trading days outside the member's
`[firstpricedate, lastpricedate]` price interval (spin-offs trading
when-issued just before their first regular-way bar; acquisition targets
delisted a day or two before the quarter-end removal), so date-gated
resolution would raise on a legitimate member. Date-agnostic resolution is
safe because every snapshot ticker resolves to exactly one permaticker (no
within-snapshot ticker reuse); the `Sp500SnapshotMembersResolveContract`
asserts that uniqueness invariant at ingest, so this is a checked
guarantee, not silent masking.

Construction-time validation (`UniverseValidationError`):
- A snapshot member ticker is absent from TICKERS entirely (the resolver
  has no row for it). Common cause: SP500 and TICKERS bundles pulled at
  different vintages.
- A snapshot member ticker maps to more than one permaticker (genuine
  ticker reuse the resolver refuses to silently disambiguate).
Both chain via `raise ... from exc` so the underlying resolver diagnostic
is preserved.

Consumer contract (ADR 0017): `members_at` returns membership; a consumer
that needs the ticker as traded on a specific date (the momentum signal
resolves AssetId to ticker at the rebalance date to read SEP prices) will
omit a member that has no tradeable price at that date (a name delisted
between the quarterly snapshot and the rebalance). That omission is
economically necessary for a long-only study and is bounded (about 0.18%
of member-month observations over 2005-2024, all delisting-lag); the M5
study reports the per-rebalance omission count rather than hiding it.

Cross-references:
- ADR 0017 (snapshot-based universe; this module).
- ADR 0003 dec 6 (Universe Protocol shape; the surface is unchanged).
- `docs/methodology/dataset_versioning.md` Sharadar SP500 table.
- `docs/methodology/determinism.md` Requirement 3 (sorted output) +
  Requirement 4 (no set iteration in output paths; the per-date frozenset
  here is used for membership tests only, never iterated into output).
"""

from __future__ import annotations

from bisect import bisect_right
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

        The end_dt is `None` for open-ended spells (the asset is a member
        as of the latest snapshot). The Protocol return type carries the
        None honestly rather than relying on a magic `datetime.max`
        sentinel.

        Under the v1 snapshot model (ADR 0017) spell boundaries are
        quarter-end snapshot dates, accurate to the containing calendar
        quarter (the effective-date error is 0 to about 92 days). A spell
        is NOT a tradable effective-date interval; the finer event-log
        effective dates are a v1.1 reconciliation.
        """
        ...


class UniverseValidationError(ValueError):
    """Raised when a Universe instance fails its construction-time checks.

    Per ADR 0017 the two snapshot-model failure modes are:
    1. A snapshot member ticker is absent from TICKERS (the resolver has
       no AssetId for it). The message names the bundle and hints that
       mismatched SP500/TICKERS pull dates are a common cause.
    2. A snapshot member ticker maps to more than one permaticker (ticker
       reuse the resolver refuses to silently disambiguate).

    The M3 PR 4 event-replay failure modes (double-add, remove-without-add,
    unknown-action) no longer exist: the snapshot model consumes membership
    directly and does not replay a state machine, so a ticker simply
    appears in the snapshots that list it. The add/drop event log is
    validated separately by `Sp500AddedRemovedCrossCheckContract`.

    Each failure surfaces the offending ticker (and the bundle name) in the
    args so a future debug session can grep it without re-reading the
    SP500 parquet.
    """


def _date_to_eod_datetime(d: date) -> datetime:
    """Promote a Sharadar snapshot date to America/New_York 16:00 ET.

    Matches the convention `_row_date_to_datetime` from
    `pit_backtest.data.sources.sharadar` (ADR 0002 decision 11). Locally
    defined so the universe module does not depend on a sources-specific
    helper at runtime.
    """
    return datetime.combine(d, time(16, 0))


class SharadarSP500Universe:
    """v1 Universe backed by the Sharadar SP500 quarterly membership snapshots.

    Reads the SP500 table at construction, keeps every `historical` and
    `current` snapshot as a (snapshot_date -> sorted AssetId tuple) map,
    and answers membership by an as-of lookup: the most-recent snapshot on
    or before the query date. See the module docstring for the snapshot
    model, the date-agnostic resolution rationale, and the quarterly
    staleness semantics (all locked in ADR 0017).

    Determinism: `members_at` returns from the per-date sorted tuple;
    `is_member` tests the per-date frozenset (a membership-only sidecar,
    never iterated into output). Both are built once from a sorted source
    frame, so two constructions over the same input are identical.
    """

    __slots__ = (
        "_snapshots",
        "_snapshot_dates",
        "_membership_sets",
        "_bundle_name",
    )

    def __init__(self, source: SharadarDataSource) -> None:
        """Construct from a SharadarDataSource so the snapshot SHA256
        commitment in `docs/methodology/dataset_versioning.md` is the
        vintage gate (matches the resolver pattern from M3 PR 1).

        For tests that do not want the parquet + manifest dance, use
        `SharadarSP500Universe.from_lazy_frame(...)`.

        Raises:
          FileNotFoundError: when the bundle does not include sp500.parquet
            (propagates from `source.get_table('sp500')`).
          UniverseValidationError: per the module docstring failure modes.
        """
        sp500_lf = source.get_table("sp500")
        resolver = source._resolver
        self._bundle_name = source.bundle_name
        (
            self._snapshots,
            self._snapshot_dates,
            self._membership_sets,
        ) = self._build_snapshots(sp500_lf, resolver, self._bundle_name)

    @classmethod
    def from_lazy_frame(
        cls,
        sp500_lf: pl.LazyFrame,
        resolver: IdentifierResolver,
        bundle_name: str = "<test>",
    ) -> SharadarSP500Universe:
        """Alternate constructor for tests; caller accepts vintage
        responsibility (no SHA256 gate).

        The resolver is an explicit parameter, not derived from a
        `SharadarDataSource`, so tests can wire a synthetic resolver via
        `SharadarPermatickerResolver.from_lazy_frame` against a synthetic
        TICKERS LazyFrame.
        """
        instance = cls.__new__(cls)
        instance._bundle_name = bundle_name
        (
            instance._snapshots,
            instance._snapshot_dates,
            instance._membership_sets,
        ) = cls._build_snapshots(sp500_lf, resolver, bundle_name)
        return instance

    @staticmethod
    def _build_snapshots(
        sp500_lf: pl.LazyFrame,
        resolver: IdentifierResolver,
        bundle_name: str,
    ) -> tuple[
        dict[date, tuple[AssetId, ...]],
        tuple[date, ...],
        dict[date, frozenset[AssetId]],
    ]:
        # Cast date to pl.Date BEFORE any other operation (project rule 12;
        # the M1 hotfix at fix/adapter-date-filter-and-pandas-pin). Keep
        # only the snapshot actions; `added`/`removed` feed the separate
        # cross-check contract, not membership. `.unique()` collapses any
        # accidental duplicate (ticker, snapshot-date) row defensively (a
        # genuine duplicate is also caught at ingest by
        # NoDuplicateSp500EventsContract).
        materialized = (
            sp500_lf.with_columns(pl.col("date").cast(pl.Date))
            .filter(pl.col("action").is_in(["historical", "current"]))
            .select(pl.col("ticker"), pl.col("date"))
            .unique()
            .sort(["date", "ticker"])
            .collect()
        )

        # Resolve each DISTINCT snapshot ticker to an AssetId exactly once
        # (about 1,200 lookups on the real bundle, not one per snapshot
        # row). Date-agnostic resolution per ADR 0017; the sorted ticker
        # list keeps the resolution order deterministic.
        distinct_tickers = (
            materialized.get_column("ticker").unique().sort().to_list()
        )
        ticker_to_asset: dict[str, AssetId] = {
            ticker: _resolve_snapshot_ticker(resolver, ticker, bundle_name)
            for ticker in distinct_tickers
        }

        # Group members by snapshot date. A dict keyed on date dedupes
        # snapshot dates (so a future bundle that repeats a quarter-end
        # would MERGE the rows rather than silently pick one); the
        # intermediate set dedupes AssetIds within a date.
        by_date: dict[date, set[AssetId]] = {}
        for row in materialized.iter_rows(named=True):
            by_date.setdefault(row["date"], set()).add(
                ticker_to_asset[row["ticker"]]
            )

        snapshot_dates = tuple(sorted(by_date.keys()))
        snapshots: dict[date, tuple[AssetId, ...]] = {}
        membership_sets: dict[date, frozenset[AssetId]] = {}
        for snapshot_date in snapshot_dates:
            asset_ids = by_date[snapshot_date]
            # sorted(...) is deterministic regardless of set order; the
            # frozenset is for membership tests only (never iterated into
            # output) so it does not violate Determinism Requirement 4.
            snapshots[snapshot_date] = tuple(sorted(asset_ids, key=int))
            membership_sets[snapshot_date] = frozenset(asset_ids)
        return snapshots, snapshot_dates, membership_sets

    def __repr__(self) -> str:
        member_rows = sum(len(ids) for ids in self._snapshots.values())
        first = self._snapshot_dates[0].isoformat() if self._snapshot_dates else "none"
        last = self._snapshot_dates[-1].isoformat() if self._snapshot_dates else "none"
        return (
            f"SharadarSP500Universe(bundle={self._bundle_name!r}, "
            f"snapshots={len(self._snapshot_dates)} [{first}..{last}], "
            f"member_rows={member_rows})"
        )

    def _asof_snapshot_date(self, lookup_date: date) -> date | None:
        """The most-recent snapshot date on or before lookup_date, or None."""
        index = bisect_right(self._snapshot_dates, lookup_date) - 1
        if index < 0:
            return None
        return self._snapshot_dates[index]

    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        snapshot_date = self._asof_snapshot_date(lookup_date)
        if snapshot_date is None:
            return False
        return asset_id in self._membership_sets[snapshot_date]

    def members_at(self, dt: datetime) -> list[AssetId]:
        lookup_date = dt.date() if isinstance(dt, datetime) else dt
        snapshot_date = self._asof_snapshot_date(lookup_date)
        if snapshot_date is None:
            return []
        # Copy the cached sorted tuple so callers cannot mutate state.
        return list(self._snapshots[snapshot_date])

    def membership_spells(
        self, asset_id: AssetId
    ) -> list[tuple[datetime, datetime | None]]:
        # A spell is a maximal run of consecutive snapshot dates in which
        # the asset is present. The run closes at the last present date on
        # a present->absent transition; a run still open after the final
        # snapshot means the asset is in the latest roster, so the spell is
        # open-ended (end=None). Boundaries are quarter-end snapshot dates
        # (ADR 0017): a spell is NOT a tradable effective-date interval.
        spells: list[tuple[datetime, datetime | None]] = []
        run_start: date | None = None
        run_last: date | None = None
        for snapshot_date in self._snapshot_dates:
            if asset_id in self._membership_sets[snapshot_date]:
                if run_start is None:
                    run_start = snapshot_date
                run_last = snapshot_date
            elif run_start is not None:
                assert run_last is not None  # set whenever run_start is
                spells.append(
                    (
                        _date_to_eod_datetime(run_start),
                        _date_to_eod_datetime(run_last),
                    )
                )
                run_start = None
                run_last = None
        if run_start is not None:
            # An open run at loop end means the asset is present at the
            # final (most-recent) snapshot, so it is a current member.
            spells.append((_date_to_eod_datetime(run_start), None))
        return spells


def _resolve_snapshot_ticker(
    resolver: IdentifierResolver,
    ticker: str,
    bundle_name: str,
) -> AssetId:
    """Resolve a snapshot member ticker to an AssetId; wrap resolver errors.

    Per ADR 0017 the resolution is date-agnostic (`resolve_ticker_unique`)
    so the quarter-end-just-outside-the-price-interval boundary members
    resolve. The two failure modes chain via `raise ... from exc` so the
    underlying resolver diagnostic stays in the traceback.
    """
    try:
        return resolver.resolve_ticker_unique(ticker)
    except TickerNotFoundError as exc:
        raise UniverseValidationError(
            f"SP500 snapshot lists ticker {ticker!r} but the resolver has "
            f"no AssetId for it (bundle={bundle_name!r}). Common cause: "
            f"SP500 and TICKERS bundles were pulled at different vintages; "
            f"re-pull both to the same date and verify."
        ) from exc
    except ValueError as exc:
        raise UniverseValidationError(
            f"SP500 snapshot ticker {ticker!r} maps to multiple permatickers "
            f"(ticker reuse) so the universe cannot resolve a single AssetId "
            f"(bundle={bundle_name!r}). {exc}"
        ) from exc
