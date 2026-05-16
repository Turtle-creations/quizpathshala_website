from datetime import datetime, timezone
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Kolkata"
DISPLAY_FORMAT = "%d %b %Y, %I:%M %p"
_USER_TIMEZONE_KEYS = ("timezone", "timezone_name", "time_zone", "tz")


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    raw_value = str(value).strip()
    if not raw_value:
        return None

    normalized = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def resolve_user_timezone(user: dict | None, default_timezone: str = DEFAULT_TIMEZONE) -> str:
    if isinstance(user, dict):
        for key in _USER_TIMEZONE_KEYS:
            candidate = str(user.get(key) or "").strip()
            if candidate:
                try:
                    ZoneInfo(candidate)
                    return candidate
                except Exception:
                    continue
    return default_timezone


def format_user_datetime(
    value: str | None,
    user: dict | None = None,
    *,
    default_timezone: str = DEFAULT_TIMEZONE,
    fallback: str = "-",
) -> str:
    parsed = parse_utc_timestamp(value)
    if not parsed:
        return fallback

    timezone_name = resolve_user_timezone(user, default_timezone=default_timezone)
    try:
        localized = parsed.astimezone(ZoneInfo(timezone_name))
    except Exception:
        localized = parsed.astimezone(ZoneInfo(default_timezone))

    return localized.strftime(DISPLAY_FORMAT)
