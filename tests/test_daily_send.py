"""Выдача задания: fake bot, in-memory SQLite, замоканные Gemini и TTS."""
import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.exceptions import (TelegramForbiddenError, TelegramRetryAfter,
                                TelegramServerError)

import clock
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


def _tasks_on(conn, day):
    """Все задачи пользователя за день (в проде счётчика нет — только повторы/новые)."""
    return conn.execute("SELECT COUNT(*) AS n FROM daily_tasks WHERE user_id = ? AND sent_on = ?",
                        (U, day.isoformat())).fetchone()["n"]


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
    assert _tasks_on(conn, TODAY) == 1
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
    assert _tasks_on(conn, TODAY) == 1


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
    clock.configure(cfg.daily_tz)   # единственный владелец зоны — bot.main (Task 3)
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


# ---- дельта (р): /next через 15 минут выдаёт новое ----

from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def _open_next_task(conn, cid, *, minutes_ago, morning=False):
    return db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence=GOOD["sentence"],
                          sentence_ru="r", phrase_form="a heads-up", from_example=False, today=TODAY,
                          morning=morning, issued_at=T0 - timedelta(minutes=minutes_ago),
                          requested=True)


async def test_next_with_fresh_task_resends(conn, fake_llm, fake_tts, monkeypatch):
    monkeypatch.setattr(daily, "_utcnow", lambda: T0)
    cid = _card(conn)
    tid = _open_next_task(conn, cid, minutes_ago=14)
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=False)
    assert out == "resent" and db.open_task(conn, U)["id"] == tid


async def test_next_with_old_morning_task_resends(conn, fake_llm, fake_tts, monkeypatch):
    monkeypatch.setattr(daily, "_utcnow", lambda: T0)
    cid = _card(conn)
    tid = _open_next_task(conn, cid, minutes_ago=300, morning=True)
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=False)
    assert out == "resent" and db.get_task(conn, tid)["status"] == "open"



async def test_daily_loop_does_not_configure_clock_itself(monkeypatch):
    """Зона — у bot.main (единственный владелец); цикл только читает clock."""
    from datetime import time
    calls = []
    monkeypatch.setattr(clock, "configure", lambda tz: calls.append(tz))

    async def stop(d):
        raise asyncio.CancelledError
    monkeypatch.setattr(daily.asyncio, "sleep", stop)
    cfg = SimpleNamespace(daily_tz="Pacific/Kiritimati", daily_at=time(9, 0),
                          allowed_user_ids={1}, daily_exclude_ids=set())
    with pytest.raises(asyncio.CancelledError):
        await daily.daily_loop(None, None, None, EN, cfg)
    assert calls == []


async def test_next_with_stale_task_resends_and_touches_issued_at(conn, fake_llm, fake_tts, monkeypatch):
    """/next при открытой задаче только повторяет её: не истекает, issued_at = сейчас
    (иначе ответ сразу после /next уйдёт в 15-минутный «переспрос»)."""
    monkeypatch.setattr(daily, "_utcnow", lambda: T0)
    cid = _card(conn)
    old = _open_next_task(conn, cid, minutes_ago=60)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "resent" and len(bot.sent) == 1
    task = db.get_task(conn, old)
    assert task["status"] == "open" and task["issued_at"] == T0.isoformat()
    assert daily.is_stale(task, T0) is False          # stale-текст после повтора не переспрашивает
    assert _tasks_on(conn, TODAY) == 1


# ---- Task 3: расписание — повторы первыми, новые по недельной норме ----

def _new_task(conn, cid, day, *, status="answered"):
    """Задача-«новое слово» (is_new=1), выданная в day; по умолчанию уже закрыта."""
    tid = db.create_task(conn, user_id=U, card_id=cid, kind="compose_hinted", sentence=None,
                         sentence_ru=None, phrase_form=None, from_example=False, today=day,
                         morning=True, is_new=True)
    if status == "answered":
        db.claim_task(conn, tid); db.finish_task(conn, tid, ok=True)
    elif status == "expired":
        db.expire_task(conn, tid)
    return tid


def _close_open(conn, *, next_due=date(2026, 11, 1)):
    """Ответить на открытую задачу «вручную»: закрыть и увести карточку в будущее."""
    t = db.open_task(conn, U)
    db.claim_task(conn, t["id"]); db.finish_task(conn, t["id"], ok=True)
    db.update_review(conn, t["card_id"], interval_days=30, due_at=next_due, remembered=True)
    return t


async def test_morning_repeat_comes_before_new(conn, fake_llm, fake_tts):
    _card(conn, "fresh new")                                         # новая, свежая
    rep = _card(conn, "old repeat", interval=3, due=date(2026, 10, 3))
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=True)
    task = db.open_task(conn, U)
    assert out == "sent" and task["card_id"] == rep and task["is_new"] == 0
    assert task["requested"] == 0


async def test_morning_new_word_when_no_repeats_and_quota_allows(conn, fake_llm, fake_tts):
    cid = _card(conn)
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=True)
    task = db.open_task(conn, U)
    assert out == "sent" and task["card_id"] == cid and task["is_new"] == 1


async def test_morning_quota_exhausted_is_nothing(conn, fake_llm, fake_tts):
    a, b = _card(conn, "a"), _card(conn, "b")
    _card(conn, "c")
    monday = date(2026, 10, 5)
    _new_task(conn, a, monday)
    _new_task(conn, b, date(2026, 10, 8))
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, date(2026, 10, 11),
                                      random.Random(1), morning=True)
    assert out == "nothing" and bot.sent == []


async def test_new_word_gap_rule_across_iso_week_boundary(conn, fake_llm, fake_tts):
    """Прошлая неделя в норму не идёт, но интервал 3 дня — сквозной."""
    a, b = _card(conn, "a"), _card(conn, "b")
    c = _card(conn, "c")
    _new_task(conn, a, date(2026, 10, 1))   # чт прошлой недели
    _new_task(conn, b, date(2026, 10, 4))   # вс прошлой недели
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, date(2026, 10, 5),
                                      random.Random(1), morning=True)
    assert out == "nothing"                 # пн: с воскресенья прошёл 1 день
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, date(2026, 10, 7),
                                      random.Random(1), morning=True)
    assert out == "sent" and db.open_task(conn, U)["card_id"] == c


async def test_reissued_unanswered_new_card_does_not_eat_second_slot(conn, fake_llm, fake_tts):
    a = _card(conn, "a")
    b = _card(conn, "b", created=date(2026, 9, 1))
    _new_task(conn, a, date(2026, 10, 5), status="expired")   # пн: выдана, не отвечена
    _new_task(conn, a, date(2026, 10, 8))                     # чт: та же карточка повторно
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, date(2026, 10, 11),
                                      random.Random(1), morning=True)
    task = db.open_task(conn, U)
    assert out == "sent" and task["card_id"] == b and task["is_new"] == 1


async def test_six_repeats_due_five_today_sixth_tomorrow_no_new_on_debt_day(conn, fake_llm, fake_tts):
    reps = [_card(conn, f"rep {i}", interval=3, due=date(2026, 9, 20 + i)) for i in range(6)]
    _card(conn, "new word")                  # новое доступно, но в день долга не приходит
    bot = FakeBot()
    issued = []
    for _ in range(5):
        out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1))
        assert out == "sent"
        issued.append(_close_open(conn)["card_id"])
    assert issued == reps[:5]                # самые просроченные первыми
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1))
    assert out == "nothing" and len(bot.sent) == 5
    assert db.count_new_on(conn, U, TODAY) == 0
    tomorrow = date(2026, 10, 6)
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, tomorrow, random.Random(1),
                                      morning=True)
    task = db.open_task(conn, U)
    assert out == "sent" and task["card_id"] == reps[5] and task["is_new"] == 0


async def test_repeat_cap_counts_only_todays_repeats(conn, fake_llm, fake_tts):
    """MAX_REPEATS_PER_DAY: вчерашние повторы и сегодняшние новые не считаются."""
    old = _card(conn, "old", interval=3, due=date(2026, 9, 1))
    for _ in range(daily.MAX_REPEATS_PER_DAY):
        t = db.create_task(conn, user_id=U, card_id=old, kind="recall", sentence=None,
                           sentence_ru=None, phrase_form=None, from_example=False,
                           today=date(2026, 10, 4), morning=False)
        db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    nw = _card(conn, "nw")
    _new_task(conn, nw, TODAY)
    rep = _card(conn, "due today", interval=1, due=TODAY)
    db.update_review(conn, old, interval_days=30, due_at=date(2026, 11, 1), remembered=True)
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1))
    assert out == "sent" and db.open_task(conn, U)["card_id"] == rep


async def test_morning_yesterday_open_task_expires_bumps_and_issues_repeat(conn, fake_llm, fake_tts):
    cid = _card(conn, "yesterday", interval=1, due=date(2026, 10, 4))
    old = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="yesterday", from_example=False,
                         today=date(2026, 10, 4), morning=True)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      morning=True)
    assert out == "sent" and len(bot.sent) == 1
    assert db.get_task(conn, old)["status"] == "expired"
    assert db.get_daily_state(conn, U)["missed_streak"] == 1
    task = db.open_task(conn, U)
    assert task["id"] != old and task["card_id"] == cid and task["is_new"] == 0
    assert db.get_daily_state(conn, U)["last_sent_on"] == TODAY.isoformat()


async def test_next_gives_new_word_even_with_repeats_due(conn, fake_llm, fake_tts):
    _card(conn, "rep", interval=3, due=date(2026, 10, 1))
    nw = _card(conn, "nw")
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    task = db.open_task(conn, U)
    assert out == "sent" and task["card_id"] == nw and task["is_new"] == 1 and task["morning"] == 0
    assert task["requested"] == 1                                # только /next ставит requested
    assert db.get_daily_state(conn, U)["last_sent_on"] is None   # set_last_sent — только утром


async def test_next_quota_exhausted_is_later(conn, fake_llm, fake_tts):
    a = _card(conn, "a")
    _card(conn, "b")
    _new_task(conn, a, date(2026, 10, 4))
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "later" and bot.sent == []


async def test_next_one_new_per_day(conn, fake_llm, fake_tts):
    a = _card(conn, "a")
    _card(conn, "b")
    _new_task(conn, a, TODAY)
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "later"


async def test_next_availability_before_quota(conn, fake_llm, fake_tts):
    """Round 1: сначала наличие карточек, потом норма. Норма исчерпана, но новых нет →
    "nothing"; есть только сохранённые сегодня → "tomorrow"; есть доступная → "later"."""
    used = _card(conn, "used")
    _new_task(conn, used, TODAY)                 # норма дня/интервала исчерпана
    db.update_review(conn, used, interval_days=1, due_at=date(2026, 11, 1), remembered=True)
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "nothing"
    _card(conn, "saved today", created=TODAY)
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "tomorrow"
    _card(conn, "available")
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "later" and bot.sent == []


async def test_next_saved_today_is_tomorrow_and_empty_is_nothing(conn, fake_llm, fake_tts):
    bot = FakeBot()
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "nothing"
    _card(conn, "saved today", created=TODAY)
    out = await daily.send_daily_task(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "tomorrow" and bot.sent == []


async def test_next_with_open_task_of_deleted_card_expires_and_continues(conn, fake_llm, fake_tts):
    gone = _card(conn, "gone", interval=1, due=TODAY)
    tid = db.create_task(conn, user_id=U, card_id=gone, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="gone", from_example=False, today=TODAY, morning=True)
    db.delete_card(conn, gone, user_id=U)
    nw = _card(conn, "nw")
    out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                      want_new=True)
    assert out == "sent"
    assert db.get_task(conn, tid)["status"] == "expired"
    assert db.open_task(conn, U)["card_id"] == nw


async def test_issue_locked_chain_with_open_task_is_nothing(conn, fake_llm, fake_tts):
    cid = _card(conn, "rep", interval=1, due=TODAY)
    tid = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                         phrase_form="rep", from_example=False, today=TODAY, morning=False)
    bot = FakeBot()
    out = await daily._issue_locked(bot, conn, fake_llm, EN, U, TODAY, random.Random(1),
                                    morning=False, want_new=False, chain=True)
    assert out == "nothing" and bot.sent == [] and db.get_task(conn, tid)["status"] == "open"


async def test_send_daily_task_rng_defaults_to_module_rng(conn, fake_llm, fake_tts, monkeypatch):
    seen = []
    monkeypatch.setattr(daily, "task_kind", lambda interval, rng: seen.append(rng) or "compose")
    monkeypatch.setattr(daily, "rng", random.Random(7))
    _card(conn, "rep", interval=40, due=TODAY)
    await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY)
    assert seen == [daily.rng]


# ---- финальная волна: причина каждого "nothing" — в логе ----

def _nothing_reasons(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelname == "INFO" and "nothing" in r.getMessage() and str(U) in r.getMessage()]


async def test_nothing_reason_logged_open_today_and_chain_guard(conn, fake_llm, fake_tts, caplog):
    cid = _card(conn)
    db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                   phrase_form="a heads-up", from_example=False, today=TODAY, morning=False)
    with caplog.at_level("INFO", logger="daily"):
        await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, morning=True)
        await daily._issue_locked(FakeBot(), conn, fake_llm, EN, U, TODAY, random.Random(1),
                                  morning=False, want_new=False, chain=True)
    reasons = _nothing_reasons(caplog)
    assert any("open task from today" in m for m in reasons)
    assert any("chain guard" in m for m in reasons)


async def test_nothing_reason_logged_quiet_after_expiry(conn, fake_llm, fake_tts, caplog):
    cid = _card(conn)
    db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence="S", sentence_ru="r",
                   phrase_form="a heads-up", from_example=False, today=date(2026, 10, 4),
                   morning=True)
    for _ in range(2):
        db.bump_missed(conn, U)
    db.set_last_sent(conn, U, date(2026, 10, 4))
    with caplog.at_level("INFO", logger="daily"):
        out = await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY, morning=True)
    assert out == "nothing"
    assert any("quiet mode" in m and "expired" in m for m in _nothing_reasons(caplog))


async def test_nothing_reason_logged_cap_quota_and_empty(conn, fake_llm, fake_tts, caplog):
    with caplog.at_level("INFO", logger="daily"):
        await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY)       # пусто
        a = _card(conn, "a")
        _new_task(conn, a, TODAY)
        _card(conn, "b")
        await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY)       # норма
        rep = _card(conn, "rep", interval=1, due=TODAY)
        for _ in range(daily.MAX_REPEATS_PER_DAY):
            t = db.create_task(conn, user_id=U, card_id=rep, kind="recall", sentence=None,
                               sentence_ru=None, phrase_form=None, from_example=False,
                               today=TODAY, morning=False)
            db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
        await daily.send_daily_task(FakeBot(), conn, fake_llm, EN, U, TODAY)       # потолок
    reasons = " | ".join(_nothing_reasons(caplog))
    assert "no due repeat and no new card" in reasons
    assert "new-word quota or 3-day gap" in reasons
    assert "repeat cap reached" in reasons
