from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import languages

MORNING_HOURS = range(5, 14)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    allowed_user_ids: set[int]
    db_path: str
    bot_lang: str
    daily_at: time | None = None
    daily_tz: str = "Europe/Madrid"
    daily_exclude_ids: set[int] = field(default_factory=set)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def _parse_ids(raw: str) -> set[int]:
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


_HHMM = re.compile(r"^(\d{2}):(\d{2})$")


def _parse_hhmm(raw: str) -> time:
    m = _HHMM.match(raw.strip())
    if not m:
        raise ValueError(f"DAILY_AT must be HH:MM, got {raw!r}")
    try:
        value = time(int(m.group(1)), int(m.group(2)))
    except ValueError as exc:
        raise ValueError(f"DAILY_AT must be HH:MM, got {raw!r}") from exc
    if value.hour not in MORNING_HOURS:
        log.warning("DAILY_AT=%s вне утреннего окна 05:00–13:59 — намеренно?", raw)
    return value


def load() -> Config:
    load_dotenv()
    raw_ids = _require("ALLOWED_USER_IDS")
    ids = _parse_ids(raw_ids)
    # `or`: пустая строка в .env не должна дать неизвестный язык
    bot_lang = os.environ.get("BOT_LANG") or "es"
    if bot_lang not in languages.PROFILES:
        raise ValueError(
            f"Unknown BOT_LANG: {bot_lang!r} (known: {sorted(languages.PROFILES)})")
    raw_daily_at = os.environ.get("DAILY_AT") or ""
    daily_at = _parse_hhmm(raw_daily_at) if raw_daily_at else None
    daily_tz = "Europe/Madrid"
    daily_exclude: set[int] = set()
    if daily_at is not None:
        daily_tz = os.environ.get("DAILY_TZ") or "Europe/Madrid"
        ZoneInfo(daily_tz)  # ZoneInfoNotFoundError — громко на старте
        daily_exclude = _parse_ids(os.environ.get("DAILY_EXCLUDE_IDS", ""))
    return Config(
        telegram_token=_require("TELEGRAM_TOKEN"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        # `or`: пустая строка в .env не должна дать models=("",)
        gemini_model=os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash",
        gemini_fallback_model=os.environ.get(
            "GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
        allowed_user_ids=ids,
        db_path=os.environ.get("DB_PATH", "spanish_bot.db"),
        bot_lang=bot_lang,
        daily_at=daily_at,
        daily_tz=daily_tz,
        daily_exclude_ids=daily_exclude,
    )
