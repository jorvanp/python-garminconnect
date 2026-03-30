"""
Timezone utilities for the Garmin Dashboard application.
Cloud Run defaults to UTC, so all date/time operations must explicitly
specify a timezone.

- Request-time code: use now_tz(user_tz) / today_tz(user_tz) passing the
  user's detected browser timezone (stored in session + Firestore).
- Background threads / cron: use now_cdmx() / today_cdmx() as fallback since
  all current users are in the America/Mexico_City timezone.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CDMX_TZ = ZoneInfo("America/Mexico_City")
DEFAULT_TZ_STR = "America/Mexico_City"


def _resolve_tz(tz_str: str | None) -> ZoneInfo:
    """Returns ZoneInfo for the given IANA string, falling back to CDMX."""
    if tz_str:
        try:
            return ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, KeyError):
            pass
    return CDMX_TZ


def now_cdmx() -> datetime:
    """Returns the current datetime in America/Mexico_City timezone."""
    return datetime.now(CDMX_TZ)


def today_cdmx() -> date:
    """Returns today's date in America/Mexico_City timezone."""
    return datetime.now(CDMX_TZ).date()


def now_tz(tz_str: str | None = None) -> datetime:
    """Returns current datetime in the given IANA timezone, defaulting to CDMX."""
    return datetime.now(_resolve_tz(tz_str))


def today_tz(tz_str: str | None = None) -> date:
    """Returns today's date in the given IANA timezone, defaulting to CDMX."""
    return now_tz(tz_str).date()
