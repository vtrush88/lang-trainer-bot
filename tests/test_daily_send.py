"""Выдача задания: fake bot, in-memory SQLite, замоканные Gemini и TTS."""
import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.exceptions import (TelegramForbiddenError, TelegramRetryAfter,
                                TelegramServerError)

import daily
import db
from languages import PROFILES
from services import sentences, tts

EN = PROFILES["en"]
U = 111
TODAY = date(2026, 10, 5)
GOOD = {"sentence": "Can you give me a heads-up before you merge?",
        "sentence_ru": "Предупредишь перед мержем?", "phrase_form": "a heads-up"}


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def _check(self, chat_id):
        if chat_id in self.fail_for:
            raise TelegramForbiddenError(method=None, message="bot was blocked")

    async def send_message(self, chat_id, text, **kw):
        await self._check(chat_id)
        self.sent.append(("message", chat_id, text, kw))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_voice(self, chat_id, voice, **kw):
        await self._check(chat_id)
        self.sent.append(("voice", chat_id, kw.get("caption"), kw))
        return SimpleNamespace(message_id=len(self.sent), voice=SimpleNamespace(file_id="FID"))


@pytest.fixture
def fake_tts(monkeypatch):
    async def ok(text, voice_name, out_path):
        with open(out_path, "wb") as fh:
            fh.write(b"ID3")
        return out_path
    monkeypatch.setattr(tts, "synthesize", ok)


@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setattr(sentences, "make_sentence", lambda llm, p, card, kind, avoid: dict(GOOD))
    return SimpleNamespace()


def _card(conn, word="a heads-up", *, created=date(2026, 10, 1), interval=0,
          due=None, example="Just a heads-up: tests are late.", user=U):
    cid = db.add_card(conn, user_id=user, kind="phrase", word=word, translation="предупредить заранее",
                      transcription="/x/", example=example, example_translation="Предупреждаю.",
                      enriched=True, today=created, context="созвон")
    if interval:
        db.update_review(conn, cid, interval_days=interval, due_at=due or TODAY, remembered=True)
    return cid


async def test_nothing_due_is_silent(conn, fake_llm, fake_tts):
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert out == "nothing" and bot.sent == []
    assert db.get_daily_state(conn, U)["last_sent_on"] is None


async def test_new_card_gets_compose_hinted_as_single_voice_with_caption(conn, fake_llm, fake_tts):
    cid = _card(conn)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert out == "sent"
    assert len(bot.sent) == 1
    kind, chat, caption, kw = bot.sent[0]
    assert kind == "voice" and chat == U and kw["parse_mode"] == "HTML"
    assert "Напиши своё предложение с <b>a heads-up</b>" in caption
    task = db.open_task(conn, U)
    assert task["kind"] == "compose_hinted" and task["card_id"] == cid and task["morning"] == 1
    assert task["sentence"] == GOOD["sentence"] and task["from_example"] == 0
    assert db.get_daily_state(conn, U)["last_sent_on"] == "2026-10-05"


async def test_gap_is_text_with_blank(conn, fake_llm, fake_tts):
    _card(conn, interval=1)
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    kind, _, text, _ = bot.sent[0]
    assert kind == "message" and "___" in text and GOOD["sentence_ru"] in text


async def test_morning_expires_open_task_and_bumps_missed(conn, fake_llm, fake_tts):
    cid = _card(conn)
    old = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="a heads-up", from_example=False,
                         today=date(2026, 10, 4), morning=True)
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert db.get_task(conn, old)["status"] == "expired"
    assert db.get_daily_state(conn, U)["missed_streak"] == 1
    assert db.open_task(conn, U)["id"] != old


async def test_morning_keeps_task_issued_today_by_next(conn, fake_llm, fake_tts):
    cid = _card(conn)
    tid = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="a heads-up", from_example=False, today=TODAY, morning=False)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert out == "nothing" and bot.sent == []
    assert db.get_task(conn, tid)["status"] == "open"
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_limit_wins_over_resend(conn, fake_llm, fake_tts):
    cid = _card(conn)
    for _ in range(2):
        t = db.create_task(conn, user_id=U, card_id=cid, kind="compose", sentence=None, sentence_ru=None,
                           phrase_form=None, from_example=False, today=TODAY, morning=False)
        db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    db.create_task(conn, user_id=U, card_id=cid, kind="compose", sentence=None, sentence_ru=None,
                   phrase_form=None, from_example=False, today=TODAY, morning=False)   # третья, открыта
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=False, limit=daily.MAX_TASKS_PER_DAY)
    assert out == "limit" and bot.sent == []


async def test_expired_non_morning_task_does_not_bump(conn, fake_llm, fake_tts):
    cid = _card(conn)
    db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                   phrase_form="a heads-up", from_example=False, today=date(2026, 10, 4), morning=False)
    await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_quiet_mode_after_three_misses(conn, fake_llm, fake_tts):
    _card(conn)
    for _ in range(3):
        db.bump_missed(conn, U)
    db.set_last_sent(conn, U, date(2026, 10, 4))
    bot = FakeBot()
    assert await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True) == "nothing"
    db.set_last_sent(conn, U, date(2026, 9, 28))
    assert await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True) == "sent"


async def test_sentence_error_falls_back_to_example(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise sentences.SentenceError("x")
    monkeypatch.setattr(sentences, "make_sentence", boom)
    _card(conn, interval=1)   # gap
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, None, EN, U, TODAY, random.Random(1), morning=True)
    task = db.open_task(conn, U)
    assert task["kind"] == "gap" and task["from_example"] == 1
    assert task["sentence"] == "Just a heads-up: tests are late."
    assert "___" in bot.sent[0][2]


async def test_no_example_downgrades_gap_to_recall(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise sentences.SentenceError("x")
    monkeypatch.setattr(sentences, "make_sentence", boom)
    _card(conn, interval=1, example="No phrase inside.")
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, None, EN, U, TODAY, random.Random(1), morning=True)
    task = db.open_task(conn, U)
    assert task["kind"] == "recall" and task["sentence"] is None
    assert "Как сказать по-английски" in bot.sent[0][2]


async def test_tts_error_downgrades_listen_to_gap(conn, fake_llm, monkeypatch):
    async def boom(text, voice_name, out_path):
        raise tts.TTSError("403")
    monkeypatch.setattr(tts, "synthesize", boom)
    _card(conn, interval=7)   # listen
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert bot.sent[0][0] == "message" and "___" in bot.sent[0][2]
    assert db.open_task(conn, U)["kind"] == "gap"


async def test_compose_hinted_no_sentence_with_word_voice_has_no_note(conn, fake_tts, monkeypatch):
    def boom_llm(*a, **k):
        raise sentences.SentenceError("x")
    monkeypatch.setattr(sentences, "make_sentence", boom_llm)
    _card(conn, example="No phrase inside.")
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, None, EN, U, TODAY, random.Random(1), morning=True)
    assert bot.sent[0][0] == "voice" and daily.VOICE_UNAVAILABLE not in bot.sent[0][2]


async def test_compose_hinted_no_sentence_and_no_word_voice_shows_note(conn, monkeypatch):
    def boom_llm(*a, **k):
        raise sentences.SentenceError("x")
    monkeypatch.setattr(sentences, "make_sentence", boom_llm)

    async def boom_tts(text, voice_name, out_path):
        raise tts.TTSError("403")
    monkeypatch.setattr(tts, "synthesize", boom_tts)
    _card(conn, example="No phrase inside.")   # fallback-примера тоже нет
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, None, EN, U, TODAY, random.Random(1), morning=True)
    assert bot.sent[0][0] == "message" and daily.VOICE_UNAVAILABLE in bot.sent[0][2]


async def test_compose_hinted_without_voice_goes_text_with_note(conn, fake_llm, monkeypatch):
    async def boom(text, voice_name, out_path):
        raise tts.TTSError("403")
    monkeypatch.setattr(tts, "synthesize", boom)
    _card(conn)
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert bot.sent[0][0] == "message" and daily.VOICE_UNAVAILABLE in bot.sent[0][2]
    assert db.open_task(conn, U)["kind"] == "compose_hinted"


async def test_send_failure_creates_no_task_and_no_bump(conn, fake_llm, fake_tts):
    _card(conn)
    bot = FakeBot(fail_for={U})
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=True)
    assert out == "failed"
    assert db.open_task(conn, U) is None
    assert db.get_daily_state(conn, U)["missed_streak"] == 0
    assert db.get_daily_state(conn, U)["last_sent_on"] is None


async def test_run_morning_isolates_users(conn, fake_llm, fake_tts):
    _card(conn, user=U)
    _card(conn, user=222)
    bot = FakeBot(fail_for={U})
    await daily.run_morning(bot, conn, fake_llm, EN, [U, 222], TODAY, random.Random(1))
    assert db.open_task(conn, U) is None
    assert db.open_task(conn, 222) is not None
    assert [s[1] for s in bot.sent] == [222]


async def test_next_resends_open_task_without_new_generation(conn, fake_llm, fake_tts, monkeypatch):
    _card(conn, interval=1)
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=False)
    calls = []
    monkeypatch.setattr(sentences, "make_sentence", lambda *a, **k: calls.append(1) or dict(GOOD))
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=False)
    assert out == "resent" and calls == [] and len(bot.sent) == 2
    assert db.count_tasks_on(conn, U, TODAY) == 1
    assert db.get_daily_state(conn, U)["last_sent_on"] is None   # цепочка/next не трогают


async def test_concurrent_next_yields_one_sent_one_resent(conn, fake_tts, monkeypatch):
    import time

    def slow(*a, **k):          # идёт через asyncio.to_thread → первый вызов отдаёт loop второму
        time.sleep(0.02)
        return dict(GOOD)
    monkeypatch.setattr(sentences, "make_sentence", slow)
    _card(conn, interval=1)
    bot = FakeBot()
    rng = random.Random(1)
    results = await asyncio.gather(
        daily.send_daily_task(bot, conn, None, EN, U, TODAY, rng, morning=False),
        daily.send_daily_task(bot, conn, None, EN, U, TODAY, rng, morning=False))
    assert sorted(results) == ["resent", "sent"]
    assert db.count_tasks_on(conn, U, TODAY) == 1


async def test_resend_listen_with_tts_failure_falls_to_text(conn, fake_llm, fake_tts, monkeypatch):
    _card(conn, interval=7)
    bot = FakeBot()
    await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=False)
    assert db.open_task(conn, U)["kind"] == "listen"

    async def boom(text, voice_name, out_path):
        raise tts.TTSError("403")
    monkeypatch.setattr(tts, "synthesize", boom)
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=False)
    assert out == "resent" and bot.sent[-1][0] == "message" and "___" in bot.sent[-1][2]
    assert db.open_task(conn, U)["kind"] == "gap"   # вид в БД = то, что увидел пользователь


async def test_limit_is_checked_inside_lock(conn, fake_llm, fake_tts):
    cid = _card(conn)
    _card(conn, word="second", example="Second one here.")
    for _ in range(3):
        t = db.create_task(conn, user_id=U, card_id=cid, kind="compose", sentence=None, sentence_ru=None,
                           phrase_form=None, from_example=False, today=TODAY, morning=False)
        db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=False, limit=daily.MAX_TASKS_PER_DAY)
    assert out == "limit" and bot.sent == []
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1), morning=False)
    assert out == "sent"   # /next без лимита


async def test_synthesize_tmp_names_are_unique(fake_tts):
    import os
    a = await daily._synthesize_tmp("same text", "v")
    b = await daily._synthesize_tmp("same text", "v")
    try:
        assert a != b and os.path.exists(a) and os.path.exists(b)
    finally:
        os.remove(a); os.remove(b)


async def test_daily_loop_uses_configured_tz_date_and_absolute_delay(monkeypatch):
    from datetime import time
    delays, runs = [], []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) > 1:
            raise asyncio.CancelledError

    async def fake_run(bot, conn, llm, profile, ids, today, rng):
        runs.append((set(ids), today))

    monkeypatch.setattr(daily.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(daily, "run_morning", fake_run)
    cfg = SimpleNamespace(daily_tz="Pacific/Kiritimati", daily_at=time(9, 0),
                          allowed_user_ids={1, 2, 3}, daily_exclude_ids={2})
    with pytest.raises(asyncio.CancelledError):
        await daily.daily_loop(None, None, None, EN, cfg)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert runs[0][0] == {1, 3}
    assert runs[0][1] == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert 0 <= delays[0] <= 86400


class _RaisingBot(FakeBot):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    async def _check(self, chat_id):
        raise self.exc


@pytest.mark.parametrize("exc", [
    TelegramRetryAfter(method=None, message="Flood control exceeded", retry_after=5),
    TelegramServerError(method=None, message="Internal Server Error"),
])
async def test_send_rate_limit_or_server_error_is_failed_without_task(conn, fake_llm, fake_tts, exc):
    _card(conn)
    out = await daily.send_daily_task(_RaisingBot(exc), conn, fake_llm, EN, U, TODAY,
                                      random.Random(1), morning=True)
    assert out == "failed"
    assert db.open_task(conn, U) is None


async def test_morning_expires_zombie_grading_from_previous_day(conn, fake_llm, fake_tts):
    cid = _card(conn)
    old = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="a heads-up", from_example=False,
                         today=date(2026, 10, 4), morning=True)
    db.claim_task(conn, old)            # падение между claim и finish, без рестарта
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=True)
    assert out == "sent"
    assert db.get_task(conn, old)["status"] == "expired"
    assert db.open_task(conn, U)["id"] != old
    assert db.release_stale_grading(conn) == 0     # следующему старту нечего «воскрешать»
