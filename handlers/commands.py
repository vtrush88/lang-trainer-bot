"""Команды Telegram «/» — меню en-бота вместо reply-клавиатуры.

Роутер подключается ПЕРВЫМ и только для профиля с command_menu=True (см. bot.build_dispatcher):
иначе «/vocab», набранное в режиме добавления, поймал бы add.receive_text и отправил в Gemini
как слово. Хендлеры без StateFilter — команда работает из любого режима; вся логика — в
существующих хендлерах, сюда только делегирование. Мамин es-бот этот роутер не видит."""
from __future__ import annotations

import sqlite3

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, Message

from handlers import add, menu, training
from handlers import daily as daily_handlers
from languages import LanguageProfile
from services.llm import LLM
from states import leave_modes

router = Router()


def bot_commands() -> list[BotCommand]:
    """Список для bot.set_my_commands (подсказка «/» в клиенте Telegram)."""
    return [
        BotCommand(command="next", description="новое слово"),
        BotCommand(command="add", description="добавить слово или фразу"),
        BotCommand(command="vocab", description="мой словарь"),
        BotCommand(command="cards", description="карточки"),
        BotCommand(command="check", description="проверить себя"),
        BotCommand(command="listen", description="аудирование"),
    ]


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext, profile: LanguageProfile) -> None:
    await add.start_add(message, state, profile)


@router.message(Command("vocab"))
async def cmd_vocab(message: Message, state: FSMContext, conn: sqlite3.Connection,
                    profile: LanguageProfile) -> None:
    await menu.show_vocab(message, state, conn, profile)


@router.message(Command("cards"))
async def cmd_cards(message: Message, state: FSMContext, conn: sqlite3.Connection,
                    profile: LanguageProfile) -> None:
    await training.start_flashcards(message, state, conn, profile)


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext, conn: sqlite3.Connection,
                    profile: LanguageProfile) -> None:
    await training.start_translate(message, state, conn, profile)


@router.message(Command("listen"))
async def cmd_listen(message: Message, state: FSMContext, conn: sqlite3.Connection,
                     profile: LanguageProfile) -> None:
    await training.start_listen(message, state, conn, profile)


@router.message(Command("next"))
async def cmd_next(message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                   profile: LanguageProfile) -> None:
    await leave_modes(state)   # /next из режима: сначала выйти, иначе ответ на задание уйдёт в режим
    await daily_handlers.cmd_next(message, conn, llm, profile)
