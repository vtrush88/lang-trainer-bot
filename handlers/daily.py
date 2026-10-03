"""Ежедневная практика: /next, ответы на задание, сбор фраз из свободного текста.

Роутер подключается ПОСЛЕДНИМ и только для профиля с daily_practice=True.
Все хендлеры — вне режимов (StateFilter(None)); меню/add/training имеют приоритет.
Логика — в daily.py; здесь только разбор апдейта и FSM-`pending`.

`pending` общий с превью add: ключи — монотонный `seq`, записи трёх видов (превью add,
превью capture — словари карточки; уточнение clarify — {"text", "task_id"}). Каждый
callback-префикс берёт только запись своего вида."""
from __future__ import annotations

import random
import sqlite3
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import daily
import db
import formatting
import keyboards
from languages import LanguageProfile
from services.llm import LLM

router = Router()
_rng = random.Random()

TEXT_INACTIVE = "Эта карточка уже неактивна 🙂"


async def _reserve(state: FSMContext, user_id: int, entries: list[dict]) -> list[int]:
    """Выдать seq под записи и сохранить их в pending — под локом пользователя, ДО любых
    отправок: два пересланных подряд сообщения не делят один seq."""
    async with daily.user_lock(user_id):
        data = await state.get_data()
        seq = data.get("seq", 0)
        pending = data.get("pending", {})
        seqs = []
        for entry in entries:
            seq += 1
            pending[str(seq)] = entry
            seqs.append(seq)
        await state.update_data(pending=pending, seq=seq)
    return seqs


async def _pop(state: FSMContext, seq: str, key: str) -> dict | None:
    """Забрать запись pending своего вида (у неё есть поле `key`); чужую не трогаем."""
    data = await state.get_data()
    pending = data.get("pending", {})
    entry = pending.get(seq)
    if not isinstance(entry, dict) or key not in entry:
        return None
    del pending[seq]
    await state.update_data(pending=pending)
    return entry


@router.message(Command("next"), StateFilter(None))
async def cmd_next(message: Message, conn: sqlite3.Connection, llm: LLM,
                   profile: LanguageProfile) -> None:
    result = await daily.send_daily_task(message.bot, conn, llm, profile, message.from_user.id,
                                         date.today(), _rng, morning=False)
    if result == "nothing":
        await message.answer(daily.TEXT_NOTHING_NEXT)
    elif result == "failed":
        await message.answer(daily.TEXT_CAPTURE_FAILED)


async def _drop_markup(call: CallbackQuery) -> None:
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(StateFilter(None), F.data.startswith("more:"))
async def on_more(call: CallbackQuery, conn: sqlite3.Connection, llm: LLM,
                  profile: LanguageProfile) -> None:
    await _drop_markup(call)
    await call.answer()   # сразу снять «крутилку»: дальше Gemini + TTS
    today = date.today()
    if call.data.split(":", 1)[1] != today.isoformat():
        await call.message.answer(daily.TEXT_MORE_STALE)
    else:
        result = await daily.send_daily_task(call.message.bot, conn, llm, profile,
                                             call.from_user.id, today, _rng, morning=False,
                                             limit=daily.MAX_TASKS_PER_DAY)
        if result == "limit":
            await call.message.answer(daily.TEXT_MORE_LIMIT)
        elif result == "nothing":
            await call.message.answer(daily.TEXT_MORE_EMPTY)
        elif result == "failed":
            await call.message.answer(daily.TEXT_CAPTURE_FAILED)


async def _capture(message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                   profile: LanguageProfile, text: str, user_id: int) -> None:
    """`message` — куда отвечать; `user_id` — чей словарь (у call.message from_user = бот)."""
    if not text:
        await message.answer(daily.TEXT_EMPTY_CAPTURE)
        return
    items, error = await daily.capture_items(conn, llm, profile, user_id, text)
    if error:
        await message.answer(error)
        return
    if not items:
        await message.answer(daily.TEXT_NOTHING_FOUND)
        return
    seqs = await _reserve(state, user_id, items)
    for seq, item in zip(seqs, items):
        text_out = formatting.card_preview(item)
        if item.get("usage"):
            text_out += f"\n💬 {formatting.esc(item['usage'])}"
        await message.answer(text_out, parse_mode="HTML", reply_markup=keyboards.take_keyboard(seq))


async def _finish(call: CallbackQuery, text: str) -> None:
    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text)
    await call.answer()


@router.callback_query(StateFilter(None), F.data.startswith("take:yes:"))
async def on_take_yes(call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
                      profile: LanguageProfile) -> None:
    item = await _pop(state, call.data.split(":")[2], "word")
    if item is None:
        await call.answer(TEXT_INACTIVE)
        return
    if db.card_exists(conn, call.from_user.id, item["word"]):
        await _finish(call, f"«{item['word']}» уже есть в твоём словаре 🙂")
        return
    db.add_card(conn, user_id=call.from_user.id, kind=item["kind"], word=item["word"],
                translation=item["translation"], transcription=item["transcription"],
                example=item["example"], example_translation=item["example_translation"],
                enriched=True, today=date.today(), context=item.get("context"))
    db.reset_missed(conn, call.from_user.id)   # добавила слово → тихий режим снят
    await _finish(call, daily.TEXT_SAVED)


@router.callback_query(StateFilter(None), F.data.startswith("take:no:"))
async def on_take_no(call: CallbackQuery, state: FSMContext) -> None:
    if await _pop(state, call.data.split(":")[2], "word") is None:
        await call.answer(TEXT_INACTIVE)
        return
    await _finish(call, "Ок, пропускаю 🙂")


@router.callback_query(StateFilter(None), F.data.startswith("clarify:"))
async def on_clarify(call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                     profile: LanguageProfile) -> None:
    _, action, seq = call.data.split(":")
    entry = await _pop(state, seq, "task_id")
    await _drop_markup(call)
    if entry is None:
        await call.answer("Уже неактивно 🙂")
        return
    await call.answer()   # сразу снять «крутилку»: дальше Gemini (+ TTS)
    uid = call.from_user.id   # не call.message.from_user — там бот
    if action == "capture":
        await _capture(call.message, state, conn, llm, profile, entry["text"], uid)
    else:
        task = db.open_task(conn, uid)
        result = "stale"
        if task is not None and task["id"] == entry["task_id"]:
            result = await daily.answer_task(call.message.bot, conn, llm, profile, uid,
                                             entry["text"], date.today(), giveup=False,
                                             task_id=entry["task_id"])
        if result == "stale":   # "busy" (ещё оценивается/уже отвечено) — молча
            await call.message.answer(daily.TEXT_CLARIFY_STALE)


@router.message(StateFilter(None), F.text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def on_free_text(message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                       profile: LanguageProfile) -> None:
    uid = message.from_user.id
    task = db.open_task(conn, uid)   # снимок ДО лока: ответ идёт именно этой задаче
    targets, stale = (), False
    if task is not None:   # дельта (р): длинный текст без слова / просроченное /next → переспросить
        card = db.get_card(conn, task["card_id"])
        targets = (task["phrase_form"], card["word"] if card is not None else None)
        stale = daily.is_stale(task, daily._utcnow())
    verdict = daily.classify_incoming(message.text, forwarded=message.forward_origin is not None,
                                      has_open_task=task is not None, target=targets, stale=stale)
    if verdict == "ignore":
        return
    if verdict == "capture":
        await _capture(message, state, conn, llm, profile,
                       daily.strip_capture_prefix(message.text), uid)
    elif verdict in ("giveup", "answer"):
        result = await daily.answer_task(message.bot, conn, llm, profile, uid, message.text,
                                         date.today(), giveup=(verdict == "giveup"),
                                         task_id=task["id"])
        if result == "stale":   # утро успело сменить задание
            await message.answer(daily.TEXT_CLARIFY_STALE)
        # "busy": второй быстрый ответ, пока оценивается первый, — молча (спека)
    else:  # clarify
        (seq,) = await _reserve(state, uid, [{"text": message.text, "task_id": task["id"]}])
        await message.answer(daily.TEXT_CLARIFY, reply_markup=keyboards.clarify_keyboard(seq))


@router.message(StateFilter(None), ~F.text)
async def reject_non_text(message: Message) -> None:
    await message.answer(daily.TEXT_ONLY_TEXT)


@router.callback_query(F.data.startswith(("more:", "take:", "clarify:")))
async def daily_button_inside_mode(call: CallbackQuery) -> None:
    """Кнопка daily нажата внутри режима добавления/тренировки (StateFilter(None) не
    пропустил) — не оставляем «крутилку», подсказываем выйти командой «/» (daily — только en)."""
    await call.answer("Сначала выйди из режима — выбери любую команду в меню «/» 🙂")
