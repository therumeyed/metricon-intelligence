"""Business-date helpers. Timestamps are stored in UTC; presentation/scheduling decisions use Australia/Sydney."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import get_settings

STATE_TZ = {"VIC": "Australia/Melbourne", "NSW": "Australia/Sydney", "QLD": "Australia/Brisbane", "SA": "Australia/Adelaide"}


def app_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_local(now: datetime | None = None) -> date:
    return (now or now_utc()).astimezone(app_tz()).date()


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def end_of_day(d: date, tz_name: str | None) -> datetime:
    """Instant at which a published expiry date ends, in the source's stated (or assumed) timezone."""
    tz = ZoneInfo(tz_name) if tz_name else app_tz()
    return datetime.combine(d, time(23, 59, 59), tzinfo=tz).astimezone(timezone.utc)


def is_expired(expiry: date | None, tz_name: str | None, now: datetime | None = None) -> bool:
    if expiry is None:
        return False
    return (now or now_utc()) > end_of_day(expiry, tz_name)


def default_tz_for_state(state: str | None) -> str:
    return STATE_TZ.get(state or "", get_settings().app_timezone)
