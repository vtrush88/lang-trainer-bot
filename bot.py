from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai
from google.genai import types as genai_types

import clock
import config
import daily
import db
import languages
from handlers import add, commands, menu, training
from handlers import daily as daily_handlers
from services import llm as llm_service


def prepare_db(conn) -> None:
    db.init_db(conn)
    db.release_stale_grading(conn)   # оценок в полёте на старте нет — зомби снимаем


BASE_ROUTERS = (menu.router, add.router, training.router)
DAILY_ROUTER = daily_handlers.router
COMMANDS_ROUTER = commands.router


def build_dispatcher(*, conn, llm, profile: languages.LanguageProfile,
                     base_routers=BASE_ROUTERS, daily_router=DAILY_ROUTER,
                     commands_router=COMMANDS_ROUTER) -> Dispatcher:
    """Роутеры в фиксированном порядке: команды «/» — ПЕРВЫМИ и только для профиля с
    command_menu (иначе «/vocab» в режиме добавления поймает add.receive_text); daily —
    ПОСЛЕДНИМ и только для профиля с daily_practice.

    Роутеры инжектируются ради тестов: aiogram не даёт подключить один Router к двум Dispatcher'ам.
    """
    dp = Dispatcher(storage=MemoryStorage())
    # Inject shared deps into every handler via the data dict.
    dp["conn"] = conn
    dp["llm"] = llm
    dp["profile"] = profile
    if profile.command_menu:
        dp.include_router(commands_router)   # команды из любого режима — раньше режимных хендлеров
    for router in base_routers:
        dp.include_router(router)
    if profile.daily_practice:
        dp.include_router(daily_router)   # ловит свободный текст вне режимов
    return dp


def start_daily_loop(bot, conn, llm, profile, cfg) -> asyncio.Task | None:
    """Таймер только при обоих гейтах (профиль + DAILY_AT). Ссылку на task держит вызывающий."""
    if not daily.should_start_loop(profile, cfg):
        return None
    return asyncio.create_task(daily.daily_loop(bot, conn, llm, profile, cfg))


async def setup_commands(bot, profile: languages.LanguageProfile) -> None:
    """Подсказка «/» только для профиля с command_menu; у es список команд не трогаем вовсе.

    Список косметический: сбой Telegram логируем и продолжаем — старт polling не блокируем."""
    if not profile.command_menu:
        return
    try:
        await bot.set_my_commands(commands.bot_commands())
    except Exception:
        logging.exception("set_my_commands failed; continuing without command list")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = config.load()
    # Бизнес-дата (DAILY_TZ) для обоих ботов — ДО всего, что берёт «сегодня»; единственный владелец.
    clock.configure(cfg.daily_tz)

    conn = db.connect(cfg.db_path)
    prepare_db(conn)
    gemini_client = genai.Client(
        api_key=cfg.gemini_api_key,
        # ms; hung request must not park a to_thread worker forever
        http_options=genai_types.HttpOptions(timeout=30_000),
    )
    models = (cfg.gemini_model,)
    if cfg.gemini_fallback_model:
        models += (cfg.gemini_fallback_model,)
    llm = llm_service.LLM(client=gemini_client, models=models)
    profile = languages.PROFILES[cfg.bot_lang]

    bot = Bot(token=cfg.telegram_token)
    dp = build_dispatcher(conn=conn, llm=llm, profile=profile)

    # Access control: ignore anyone not in the allow-list.
    dp.message.filter(F.from_user.id.in_(cfg.allowed_user_ids))
    dp.callback_query.filter(F.from_user.id.in_(cfg.allowed_user_ids))

    daily_task = start_daily_loop(bot, conn, llm, profile, cfg)
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_commands(bot, profile)
    try:
        await dp.start_polling(bot)
    finally:
        if daily_task is not None:
            daily_task.cancel()
            await asyncio.gather(daily_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
