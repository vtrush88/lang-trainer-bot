from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

import db
import voice
from services import tts


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_voice(self, chat_id, voice_file, **kw):
        self.calls.append((chat_id, voice_file, kw))
        return SimpleNamespace(message_id=len(self.calls),
                               voice=SimpleNamespace(file_id="FID-NEW"))


def _card(conn, file_id=None):
    cid = db.add_card(conn, user_id=1, kind="word", word="heads-up", translation="п",
                      transcription="x", example="e", example_translation="э",
                      enriched=True, today=date(2026, 10, 1))
    if file_id:
        db.set_audio_file_id(conn, cid, file_id)
    return db.get_card(conn, cid)


async def test_send_text_voice_passes_caption_and_parse_mode(tmp_path):
    mp3 = tmp_path / "s.mp3"
    mp3.write_bytes(b"ID3")
    bot = FakeBot()
    msg = await voice.send_text_voice(bot, 42, str(mp3), caption="<b>x</b>", parse_mode="HTML",
                                      reply_markup="KB")
    assert msg.message_id == 1
    chat_id, _file, kw = bot.calls[0]
    assert chat_id == 42 and kw["caption"] == "<b>x</b>" and kw["parse_mode"] == "HTML"
    assert kw["reply_markup"] == "KB"
    assert mp3.exists()   # файл не удалён — им владеет вызывающий


async def test_send_text_voice_propagates_telegram_errors(tmp_path):
    mp3 = tmp_path / "s.mp3"; mp3.write_bytes(b"ID3")
    bot = FakeBot()
    bot.send_voice = AsyncMock(side_effect=TelegramBadRequest(method=None, message="bad"))
    with pytest.raises(TelegramBadRequest):
        await voice.send_text_voice(bot, 42, str(mp3))


async def test_send_card_voice_to_uses_cached_file_id_with_caption(conn):
    card = _card(conn, file_id="FID-CACHED")
    bot = FakeBot()
    await voice.send_card_voice_to(bot, 7, conn, card, "en-US-EmmaNeural",
                                   caption="cap", parse_mode="HTML")
    chat_id, file, kw = bot.calls[0]
    assert file == "FID-CACHED" and kw["caption"] == "cap" and kw["parse_mode"] == "HTML"


async def test_send_card_voice_to_synthesizes_and_caches(conn, monkeypatch):
    card = _card(conn)

    async def fake_synth(text, voice_name, out_path):
        with open(out_path, "wb") as fh:
            fh.write(b"ID3")
        return out_path
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    bot = FakeBot()
    sent = await voice.send_card_voice_to(bot, 7, conn, card, "en-US-EmmaNeural")
    assert sent is not None
    assert db.get_card(conn, card["id"])["audio_file_id"] == "FID-NEW"


async def test_send_card_voice_to_cached_bad_request_returns_none(conn):
    card = _card(conn, file_id="FID-STALE")
    bot = FakeBot()
    bot.send_voice = AsyncMock(side_effect=TelegramBadRequest(method=None, message="wrong file id"))
    assert await voice.send_card_voice_to(bot, 7, conn, card, "v") is None


async def test_send_card_voice_to_returns_none_on_tts_error(conn, monkeypatch):
    card = _card(conn)

    async def boom(text, voice_name, out_path):
        raise tts.TTSError("403")
    monkeypatch.setattr(tts, "synthesize", boom)
    assert await voice.send_card_voice_to(FakeBot(), 7, conn, card, "v") is None


async def test_send_card_voice_delegates_with_message(conn):
    card = _card(conn, file_id="FID-CACHED")
    bot = FakeBot()
    message = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=9))
    await voice.send_card_voice(message, conn, card, "v")
    assert bot.calls[0][0] == 9 and bot.calls[0][2]["caption"] is None
