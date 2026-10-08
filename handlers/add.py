from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import clock
import db
import formatting
import keyboards
from languages import LanguageProfile
from services import enrichment, tts
from services.llm import LLM, QuotaExceededError
from states import AddCard, leave_modes

router = Router()


@router.message(F.text == keyboards.BTN_ADD)
async def start_add(
    message: Message, state: FSMContext, profile: LanguageProfile
) -> None:
    await leave_modes(state)  # drop stale previews so their «✅ Да» goes inert
    await state.set_state(AddCard.waiting_for_text)
    await message.answer(profile.add_intro)


@router.message(AddCard.waiting_for_text, F.text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def receive_text(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    llm: LLM, profile: LanguageProfile,
) -> None:
    text = message.text.strip()
    try:
        # to_thread: синхронный вызов Gemini не должен блокировать event loop
        card = await asyncio.to_thread(enrichment.enrich, llm, profile, text)
    except QuotaExceededError:
        # 429 на обеих моделях: дневной лимит ИЛИ минутный всплеск —
        # не обещаем «завтра», предлагаем и «позже».
        await message.answer(
            "Лимит бесплатных ИИ-запросов пока исчерпан 😕 "
            "Попробуй позже или завтра."
        )
        return
    except enrichment.EnrichmentError:
        # Stay in waiting_for_text; next message retries. Save-as-is +
        # later re-enrichment is deferred (see out-of-MVP improvements).
        await message.answer(
            "Не получилось обработать сейчас 😕 Попробуй ещё раз через минутку "
            "или пришли другое слово."
        )
        return
    except Exception:
        # Не даём боту молчать на непредвиденной ошибке (F1c): та же тёплая
        # деградация, что при EnrichmentError; слово не сохраняется.
        logging.getLogger(__name__).exception("enrichment failed unexpectedly")
        await message.answer(
            "Не получилось обработать сейчас 😕 Попробуй ещё раз через минутку "
            "или пришли другое слово."
        )
        return

    if db.card_exists(conn, message.from_user.id, card["word"]):
        # Stay in waiting_for_text so the next word can be sent straight away,
        # without tapping «Добавить слово» again. Menu buttons still escape
        # (handled at the top of this function).
        await message.answer(
            f"«{card['word']}» уже есть в твоём словаре 🙂 Пришли другое слово."
        )
        return

    data = await state.get_data()
    seq = data.get("seq", 0) + 1            # monotonic; never reset (token uniqueness)
    pending = data.get("pending", {})
    pending[str(seq)] = card
    await state.update_data(pending=pending, seq=seq)
    await message.answer(formatting.card_preview(card), parse_mode="HTML")
    await _send_voice(message, card["word"], profile.tts_voice)
    await message.answer("Сохранить?",
                         reply_markup=keyboards.save_card_keyboard(seq))


async def _send_voice(message: Message, word: str, voice: str) -> None:
    """Best-effort audio; silent text-only fallback on failure.

    Sent as a voice message (Bot API ≥7.2 accepts MP3 in sendVoice): voice
    bubbles don't join the chat-wide music playlist, so playing one word
    never auto-plays the others.
    """
    tmp = os.path.join(tempfile.gettempdir(), f"tts_{os.getpid()}_{abs(hash(word))}.mp3")
    try:
        await tts.synthesize(word, voice, tmp)
        with open(tmp, "rb") as fh:
            await message.answer_voice(
                BufferedInputFile(fh.read(), filename="произношение.mp3")
            )
    except (tts.TTSError, OSError, TelegramBadRequest):
        await message.answer("🔇 (озвучка временно недоступна)")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def _finish_preview(call: CallbackQuery, text: str) -> None:
    """Replace the «Сохранить?» prompt with the result, dropping the buttons.

    edit_text omits reply_markup, so the inline keyboard disappears. Falls back
    to a fresh message if the original is too old to edit (pattern from
    menu.py:_remove_card_voice).
    """
    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text)


@router.callback_query(F.data.startswith("save:yes:"))
async def save_yes(
    call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    pending = data.get("pending", {})
    card = pending.pop(call.data.split(":")[2], None)
    if card is None:
        await call.answer("Эта карточка уже неактивна 🙂")
        return
    await state.update_data(pending=pending)  # consumed: read-once + per-preview scope
    if db.card_exists(conn, call.from_user.id, card["word"]):
        # Already saved (e.g. a second unsaved preview of the same word, or added
        # since this preview was created) — don't duplicate.
        await _finish_preview(call, f"«{card['word']}» уже есть в твоём словаре 🙂")
        await call.answer()
        return
    db.add_card(
        conn, user_id=call.from_user.id, kind=card["kind"],
        word=card["word"], translation=card["translation"],
        transcription=card["transcription"], example=card["example"],
        example_translation=card["example_translation"],
        enriched=True, today=clock.today(),
    )
    if profile.daily_practice:
        db.reset_missed(conn, call.from_user.id)  # добавила слово любым путём → тихий режим снят
    await _finish_preview(call, "Сохранено! ✅ Пиши следующее 🙂")
    await call.answer()


@router.callback_query(F.data.startswith("save:no:"))
async def save_no(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending", {})
    card = pending.pop(call.data.split(":")[2], None)
    if card is None:
        await call.answer("Эта карточка уже неактивна 🙂")
        return
    await state.update_data(pending=pending)
    await _finish_preview(call, "Ок, пропускаю 🙂 Пиши следующее.")
    await call.answer()


@router.callback_query(F.data.in_({"save:yes", "save:no"}))
async def save_legacy(call: CallbackQuery) -> None:
    # Tokenless buttons from messages sent before this deploy — answer gracefully.
    await call.answer("Эта карточка уже неактивна 🙂")


@router.message(AddCard.waiting_for_text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def reject_non_text(message: Message) -> None:
    await message.answer("Я понимаю пока только текст 🙂 Напиши слово или фразу.")
