"""Проводка в bot.py: роутер daily — последним и только для en; цикл — только при двух гейтах."""
import asyncio
from datetime import time
from types import SimpleNamespace

from aiogram import Router

import bot as bot_module
from handlers import daily as daily_handlers
from languages import PROFILES


def _fresh_routers():
    # Router нельзя подключить к двум Dispatcher'ам (aiogram: «Router is already attached»),
    # поэтому в тестах — свежие экземпляры; порядок и гейт проверяем на них.
    return [Router(), Router(), Router()], Router()


def test_build_dispatcher_includes_daily_router_last_for_en():
    base, daily_router = _fresh_routers()
    dp = bot_module.build_dispatcher(conn=None, llm=None, profile=PROFILES["en"],
                                     base_routers=base, daily_router=daily_router)
    assert dp.sub_routers[:3] == base and dp.sub_routers[-1] is daily_router
    assert dp["profile"] is PROFILES["en"]


def test_build_dispatcher_excludes_daily_router_for_es():
    base, daily_router = _fresh_routers()
    dp = bot_module.build_dispatcher(conn=None, llm=None, profile=PROFILES["es"],
                                     base_routers=base, daily_router=daily_router)
    assert daily_router not in dp.sub_routers and dp.sub_routers == base


def test_build_dispatcher_twice_in_one_session_does_not_reattach():
    # Два Dispatcher'а в одной сессии (en и es) — без «Router is already attached».
    for lang in ("en", "es", "en"):
        base, daily_router = _fresh_routers()
        dp = bot_module.build_dispatcher(conn="c", llm="l", profile=PROFILES[lang],
                                         base_routers=base, daily_router=daily_router)
        assert dp["conn"] == "c" and dp["llm"] == "l"


def test_build_dispatcher_defaults_are_the_real_routers():
    assert bot_module.BASE_ROUTERS == (bot_module.menu.router, bot_module.add.router,
                                       bot_module.training.router)
    assert bot_module.DAILY_ROUTER is daily_handlers.router


async def test_start_daily_loop_gates(monkeypatch):
    started = []

    async def fake_loop(*a, **k):
        started.append(1)
        await asyncio.sleep(3600)
    monkeypatch.setattr(bot_module.daily, "daily_loop", fake_loop)
    on = SimpleNamespace(daily_at=time(9, 30))
    off = SimpleNamespace(daily_at=None)
    assert bot_module.start_daily_loop(None, None, None, PROFILES["es"], on) is None
    assert bot_module.start_daily_loop(None, None, None, PROFILES["en"], off) is None
    task = bot_module.start_daily_loop(None, None, None, PROFILES["en"], on)
    assert task is not None
    await asyncio.sleep(0)
    assert started == [1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_release_stale_grading_called_on_startup(conn, monkeypatch):
    called = []
    monkeypatch.setattr(bot_module.db, "release_stale_grading", lambda c: called.append(c) or 0)
    bot_module.prepare_db(conn)
    assert called == [conn]


async def test_main_wires_prepare_build_loop_and_cancels_loop(conn, monkeypatch):
    """main(): prepare_db → build_dispatcher(профиль из BOT_LANG) → цикл при гейтах → cancel в finally."""
    from unittest.mock import AsyncMock, MagicMock

    cfg = SimpleNamespace(db_path=":memory:", gemini_api_key="k", gemini_model="m",
                          gemini_fallback_model=None, bot_lang="en", telegram_token="t",
                          allowed_user_ids={1}, daily_at=time(9, 30))
    monkeypatch.setattr(bot_module.config, "load", lambda: cfg)
    monkeypatch.setattr(bot_module.db, "connect", lambda path: conn)
    prepared = []
    monkeypatch.setattr(bot_module, "prepare_db", lambda c: prepared.append(c))
    monkeypatch.setattr(bot_module.genai, "Client", MagicMock())
    fake_bot = MagicMock()
    fake_bot.delete_webhook = AsyncMock()
    monkeypatch.setattr(bot_module, "Bot", MagicMock(return_value=fake_bot))

    loop_state = {}

    async def fake_loop(bot, c, llm, profile, cfg_):
        loop_state["running"] = (bot, c, profile, cfg_)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            loop_state["cancelled"] = True
            raise
    monkeypatch.setattr(bot_module.daily, "daily_loop", fake_loop)

    built = {}

    async def start_polling(bot):
        await asyncio.sleep(0)
        assert "running" in loop_state      # цикл уже запущен во время polling

    def fake_build(*, conn, llm, profile):
        built.update(conn=conn, profile=profile)
        dp = MagicMock()
        dp.start_polling = AsyncMock(side_effect=start_polling)
        return dp
    monkeypatch.setattr(bot_module, "build_dispatcher", fake_build)

    await bot_module.main()
    assert prepared == [conn]
    assert built == {"conn": conn, "profile": PROFILES["en"]}
    assert loop_state["running"][0] is fake_bot and loop_state["running"][2] is PROFILES["en"]
    assert loop_state.get("cancelled") is True
