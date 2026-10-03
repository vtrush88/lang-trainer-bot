from __future__ import annotations

from datetime import date

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BTN_ADD = "➕ Добавить слово"
BTN_FLASHCARDS = "🎴 Карточки"
BTN_TRANSLATE = "✍️ Проверить себя"
BTN_LISTEN = "🎧 Аудирование"
BTN_VOCAB = "📖 Мой словарь"

# All reply-menu button texts. Answer handlers must exclude these so a menu
# tap mid-training switches modes instead of being graded as an answer.
MENU_BUTTONS = {BTN_ADD, BTN_FLASHCARDS, BTN_TRANSLATE, BTN_LISTEN, BTN_VOCAB}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_FLASHCARDS), KeyboardButton(text=BTN_TRANSLATE)],
            [KeyboardButton(text=BTN_LISTEN), KeyboardButton(text=BTN_VOCAB)],
        ],
        resize_keyboard=True,
    )


def menu_markup(profile) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Нижнее меню профиля: es — прежняя reply-клавиатура; command_menu (en) — убрать её,
    разделы живут в командах «/»."""
    if profile.command_menu:
        return ReplyKeyboardRemove()
    return main_menu()


def reveal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👁 Показать ответ", callback_data="show_answer")
    ]])


def grade_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Помню", callback_data="grade:remember"),
        InlineKeyboardButton(text="🔁 Не помню", callback_data="grade:forgot"),
    ]])


def save_card_keyboard(seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"save:yes:{seq}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"save:no:{seq}"),
    ]])


PAGE_SIZE = 5


def vocab_keyboard(cards: list, page: int, total: int) -> InlineKeyboardMarkup:
    """One row of number buttons (open a card) + nav arrows below."""
    rows = [[InlineKeyboardButton(
        text=str(i + 1 + page * PAGE_SIZE),
        callback_data=f"card:{c['id']}:{page}",
    ) for i, c in enumerate(cards)]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"vocab:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"vocab:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_detail_keyboard(card_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{card_id}:{page}"),
        InlineKeyboardButton(text="◀ К списку", callback_data=f"vocab:{page}"),
    ]])


def more_keyboard(today: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё одно", callback_data=f"more:{today.isoformat()}")
    ]])


def take_keyboard(seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Беру", callback_data=f"take:yes:{seq}"),
        InlineKeyboardButton(text="❌ Не надо", callback_data=f"take:no:{seq}"),
    ]])


def clarify_keyboard(seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответ", callback_data=f"clarify:answer:{seq}"),
        InlineKeyboardButton(text="➕ Новое слово", callback_data=f"clarify:capture:{seq}"),
    ]])
