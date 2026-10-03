from __future__ import annotations

import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import keyboards
import voice
from languages import LanguageProfile
from states import leave_modes

router = Router()


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, profile: LanguageProfile
) -> None:
    await leave_modes(state)  # /start is a clean reset
    await message.answer(profile.greeting, reply_markup=keyboards.menu_markup(profile))


def _render_page(conn: sqlite3.Connection, user_id: int, page: int,
                 profile: LanguageProfile):
    """Render one vocab page; clamps page after deletions shrink the list."""
    total = db.count_cards(conn, user_id)
    if total == 0:
        if profile.command_menu:
            return "Словарь пуст. Добавь первое слово через /add.", None
        return "Словарь пуст. Добавь первое слово через «➕ Добавить слово».", None
    pages = (total + keyboards.PAGE_SIZE - 1) // keyboards.PAGE_SIZE
    page = max(0, min(page, pages - 1))
    cards = db.list_cards(conn, user_id, limit=keyboards.PAGE_SIZE,
                          offset=page * keyboards.PAGE_SIZE)
    lines = [formatting.word_list_line(i + 1 + page * keyboards.PAGE_SIZE, c)
             for i, c in enumerate(cards)]
    text = (formatting.vocab_title(page, pages, total)
            + "\n\n" + "\n".join(lines))
    return text, keyboards.vocab_keyboard(cards, page, total)


@router.message(F.text == keyboards.BTN_VOCAB)
async def show_vocab(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await leave_modes(state)  # «Мой словарь» leaves any active mode (add or training)
    text, kb = _render_page(conn, message.from_user.id, 0, profile)
    await message.answer(text, reply_markup=kb)


async def _remove_card_voice(call: CallbackQuery, state: FSMContext) -> None:
    """Delete the voice message left by a previously opened card, if any."""
    data = await state.get_data()
    msg_id = data.get("vocab_voice_msg_id")
    if not msg_id:
        return
    await state.update_data(vocab_voice_msg_id=None)
    try:
        await call.message.bot.delete_message(call.message.chat.id, msg_id)
    except TelegramBadRequest:
        pass  # already gone or too old — nothing to clean up


@router.callback_query(F.data.startswith("vocab:"))
async def paginate_vocab(
    call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    """Page navigation; also the «back to list» button on a card."""
    await _remove_card_voice(call, state)
    page = int(call.data.split(":")[1])
    text, kb = _render_page(conn, call.from_user.id, page, profile)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("card:"))
async def show_card(
    call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await _remove_card_voice(call, state)
    _, card_id, page = call.data.split(":")
    card = db.get_card(conn, int(card_id))
    if card is None:
        text, kb = _render_page(conn, call.from_user.id, int(page), profile)
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer("Это слово уже удалено")
        return
    await call.message.edit_text(
        formatting.card_preview(card),
        reply_markup=keyboards.card_detail_keyboard(card["id"], int(page)),
        parse_mode="HTML",
    )
    sent = await voice.send_card_voice(call.message, conn, card, profile.tts_voice)
    if sent is not None:
        await state.update_data(vocab_voice_msg_id=sent.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("del:"))
async def delete_word(
    call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await _remove_card_voice(call, state)
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    db.delete_card(conn, int(parts[1]), user_id=call.from_user.id)
    text, kb = _render_page(conn, call.from_user.id, page, profile)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("Удалено")
