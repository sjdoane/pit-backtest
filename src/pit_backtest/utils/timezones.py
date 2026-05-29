"""America/New_York timezone convention helpers.

Per ADR 0002 decision 11: all Sharadar dates interpreted as end-of-day
America/New_York (16:00 ET close). available_dt cross-references with
simulation_dt use the convention available_dt <= simulation_dt with both
in America/New_York.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
NYSE_CLOSE_HOUR = 16  # 16:00 ET regular session close
NYSE_CLOSE = time(NYSE_CLOSE_HOUR, 0)


def to_nyse_close(d: date | datetime) -> datetime:
    """Return the 16:00 ET datetime for the given date.

    Accepts either a plain `date` (assumed America/New_York 16:00) or a
    `datetime` (the date portion is used; time portion ignored). The result
    is always timezone-aware in America/New_York.
    """
    if isinstance(d, datetime):
        d = d.date()
    return datetime.combine(d, NYSE_CLOSE, tzinfo=NEW_YORK)


def ensure_ny(dt: datetime) -> datetime:
    """Return dt in America/New_York.

    Naive datetimes are interpreted as already in America/New_York (per the
    project convention). Aware datetimes in other zones are converted.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NEW_YORK)
    return dt.astimezone(NEW_YORK)
