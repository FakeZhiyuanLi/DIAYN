"""
intern_clock.py
~~~~~~~~~~~~~~~
The finder's time zone: DIAYN_TZ. Alert hours are kept in it, the daily
housekeeping turns over at its midnight, and every date and time the finder
shows is in it, with the zone named wherever an hour is shown.

The scraper's configure() reads and checks DIAYN_TZ, and its main() binds it
as `internship_poller.SETTINGS.tz` (default UTC). This module reads it from
there on every call, so the finder and the scraper never disagree, and a test
changes it by replacing SETTINGS.

Not the Gemini quota day, which is the scraper's LLM_DAY_TZ and reaches the
bot through the contract (`postings_source.Quota`).

Importing this module reads nothing: the scraper is imported on the first
call. intern_vocab, which the resume worker imports, stays out of it and is
handed the label instead.
"""

from datetime import timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = "UTC"
#: The prefix of IANA zones named by an offset, which have no city to name them by.
_OFFSET_ZONES = "Etc/"


def zone_name() -> str:
    """DIAYN_TZ as the scraper bound it: an IANA name its configure() has checked."""
    import internship_poller
    return internship_poller.SETTINGS.tz


@lru_cache(maxsize=8)
def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name == UTC:
            return timezone.utc          # a box without tz data still knows UTC
        raise


def zone() -> tzinfo:
    """DIAYN_TZ, for datetime."""
    return _zone(zone_name())


def zone_label(name: str | None = None) -> str:
    """
    How a shown hour names its zone (DIAYN_TZ unless `name` is given): by its
    city, "America/Los_Angeles" -> "Los Angeles time", or as it is when it has
    none, "UTC" or "Etc/GMT+8".
    """
    name = zone_name() if name is None else name
    if "/" not in name or name.startswith(_OFFSET_ZONES):
        return name
    return name.rsplit("/", 1)[1].replace("_", " ") + " time"
