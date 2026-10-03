from __future__ import annotations

import os
import sqlite3
import tempfile

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message

import db
from services import tts


async def send_text_voice(bot, chat_id: int, mp3_path: str, caption: str | None = None,
                          parse_mode: str | None = None, reply_markup=None) -> Message:
    """Отправить ГОТОВЫЙ mp3 голосовым (sendVoice принимает MP3 с Bot API 7.2).

    Без кэша и без синтеза: каждое предложение новое, синтез делает вызывающий,
    он же удаляет файл. Ошибки Telegram пробрасываются — решение о деградации
    принимает вызывающий.
    """
    with open(mp3_path, "rb") as fh:
        return await bot.send_voice(
            chat_id, BufferedInputFile(fh.read(), filename="произношение.mp3"),
            caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_card_voice_to(bot, chat_id: int, conn: sqlite3.Connection, card, voice: str,
                             caption: str | None = None, parse_mode: str | None = None,
                             reply_markup=None) -> Message | None:
    """Озвучка слова карточки: кэш по file_id, иначе синтез + кэширование.

    Голосовое, не audio: voice-пузыри не склеиваются клиентом в плейлист.
    file_id привязан к типу сообщения — при смене voice↔audio кэш сбрасывать.
    Best-effort: при сбое возвращает None (текст уже показан).
    """
    tmp = os.path.join(tempfile.gettempdir(), f"tts_{os.getpid()}_{card['id']}.mp3")
    try:
        if card["audio_file_id"]:
            return await bot.send_voice(chat_id, card["audio_file_id"], caption=caption,
                                        parse_mode=parse_mode, reply_markup=reply_markup)
        await tts.synthesize(card["word"], voice, tmp)
        sent = await send_text_voice(bot, chat_id, tmp, caption=caption, parse_mode=parse_mode,
                                     reply_markup=reply_markup)
        db.set_audio_file_id(conn, card["id"], sent.voice.file_id)
        return sent
    except (tts.TTSError, OSError, TelegramBadRequest):
        return None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def send_card_voice(message: Message, conn: sqlite3.Connection, card, voice: str,
                          caption: str | None = None, parse_mode: str | None = None,
                          reply_markup=None) -> Message | None:
    """Прежняя сигнатура (menu/training/add) + необязательные подпись/клавиатура."""
    return await send_card_voice_to(message.bot, message.chat.id, conn, card, voice,
                                    caption=caption, parse_mode=parse_mode,
                                    reply_markup=reply_markup)
