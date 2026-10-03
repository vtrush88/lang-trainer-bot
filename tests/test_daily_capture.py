"""Сбор фраз (daily.capture_items) и хендлеры handlers/daily.py, вызванные напрямую."""
import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter

import daily
import db
from handlers import add as add_handlers
from handlers import daily as daily_handlers
from languages import PROFILES
from services import capture, sentences
from services.llm import QuotaExceededError

EN = PROFILES["en"]
U = 111
TODAY = date(2026, 10, 5)
ITEM = {"kind": "phrase", "word": "a heads-up", "translation": "п", "transcription": "/x/",
        "example": "Just a heads-up.", "example_translation": "э", "context": "созвон", "usage": "обычно: …"}


async def test_capture_items_dedups_against_vocab(conn, monkeypatch):
    db.add_card(conn, user_id=U, kind="phrase", word="A Heads-Up", translation="п", transcription="x",
                example="e", example_translation="э", enriched=True, today=date(2026, 10, 1))
    monkeypatch.setattr(capture, "extract", lambda llm, p, text: [dict(ITEM), {**ITEM, "word": "fresh one"},
                                                                  {**ITEM, "word": "Fresh One"}])
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert err is None and [i["word"] for i in items] == ["fresh one"]   # и дубль внутри ответа снят


async def test_capture_items_quota_and_error_texts(conn, monkeypatch):
    def quota(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(capture, "extract", quota)
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert items == [] and "Лимит" in err

    def broken(*a, **k):
        raise capture.CaptureError("x")
    monkeypatch.setattr(capture, "extract", broken)
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert items == [] and "Не получилось" in err

    def crashed(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(capture, "extract", crashed)
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert items == [] and err == daily.TEXT_CAPTURE_FAILED


async def test_capture_items_empty_is_not_an_error(conn, monkeypatch):
    monkeypatch.setattr(capture, "extract", lambda *a, **k: [])
    assert await daily.capture_items(conn, None, EN, U, "привет") == ([], None)


# ---- хендлеры ----

def _call(data, user_id=U):
    call = MagicMock()
    call.data = data
    call.from_user.id = user_id
    call.message.answer = AsyncMock()
    call.message.edit_text = AsyncMock()
    call.message.edit_reply_markup = AsyncMock()
    call.answer = AsyncMock()
    return call


def _state(data):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data)
    state.update_data = AsyncMock()
    return state


class DictState:
    """FSM-заглушка с настоящим хранением и уступкой петли в каждом вызове (гонки)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get_data(self):
        await asyncio.sleep(0)
        return dict(self.data)

    async def update_data(self, **kw):
        await asyncio.sleep(0)
        self.data.update(kw)


def _message(text, *, forwarded=False, user_id=U):
    message = MagicMock()
    message.text = text
    message.forward_origin = object() if forwarded else None
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


def _today(monkeypatch, d=TODAY):
    monkeypatch.setattr(daily_handlers, "date", SimpleNamespace(today=lambda: d))


def _card(conn, word="x"):
    return db.add_card(conn, user_id=U, kind="phrase", word=word, translation="п", transcription="t",
                       example="e", example_translation="э", enriched=True, today=date(2026, 10, 1))


def _task(conn, cid):
    return db.create_task(conn, user_id=U, card_id=cid, kind="compose", sentence=None, sentence_ru=None,
                          phrase_form=None, from_example=False, today=TODAY, morning=True)


async def test_on_more_stale_date_answers_without_sending(conn, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(daily, "send_daily_task", send)
    _today(monkeypatch, date(2026, 10, 6))
    call = _call("more:2026-10-05")
    await daily_handlers.on_more(call, conn, None, EN)
    send.assert_not_awaited()
    assert call.message.answer.await_args.args[0] == daily.TEXT_MORE_STALE
    call.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    call.answer.assert_awaited()


async def test_on_more_passes_limit_and_reports_results(conn, monkeypatch):
    for result, text in (("limit", daily.TEXT_MORE_LIMIT), ("nothing", daily.TEXT_MORE_EMPTY),
                         ("failed", daily.TEXT_CAPTURE_FAILED)):
        send = AsyncMock(return_value=result)
        monkeypatch.setattr(daily, "send_daily_task", send)
        _today(monkeypatch)
        call = _call("more:2026-10-05")
        await daily_handlers.on_more(call, conn, None, EN)
        assert send.await_args.kwargs["limit"] == daily.MAX_TASKS_PER_DAY
        assert send.await_args.kwargs["morning"] is False
        assert call.message.answer.await_args.args[0] == text
        call.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_on_more_sent_is_silent_and_markup_errors_ignored(conn, monkeypatch):
    monkeypatch.setattr(daily, "send_daily_task", AsyncMock(return_value="sent"))
    _today(monkeypatch)
    call = _call("more:2026-10-05")
    call.message.edit_reply_markup = AsyncMock(side_effect=TelegramBadRequest(method=None, message="old"))
    await daily_handlers.on_more(call, conn, None, EN)
    call.message.answer.assert_not_awaited()
    call.answer.assert_awaited()


async def test_cmd_next_reports_nothing_and_failed(conn, monkeypatch):
    for result, text in (("nothing", daily.TEXT_NOTHING_NEXT), ("failed", daily.TEXT_CAPTURE_FAILED)):
        send = AsyncMock(return_value=result)
        monkeypatch.setattr(daily, "send_daily_task", send)
        message = _message("/next")
        await daily_handlers.cmd_next(message, conn, None, EN)
        assert send.await_args.kwargs["morning"] is False and send.await_args.args[4] == U
        assert message.answer.await_args.args[0] == text


async def test_on_clarify_answer_with_stale_task_refuses(conn, monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(daily, "answer_task", answer)
    live = _task(conn, _card(conn))
    call = _call("clarify:answer:4")
    state = _state({"pending": {"4": {"text": "старый ответ", "task_id": live - 1}}})
    await daily_handlers.on_clarify(call, state, conn, None, EN)
    answer.assert_not_awaited()
    assert call.message.answer.await_args.args[0] == daily.TEXT_CLARIFY_STALE


async def test_on_clarify_answer_passes_stored_task_id(conn, monkeypatch):
    answer = AsyncMock(return_value="done")
    monkeypatch.setattr(daily, "answer_task", answer)
    _today(monkeypatch)
    live = _task(conn, _card(conn))
    call = _call("clarify:answer:4")
    state = _state({"pending": {"4": {"text": "мой ответ", "task_id": live}}})
    await daily_handlers.on_clarify(call, state, conn, None, EN)
    assert answer.await_args.args[4:6] == (U, "мой ответ")
    assert answer.await_args.kwargs == {"giveup": False, "task_id": live}
    assert state.update_data.await_args.kwargs["pending"] == {}   # запись израсходована


async def test_on_clarify_unknown_seq_is_inert(conn, monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(daily, "answer_task", answer)
    call = _call("clarify:answer:9")
    await daily_handlers.on_clarify(call, _state({"pending": {}}), conn, None, EN)
    answer.assert_not_awaited()
    call.message.answer.assert_not_awaited()
    call.answer.assert_awaited()


async def test_on_clarify_capture_uses_user_id_not_bot(conn, monkeypatch):
    seen = []

    async def fake_capture_items(conn_, llm, profile, user_id, text):
        seen.append(user_id)
        return [], None
    monkeypatch.setattr(daily, "capture_items", fake_capture_items)
    call = _call("clarify:capture:4", user_id=U)
    call.message.from_user.id = 999999   # бот
    state = _state({"pending": {"4": {"text": "heads-up", "task_id": 1}}})
    await daily_handlers.on_clarify(call, state, conn, None, EN)
    assert seen == [U]
    assert call.message.answer.await_args.args[0] == daily.TEXT_NOTHING_FOUND


async def test_free_text_plus_only_asks_for_word_without_gemini(conn, monkeypatch):
    for text in ("+", "+   "):
        items = AsyncMock()
        monkeypatch.setattr(daily, "capture_items", items)
        message = _message(text)
        await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
        items.assert_not_awaited()
        assert message.answer.await_args.args[0] == daily.TEXT_EMPTY_CAPTURE


async def test_free_text_capture_sends_previews_with_take_buttons(conn, monkeypatch):
    async def fake_items(conn_, llm, profile, user_id, text):
        assert text == "give me a heads-up"   # «+» снят
        return [dict(ITEM), {**ITEM, "word": "<b>x</b>", "usage": ""}], None
    monkeypatch.setattr(daily, "capture_items", fake_items)
    state = DictState({"seq": 5, "pending": {"5": {"word": "старое"}}})
    message = _message("+ give me a heads-up")
    await daily_handlers.on_free_text(message, state, conn, None, EN)
    assert state.data["seq"] == 7 and set(state.data["pending"]) == {"5", "6", "7"}
    first, second = message.answer.await_args_list
    assert "📍 контекст: созвон" in first.args[0] and "💬 обычно: …" in first.args[0]
    assert first.kwargs["parse_mode"] == "HTML"
    assert first.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "take:yes:6"
    assert "&lt;b&gt;x&lt;/b&gt;" in second.args[0] and "💬" not in second.args[0]
    assert second.kwargs["reply_markup"].inline_keyboard[0][1].callback_data == "take:no:7"


async def test_free_text_capture_error_and_nothing_found(conn, monkeypatch):
    monkeypatch.setattr(daily, "capture_items", AsyncMock(return_value=([], "ошибка")))
    message = _message("+ что-то")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    assert message.answer.await_args.args[0] == "ошибка"
    monkeypatch.setattr(daily, "capture_items", AsyncMock(return_value=([], None)))
    message = _message("+ что-то")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    assert message.answer.await_args.args[0] == daily.TEXT_NOTHING_FOUND


async def test_two_forwarded_messages_together_get_distinct_seqs(conn, monkeypatch):
    async def fake_items(conn_, llm, profile, user_id, text):
        await asyncio.sleep(0)
        return [{**ITEM, "word": text}], None
    monkeypatch.setattr(daily, "capture_items", fake_items)
    state = DictState()
    m1, m2 = _message("one", forwarded=True), _message("two", forwarded=True)
    await asyncio.gather(daily_handlers.on_free_text(m1, state, conn, None, EN),
                         daily_handlers.on_free_text(m2, state, conn, None, EN))
    assert state.data["seq"] == 2
    assert {v["word"] for v in state.data["pending"].values()} == {"one", "two"}
    seqs = {m.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            for m in (m1, m2)}
    assert seqs == {"take:yes:1", "take:yes:2"}


async def test_free_text_answer_passes_pre_lock_task_id(conn, monkeypatch):
    answer = AsyncMock(return_value="done")
    monkeypatch.setattr(daily, "answer_task", answer)
    _today(monkeypatch)
    tid = _task(conn, _card(conn))
    message = _message("I gave them a heads-up")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    assert answer.await_args.kwargs == {"giveup": False, "task_id": tid}
    assert answer.await_args.args[4:7] == (U, "I gave them a heads-up", TODAY)
    message.answer.assert_not_awaited()


async def test_free_text_answer_rejected_by_swap_says_stale(conn, monkeypatch):
    monkeypatch.setattr(daily, "answer_task", AsyncMock(return_value="stale"))
    _task(conn, _card(conn))
    message = _message("I gave them a heads-up")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    assert message.answer.await_args.args[0] == daily.TEXT_CLARIFY_STALE


async def test_free_text_giveup_marks_giveup(conn, monkeypatch):
    answer = AsyncMock(return_value="done")
    monkeypatch.setattr(daily, "answer_task", answer)
    tid = _task(conn, _card(conn))
    await daily_handlers.on_free_text(_message("не знаю"), _state({}), conn, None, EN)
    assert answer.await_args.kwargs == {"giveup": True, "task_id": tid}


async def test_free_text_cyrillic_with_open_task_asks_to_clarify(conn, monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(daily, "answer_task", answer)
    tid = _task(conn, _card(conn))
    state = DictState({"seq": 2})
    message = _message("я дала им heads-up")
    await daily_handlers.on_free_text(message, state, conn, None, EN)
    answer.assert_not_awaited()
    assert state.data == {"seq": 3, "pending": {"3": {"text": "я дала им heads-up", "task_id": tid}}}
    assert message.answer.await_args.args[0] == daily.TEXT_CLARIFY
    kb = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["clarify:answer:3", "clarify:capture:3"]


async def test_free_text_command_is_ignored(conn, monkeypatch):
    monkeypatch.setattr(daily, "capture_items", AsyncMock())
    message = _message("/unknown")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    message.answer.assert_not_awaited()


async def test_take_yes_saves_with_context_and_resets_missed(conn, monkeypatch):
    _today(monkeypatch)
    db.bump_missed(conn, U)
    call = _call("take:yes:3")
    state = _state({"pending": {"3": dict(ITEM)}})
    await daily_handlers.on_take_yes(call, state, conn, EN)
    assert db.card_exists(conn, U, "a heads-up")
    row = conn.execute("SELECT context FROM cards WHERE user_id=?", (U,)).fetchone()
    assert row["context"] == "созвон"
    assert db.get_daily_state(conn, U)["missed_streak"] == 0
    call.message.edit_text.assert_awaited_once_with(daily.TEXT_SAVED)


async def test_take_yes_duplicate_and_inert(conn, monkeypatch):
    _card(conn, "a heads-up")
    call = _call("take:yes:3")
    await daily_handlers.on_take_yes(call, _state({"pending": {"3": dict(ITEM)}}), conn, EN)
    assert "уже есть" in call.message.edit_text.await_args.args[0]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    call = _call("take:yes:8")
    await daily_handlers.on_take_yes(call, _state({"pending": {}}), conn, EN)
    call.answer.assert_awaited_once_with("Эта карточка уже неактивна 🙂")


async def test_take_no_consumes_pending(conn):
    call = _call("take:no:3")
    state = _state({"pending": {"3": dict(ITEM)}})
    await daily_handlers.on_take_no(call, state)
    assert state.update_data.await_args.kwargs["pending"] == {}
    call.message.edit_text.assert_awaited_once()
    assert not db.card_exists(conn, U, "a heads-up")


async def test_reject_non_text_answers_only_text():
    message = _message(None)
    await daily_handlers.reject_non_text(message)
    assert message.answer.await_args.args[0] == daily.TEXT_ONLY_TEXT


async def test_daily_button_inside_mode_answers_callback():
    call = _call("take:yes:1")
    await daily_handlers.daily_button_inside_mode(call)
    call.answer.assert_awaited_once()
    call.message.answer.assert_not_awaited()


def _has_state_none(handler) -> bool:
    return any(isinstance(f.callback, StateFilter) and tuple(f.callback.states) == (None,)
               for f in handler.filters)


def test_all_daily_handlers_only_outside_modes_except_fallback():
    r = daily_handlers.router
    callbacks = {h.callback.__name__: h for h in r.callback_query.handlers}
    messages = {h.callback.__name__: h for h in r.message.handlers}
    for name in ("on_more", "on_take_yes", "on_take_no", "on_clarify"):
        assert _has_state_none(callbacks[name]), name
    for name in ("cmd_next", "on_free_text", "reject_non_text"):
        assert _has_state_none(messages[name]), name
    # фолбэк для кнопок внутри режима — без StateFilter и ПОСЛЕДНИМ среди callback'ов
    last = r.callback_query.handlers[-1]
    assert last.callback is daily_handlers.daily_button_inside_mode and not _has_state_none(last)
    assert r.message.handlers[-1].callback is daily_handlers.reject_non_text


async def test_save_yes_resets_missed_only_for_daily_profile(conn, monkeypatch):
    db.bump_missed(conn, U); db.bump_missed(conn, U)
    card = {"kind": "word", "word": "mesa", "translation": "стол", "transcription": "м",
            "example": "e", "example_translation": "э"}
    call = _call("save:yes:1")
    state = _state({"pending": {"1": dict(card)}})
    await add_handlers.save_yes(call, state, conn, PROFILES["es"])
    assert db.get_daily_state(conn, U)["missed_streak"] == 2      # es: не трогаем
    assert db.card_exists(conn, U, "mesa")
    call = _call("save:yes:2")
    state = _state({"pending": {"2": {**card, "word": "silla"}}})
    await add_handlers.save_yes(call, state, conn, PROFILES["en"])
    assert db.get_daily_state(conn, U)["missed_streak"] == 0      # en: сброшен


# ---- финальная волна: busy-ответ, ранний call.answer, seq после тренировки ----

async def test_free_text_busy_answer_is_silent(conn, monkeypatch):
    monkeypatch.setattr(daily, "answer_task", AsyncMock(return_value="busy"))
    _task(conn, _card(conn))
    message = _message("I gave them a heads-up")
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    message.answer.assert_not_awaited()


async def test_on_clarify_busy_answer_is_silent(conn, monkeypatch):
    monkeypatch.setattr(daily, "answer_task", AsyncMock(return_value="busy"))
    live = _task(conn, _card(conn))
    call = _call("clarify:answer:4")
    await daily_handlers.on_clarify(call, _state({"pending": {"4": {"text": "ответ", "task_id": live}}}),
                                    conn, None, EN)
    call.message.answer.assert_not_awaited()
    call.answer.assert_awaited_once_with()


async def test_second_quick_answer_during_grading_is_silently_ignored(conn, monkeypatch):
    """Спека, «Крайние случаи»: два ответа подряд — второй, пришедший во время оценки
    первого, молча игнорируется (без «задание истекло» и без второй оценки)."""
    from tests.test_daily_send import FakeBot

    async def slow_to_thread(fn, *args, **kwargs):
        await asyncio.sleep(0.02)
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", slow_to_thread)
    graded = []
    monkeypatch.setattr(sentences, "check_sentence",
                        lambda *a, **k: graded.append(a[3]) or {"verdict": "good", "corrected": "",
                                                                "note": "ок", "reply_sentence": None})
    _today(monkeypatch)
    tid = _task(conn, _card(conn))
    bot = FakeBot()
    first, second = _message("I gave them a heads-up"), _message("I gave them a heads-up!")
    first.bot = second.bot = bot
    t1 = asyncio.create_task(daily_handlers.on_free_text(first, _state({}), conn, None, EN))
    await asyncio.sleep(0.005)          # первая уже в оценке (grading)
    assert db.get_task(conn, tid)["status"] == "grading"
    t2 = asyncio.create_task(daily_handlers.on_free_text(second, _state({}), conn, None, EN))
    await asyncio.gather(t1, t2)
    assert graded == ["I gave them a heads-up"]          # второй оценки нет
    first.answer.assert_not_awaited()
    second.answer.assert_not_awaited()                   # и никакого TEXT_CLARIFY_STALE
    assert len(bot.sent) == 1 and bot.sent[0][2].startswith("✅ Отлично")
    assert db.get_task(conn, tid)["status"] == "answered"


async def test_on_more_answers_callback_before_long_work(conn, monkeypatch):
    call = _call("more:2026-10-05")

    async def send(*a, **k):
        call.answer.assert_awaited_once_with()   # «крутилка» снята ДО Gemini/TTS
        return "sent"
    monkeypatch.setattr(daily, "send_daily_task", send)
    _today(monkeypatch)
    await daily_handlers.on_more(call, conn, None, EN)
    call.answer.assert_awaited_once_with()


async def test_on_clarify_answers_callback_before_long_work(conn, monkeypatch):
    live = _task(conn, _card(conn))
    call = _call("clarify:answer:4")

    async def answer(*a, **k):
        call.answer.assert_awaited_once_with()
        return "done"
    monkeypatch.setattr(daily, "answer_task", answer)
    _today(monkeypatch)
    await daily_handlers.on_clarify(call, _state({"pending": {"4": {"text": "ответ", "task_id": live}}}),
                                    conn, None, EN)
    call.answer.assert_awaited_once_with()
    call.message.answer.assert_not_awaited()

    call2 = _call("clarify:capture:5")

    async def capture_items(*a, **k):
        call2.answer.assert_awaited_once_with()
        return [], None
    monkeypatch.setattr(daily, "capture_items", capture_items)
    await daily_handlers.on_clarify(call2, _state({"pending": {"5": {"text": "фраза", "task_id": live}}}),
                                    conn, None, EN)
    call2.answer.assert_awaited_once_with()


async def _real_state(data):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=U, user_id=U))
    await state.set_data(data)
    return state


@pytest.mark.parametrize("finisher", ["_show_next_flashcard", "_ask_next_translation", "_ask_next_listen"])
async def test_finished_training_session_preserves_seq(conn, finisher):
    """Конец тренировки не обнуляет seq: вчерашняя «✅ Беру» #2 не сохранит сегодняшнюю #2."""
    from handlers import training
    from states import Training
    state = await _real_state({"seq": 2, "pending": {"2": dict(ITEM)}, "queue": [], "retried": [7],
                               "vocab_voice_msg_id": 42})
    await state.set_state(Training.translate)
    message = _message("x")
    await getattr(training, finisher)(message, state, conn, EN)
    data = await state.get_data()
    assert await state.get_state() is None
    assert data == {"seq": 2}        # как прежний state.clear(), только seq выживает
    assert "vocab_voice_msg_id" not in data and "pending" not in data
    assert message.answer.await_args.args[0] == "Все слова повторены — ты молодец! ❤️"


async def test_end_session_without_seq_equals_plain_clear():
    from states import Training, end_session
    state = await _real_state({"queue": [1], "vocab_voice_msg_id": 42})
    await state.set_state(Training.flashcards)
    await end_session(state)
    assert await state.get_state() is None and await state.get_data() == {}
