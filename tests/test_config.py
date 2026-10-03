import pytest

import config


def test_load_parses_env(monkeypatch):
    # Neutralize load_dotenv so a real local .env can't leak into the test.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok123")
    monkeypatch.setenv("GEMINI_API_KEY", "key456")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222 ,333")
    cfg = config.load()
    assert cfg.telegram_token == "tok123"
    assert cfg.gemini_api_key == "key456"
    assert cfg.allowed_user_ids == {111, 222, 333}


def test_missing_required_raises(monkeypatch):
    # Neutralize load_dotenv so a real local .env can't repopulate the var.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    import pytest
    with pytest.raises(ValueError, match="TELEGRAM_TOKEN"):
        config.load()


def test_load_defaults_db_path(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.delenv("DB_PATH", raising=False)
    cfg = config.load()
    assert cfg.db_path == "spanish_bot.db"


def test_load_reads_db_path(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("DB_PATH", "/var/lib/spanish-bot/spanish_bot.db")
    cfg = config.load()
    assert cfg.db_path == "/var/lib/spanish-bot/spanish_bot.db"


def test_gemini_model_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    cfg = config.load()
    assert cfg.gemini_model == "gemini-3.5-flash"
    assert cfg.gemini_fallback_model == "gemini-3.5-flash-lite"


def test_gemini_model_overrides(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.0-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "")
    cfg = config.load()
    assert cfg.gemini_model == "gemini-3.0-flash"
    assert cfg.gemini_fallback_model == ""


def test_empty_gemini_model_falls_back_to_default(monkeypatch):
    # Пустая GEMINI_MODEL= в .env не должна дать models=("",)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_MODEL", "")
    cfg = config.load()
    assert cfg.gemini_model == "gemini-3.5-flash"


def test_bot_lang_defaults_to_es(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.delenv("BOT_LANG", raising=False)
    cfg = config.load()
    assert cfg.bot_lang == "es"


def test_bot_lang_en_is_picked_up(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("BOT_LANG", "en")
    cfg = config.load()
    assert cfg.bot_lang == "en"


def test_bot_lang_unknown_raises(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("BOT_LANG", "de")
    with pytest.raises(ValueError, match="BOT_LANG"):
        config.load()


def test_bot_lang_empty_string_falls_back_to_es(monkeypatch):
    # Пустая BOT_LANG= в .env не должна ронять старт.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("BOT_LANG", "")
    cfg = config.load()
    assert cfg.bot_lang == "es"


import logging
from datetime import time


def _base_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    for var in ("DAILY_AT", "DAILY_TZ", "DAILY_EXCLUDE_IDS"):
        monkeypatch.delenv(var, raising=False)


def test_daily_off_by_default_and_other_vars_not_validated(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_TZ", "Mars/Olympus")   # мусор не должен мешать выключенной фиче
    cfg = config.load()
    assert cfg.daily_at is None
    assert cfg.daily_exclude_ids == set()


def test_daily_at_parsed_with_defaults(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    cfg = config.load()
    assert cfg.daily_at == time(9, 30)
    assert cfg.daily_tz == "Europe/Madrid"
    assert cfg.daily_exclude_ids == set()


def test_daily_exclude_ids_parsed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    monkeypatch.setenv("DAILY_EXCLUDE_IDS", "5, 6")
    assert config.load().daily_exclude_ids == {5, 6}


@pytest.mark.parametrize("raw", ["9h", "25:00", "09:60", "", "nine", "9:30", "09:3"])
def test_malformed_daily_at_fails_fast(monkeypatch, raw):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", raw)
    if raw == "":
        assert config.load().daily_at is None   # пустая строка = выключено
        return
    with pytest.raises(ValueError, match="DAILY_AT"):
        config.load()


def test_bad_daily_tz_fails_fast_when_enabled(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    monkeypatch.setenv("DAILY_TZ", "Mars/Olympus")
    with pytest.raises(Exception):   # ZoneInfoNotFoundError
        config.load()


def test_evening_daily_at_warns_but_loads(monkeypatch, caplog):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "20:00")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = config.load()
    assert cfg.daily_at == time(20, 0)
    assert any("DAILY_AT" in r.message for r in caplog.records)
