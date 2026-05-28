"""America/New_York timezone convention helpers.

Per ADR 0002 decision 11: all Sharadar dates interpreted as end-of-day
America/New_York (16:00 ET close). available_dt cross-references with
simulation_dt use the convention available_dt <= simulation_dt with both
in America/New_York.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
NYSE_CLOSE_HOUR = 16  # 16:00 ET regular session close


def to_nyse_close(d: date) -> datetime:
    """Return the 16:00 ET datetime for the given date."""
    raise NotImplementedError("M1 deliverable")
