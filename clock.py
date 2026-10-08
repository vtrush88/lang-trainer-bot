"""Бизнес-дата бота: календарная дата в DAILY_TZ, а не серверная."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

_tz: ZoneInfo | None = None


def configure(tz_name: str | None) -> None:
    """None → локальная зона процесса."""
    global _tz
    _tz = ZoneInfo(tz_name) if tz_name else None


def now() -> datetime:
    return datetime.now(_tz).astimezone() if _tz is None else datetime.now(_tz)


def today() -> date:
    return now().date()
