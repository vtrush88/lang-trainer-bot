"""Task 3: общие хендлеры берут «сегодня» из clock.today() (DAILY_TZ), не серверный date.today().

ES-логика и тексты те же — меняется только источник даты (зона ES — тот же Europe/Madrid)."""
from __future__ import annotations

import inspect
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

import clock
from handlers import add, training
from handlers import daily as daily_handlers
from languages import PROFILES

BIZ = date(2026, 10, 9)


@pytest.fixture
def biz_today(monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: BIZ)


@pytest.mark.parametrize("module", [add, training, daily_handlers])
def test_handlers_do_not_use_server_date(module):
    assert "date.today()" not in inspect.getsource(module)


async def test_add_save_yes_uses_business_date(conn, biz_today):
    card = {"kind": "word", "word": "mesa", "translation": "стол", "transcription": "м",
            "example": "La mesa.", "example_translation": "Стол."}
    state = MagicMock()
    state.get_data = AsyncMock(return_value={"pending": {"1": card}})
    state.update_data = AsyncMock()
    call = MagicMock()
    call.data = "save:yes:1"
    call.from_user.id = 1
    call.message.edit_text = AsyncMock()
    call.answer = AsyncMock()
    await add.save_yes(call, state, conn, PROFILES["es"])
    row = conn.execute("SELECT created_at, due_at FROM cards WHERE word = 'mesa'").fetchone()
    assert row["created_at"] == "2026-10-09"


async def test_training_due_selection_and_srs_use_business_date(monkeypatch, biz_today):
    seen = {}
    monkeypatch.setattr(training.db, "get_due_cards",
                        lambda conn, uid, today: seen.setdefault("due", today) and [])
    message = MagicMock()
    message.from_user.id = 1
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    state.set_data = AsyncMock()
    monkeypatch.setattr(training, "leave_modes", AsyncMock())
    await training.start_flashcards(message, state, MagicMock(), PROFILES["es"])
    assert seen["due"] == BIZ

    card = {"id": 5, "word": "mesa", "translation": "стол", "interval_days": 1}
    monkeypatch.setattr(training.db, "get_card", MagicMock(return_value=card))
    update_review = MagicMock()
    monkeypatch.setattr(training.db, "update_review", update_review)
    monkeypatch.setattr(training, "_show_next_flashcard", AsyncMock())
    state.get_data = AsyncMock(return_value={"queue": [5], "retried": []})
    state.update_data = AsyncMock()
    call = MagicMock()
    call.data = "grade:remember"
    call.answer = AsyncMock()
    await training.grade_flashcard(call, state, MagicMock(), PROFILES["es"])
    due_at = update_review.call_args.kwargs["due_at"]
    assert due_at == training.srs.due_on(BIZ, update_review.call_args.kwargs["interval_days"])
