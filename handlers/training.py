from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import intents
import keyboards
import session
import voice
from languages import LanguageProfile
from services import grading, srs
from services.llm import LLM, QuotaExceededError
from states import Training, end_session, leave_modes

router = Router()
EMPTY = ("На сегодня всё повторили! 🎉 Можешь добавить новые слова "
         "или зайти позже.")


async def _show_next_flashcard(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    queue = data.get("queue", [])
    card = None
    while queue and card is None:
        card = db.get_card(conn, queue[0])
        if card is None:
            queue = queue[1:]
    await state.update_data(queue=queue)
    if not queue:
        await end_session(state)
        await message.answer("Все слова повторены — ты молодец! ❤️", reply_markup=keyboards.main_menu())
        return
    await message.answer(f"🎴 {card['word']}")
    await voice.send_card_voice(message, conn, card, profile.tts_voice)
    await message.answer("…вспомни перевод…",
                         reply_markup=keyboards.reveal_keyboard())


@router.message(F.text == keyboards.BTN_FLASHCARDS)
async def start_flashcards(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await leave_modes(state)  # leave any prior mode + drop pending add-previews
    due = db.get_due_cards(conn, message.from_user.id, date.today())
    if not due:
        await message.answer(EMPTY)
        return
    await state.set_state(Training.flashcards)
    await state.update_data(queue=[r["id"] for r in due], retried=[])
    await _show_next_flashcard(message, state, conn, profile)


@router.callback_query(Training.flashcards, F.data == "show_answer")
async def reveal(call: CallbackQuery, state: FSMContext,
                 conn: sqlite3.Connection, profile: LanguageProfile) -> None:
    data = await state.get_data()
    card = db.get_card(conn, data["queue"][0])
    if card is None:
        await call.answer()
        await _show_next_flashcard(call.message, state, conn, profile)
        return
    await call.message.answer(formatting.answer_reveal(card),
                              reply_markup=keyboards.grade_keyboard())
    await call.answer()


@router.callback_query(Training.flashcards, F.data.startswith("grade:"))
async def grade_flashcard(call: CallbackQuery, state: FSMContext,
                          conn: sqlite3.Connection,
                          profile: LanguageProfile) -> None:
    remembered = call.data == "grade:remember"
    data = await state.get_data()
    queue = data["queue"]
    retried = data.get("retried", [])
    card = db.get_card(conn, queue[0])
    if card is None:
        await state.update_data(queue=queue[1:])
        await call.answer()
        await _show_next_flashcard(call.message, state, conn, profile)
        return
    card_id = queue[0]
    if card_id not in retried:  # first encounter drives scheduling
        new_interval = srs.next_interval(card["interval_days"], remembered)
        db.update_review(conn, card_id, interval_days=new_interval,
                         due_at=srs.due_on(date.today(), new_interval),
                         remembered=remembered)
    new_queue, new_retried = session.advance(
        queue, retried, remembered=remembered, giveup=False)
    await state.update_data(queue=new_queue, retried=new_retried)
    await call.answer("👍" if remembered else "Повторим ещё")
    await _show_next_flashcard(call.message, state, conn, profile)


async def _ask_next_translation(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    queue = data.get("queue", [])
    card = None
    while queue and card is None:
        card = db.get_card(conn, queue[0])
        if card is None:
            queue = queue[1:]
    await state.update_data(queue=queue)
    if not queue:
        await end_session(state)
        await message.answer("Все слова повторены — ты молодец! ❤️",
                             reply_markup=keyboards.main_menu())
        return
    await message.answer(profile.translate_question.format(card["translation"]))


@router.message(F.text == keyboards.BTN_TRANSLATE)
async def start_translate(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await leave_modes(state)  # leave any prior mode + drop pending add-previews
    due = db.get_due_cards(conn, message.from_user.id, date.today())
    if not due:
        await message.answer(EMPTY)
        return
    await state.set_state(Training.translate)
    await state.update_data(queue=[r["id"] for r in due], retried=[])
    await _ask_next_translation(message, state, conn, profile)


@router.message(Training.translate, F.text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def check_translation(
    message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    queue = data["queue"]
    retried = data.get("retried", [])
    card = db.get_card(conn, queue[0])
    if card is None:
        await state.update_data(queue=queue[1:])
        await _ask_next_translation(message, state, conn, profile)
        return
    card_id = queue[0]
    giveup = intents.is_giveup(message.text)
    if giveup:
        ok = False
        await message.answer("Ничего страшного ❤️ Запомним вместе:")
        await message.answer(formatting.card_preview(card), parse_mode="HTML")
    elif grading.answers_match(message.text, card["word"]):
        # Right word, only case/space differs (e.g. phone auto-capitalised the
        # first letter) — accept outright, no need to call the model.
        ok = True
        await message.answer("✅ Верно!")
    else:
        try:
            # to_thread: синхронный вызов Gemini не должен блокировать event loop
            verdict = await asyncio.to_thread(
                grading.grade,
                llm, profile, prompt_ru=card["translation"],
                expected=card["word"], answer=message.text.strip(),
            )
            ok = verdict["verdict"] in ("correct", "typo")
            if verdict["verdict"] == "correct":
                await message.answer("✅ Верно!")
            elif verdict["verdict"] == "typo":
                await message.answer(
                    f"✅ Почти! Правильно: {verdict['correct']} "
                    f"({verdict['note']})"
                )
            else:
                await message.answer(
                    f"❌ Не совсем. Правильно: {verdict['correct']} "
                    f"({verdict['note']})"
                )
        except QuotaExceededError:
            # Лимит бесплатного тира: точное сравнение уже не совпало
            # (answers_match выше), считаем как «не вспомнила» без ИИ-комментария.
            ok = False
            await message.answer(
                f"❌ Правильно: {card['word']}\n"
                "(умная проверка пока недоступна — лимит бесплатных запросов; "
                "сравни свой ответ с правильным)"
            )
        except grading.GradingError:
            # Fall back to a forgiving exact-match check if the model returns junk.
            ok = message.text.strip().lower() == card["word"].lower()
            await message.answer("✅ Верно!" if ok
                                 else f"❌ Правильно: {card['word']}")
        except Exception:
            # Не даём боту молчать на непредвиденной ошибке (F1c): та же
            # прощающая деградация, что при GradingError.
            logging.getLogger(__name__).exception("grading failed unexpectedly")
            ok = message.text.strip().lower() == card["word"].lower()
            await message.answer("✅ Верно!" if ok
                                 else f"❌ Правильно: {card['word']}")

    if card_id not in retried:  # first encounter drives scheduling
        new_interval = srs.next_interval(card["interval_days"], ok)
        db.update_review(conn, card_id, interval_days=new_interval,
                         due_at=srs.due_on(date.today(), new_interval),
                         remembered=ok)
    new_queue, new_retried = session.advance(
        queue, retried, remembered=ok, giveup=giveup)
    await state.update_data(queue=new_queue, retried=new_retried)
    await _ask_next_translation(message, state, conn, profile)


async def _ask_next_listen(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    queue = data.get("queue", [])
    card = None
    while queue and card is None:
        card = db.get_card(conn, queue[0])
        if card is None:
            queue = queue[1:]
    await state.update_data(queue=queue)
    if not queue:
        await end_session(state)
        await message.answer("Все слова повторены — ты молодец! ❤️",
                             reply_markup=keyboards.main_menu())
        return
    await message.answer("🔊 Что это за слово? Послушай и напиши:")
    await voice.send_card_voice(message, conn, card, profile.tts_voice)


@router.message(F.text == keyboards.BTN_LISTEN)
async def start_listen(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    await leave_modes(state)  # leave any prior mode + drop pending add-previews
    due = db.get_due_cards(conn, message.from_user.id, date.today())
    if not due:
        await message.answer(EMPTY)
        return
    await state.set_state(Training.listen)
    await state.update_data(queue=[r["id"] for r in due], retried=[])
    await _ask_next_listen(message, state, conn, profile)


@router.message(Training.listen, F.text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def check_listen(
    message: Message, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    data = await state.get_data()
    queue = data["queue"]
    retried = data.get("retried", [])
    card = db.get_card(conn, queue[0])
    if card is None:
        await state.update_data(queue=queue[1:])
        await _ask_next_listen(message, state, conn, profile)
        return
    card_id = queue[0]
    giveup = intents.is_giveup(message.text)
    if giveup:
        ok = False
        await message.answer("Ничего страшного ❤️ Вот это слово:")
        await message.answer(formatting.card_preview(card), parse_mode="HTML")
    else:
        ok = message.text.strip().lower() == card["word"].lower()
        if ok:
            await message.answer(f"✅ Да! 🔤 {card['word']} — {card['translation']}")
        else:
            await message.answer(
                f"Почти! Правильно: {card['word']} — {card['translation']}"
            )
    if card_id not in retried:  # first encounter drives scheduling
        new_interval = srs.next_interval(card["interval_days"], ok)
        db.update_review(conn, card_id, interval_days=new_interval,
                         due_at=srs.due_on(date.today(), new_interval),
                         remembered=ok)
    new_queue, new_retried = session.advance(
        queue, retried, remembered=ok, giveup=giveup)
    await state.update_data(queue=new_queue, retried=new_retried)
    await _ask_next_listen(message, state, conn, profile)
