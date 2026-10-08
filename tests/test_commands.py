"""Команды «/» en-бота (handlers/commands.py) и нижнее меню по профилю.

Хендлеры вызываются напрямую с фейковыми message/state (как в test_daily_capture.py);
порядок роутеров проверяется прогоном апдейта через настоящий Dispatcher."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update

import bot as bot_module
import daily
import db
import keyboards
from handlers import commands
from handlers import menu as menu_handlers
from handlers import training as training_handlers
from languages import PROFILES
from services import enrichment
from states import AddCard, Training

EN, ES = PROFILES["en"], PROFILES["es"]
U = 111


class FakeState:
    """FSM-заглушка с настоящим хранением состояния и данных."""

    def __init__(self, state=None, data=None):
        self.state = state
        self.data = dict(data or {})

    async def get_state(self):
        return self.state

    async def set_state(self, state=None):
        self.state = state.state if hasattr(state, "state") else state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.state, self.data = None, {}


def _message(text="/x", user_id=U):
    message = MagicMock()
    message.text = text
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


def _card(conn, word="hello"):
    return db.add_card(conn, user_id=U, kind="word", word=word, translation="привет",
                       transcription="t", example="e", example_translation="э",
                       enriched=True, today=date(2026, 10, 1))


# ---- нижнее меню по профилю ----

async def test_cmd_start_en_removes_keyboard_es_keeps_reply_menu():
    for profile, kind in ((EN, ReplyKeyboardRemove), (ES, ReplyKeyboardMarkup)):
        message = _message("/start")
        await menu_handlers.cmd_start(message, FakeState(), profile)
        args, kwargs = message.answer.await_args
        assert args[0] == profile.greeting
        assert isinstance(kwargs["reply_markup"], kind)
    assert kwargs["reply_markup"] == keyboards.main_menu()   # es — ровно прежняя клавиатура


async def test_session_endings_use_profile_menu(conn):
    enders = (training_handlers._show_next_flashcard, training_handlers._ask_next_translation,
              training_handlers._ask_next_listen)
    for ender in enders:
        for profile, kind in ((EN, ReplyKeyboardRemove), (ES, ReplyKeyboardMarkup)):
            message = _message()
            await ender(message, FakeState(Training.flashcards.state, {"queue": []}), conn, profile)
            text = message.answer.await_args.args[0]
            assert text == "Все слова повторены — ты молодец! ❤️"
            assert isinstance(message.answer.await_args.kwargs["reply_markup"], kind)


def test_empty_vocab_text_by_profile(conn):
    es_text, kb = menu_handlers._render_page(conn, U, 0, ES)
    assert es_text == "Словарь пуст. Добавь первое слово через «➕ Добавить слово»." and kb is None
    en_text, _ = menu_handlers._render_page(conn, U, 0, EN)
    assert "/add" in en_text and "➕" not in en_text


# ---- делегирование команд ----

async def test_vocab_from_add_mode_leaves_mode_and_shows_vocab(conn):
    state = FakeState(AddCard.waiting_for_text.state)
    message = _message("/vocab")
    await commands.cmd_vocab(message, state, conn, EN)
    assert state.state is None
    assert "/add" in message.answer.await_args.args[0]   # пустой словарь en-текстом


async def test_add_starts_add_mode():
    state = FakeState(Training.listen.state)
    message = _message("/add")
    await commands.cmd_add(message, state, EN)
    assert state.state == AddCard.waiting_for_text.state
    assert message.answer.await_args.args[0] == EN.add_intro


async def test_cards_check_listen_start_their_trainings(conn, monkeypatch):
    monkeypatch.setattr(training_handlers.voice, "send_card_voice", AsyncMock())
    _card(conn)
    cases = ((commands.cmd_cards, Training.flashcards),
             (commands.cmd_check, Training.translate),
             (commands.cmd_listen, Training.listen))
    for handler, expected in cases:
        state = FakeState(AddCard.waiting_for_text.state)
        await handler(_message(), state, conn, EN)
        assert state.state == expected.state, handler.__name__


async def test_next_leaves_modes_then_sends_task(conn, monkeypatch):
    state = FakeState(AddCard.waiting_for_text.state, {"pending": {"1": {"word": "x"}}})
    seen = {}

    async def fake_send(bot, conn_, llm, profile, user_id, today, rng=None, *, morning=False,
                        want_new=False):
        seen.update(state=state.state, pending=state.data.get("pending"), user_id=user_id,
                    profile=profile, morning=morning, want_new=want_new)
        return "sent"
    monkeypatch.setattr(daily, "send_daily_task", fake_send)
    message = _message("/next")
    await commands.cmd_next(message, state, conn, "llm", EN)
    assert seen == {"state": None, "pending": {}, "user_id": U, "profile": EN, "morning": False,
                    "want_new": True}
    message.answer.assert_not_awaited()


def test_bot_commands_list():
    cmds = commands.bot_commands()
    assert [c.command for c in cmds] == ["next", "add", "vocab", "cards", "check", "listen"]
    assert all(c.description for c in cmds)


def test_next_command_description_is_new_word():
    assert {c.command: c.description for c in commands.bot_commands()}["next"] == "новое слово"


def test_command_handlers_have_no_state_filter():
    """Команды работают из любого режима: ни у одного хендлера роутера нет StateFilter."""
    from aiogram.filters import StateFilter
    for handler in commands.router.message.handlers:
        assert not any(isinstance(f.callback, StateFilter) for f in handler.filters)


# ---- порядок роутеров: настоящий Dispatcher ----

class RecordingBot(Bot):
    def __init__(self):
        super().__init__(token="123456:TEST-token")
        self.sent = []

    async def __call__(self, method, request_timeout=None):
        self.sent.append(method)
        return None


def _update(text, update_id=1):
    return Update.model_validate({
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": datetime.now(timezone.utc),
            "chat": {"id": U, "type": "private"},
            "from": {"id": U, "is_bot": False, "first_name": "T"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }, context={"bot": None})


async def test_vocab_typed_in_add_mode_reaches_vocab_not_enrichment(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(enrichment, "enrich", lambda *a, **k: calls.append(a))
    dp = None
    try:
        dp = bot_module.build_dispatcher(conn=conn, llm=None, profile=EN)
        fake_bot = RecordingBot()
        ctx = dp.fsm.get_context(bot=fake_bot, chat_id=U, user_id=U)
        await ctx.set_state(AddCard.waiting_for_text)
        await dp.feed_update(fake_bot, _update("/vocab"))
        assert calls == []                                     # не ушло в Gemini как слово
        assert await ctx.get_state() is None                   # режим добавления покинут
        assert len(fake_bot.sent) == 1 and "/add" in fake_bot.sent[0].text   # пустой словарь
    finally:
        # Настоящие роутеры нельзя подключить к двум Dispatcher'ам — отвязать для других тестов.
        # `_parent_router` — приватный атрибут aiogram 3.13.1; при апгрейде aiogram проверить.
        # Отвязываем и при сбое на середине подключения (dp ещё нет — по всем настоящим роутерам).
        routers = dp.sub_routers if dp is not None else [*bot_module.BASE_ROUTERS,
                                                          bot_module.COMMANDS_ROUTER,
                                                          bot_module.DAILY_ROUTER]
        for router in routers:
            router._parent_router = None
