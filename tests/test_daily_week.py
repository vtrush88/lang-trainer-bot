"""Сквозной прогон расписания на ~10 бизнес-дней: настоящие send_daily_task/answer_task, fake bot,
in-memory SQLite, фейки Gemini/TTS, дата — через monkeypatch clock.today. Без стенных часов.

Данные — как у Victoria в вечер выката (чт 2026-10-08): 13 ни разу не показанных слов,
10 просроченных повторов, 6 legacy-строк compose_hinted (сб..чт, без is_new — backfill на старте),
задача четверга открыта. На этой неделе уже 4 новых → норма исчерпана до понедельника.
"""
import random
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import clock
import daily
import db
from languages import PROFILES
from services import grading, sentences, tts

EN = PROFILES["en"]
U = 1
DEPLOY = date(2026, 10, 8)            # чт
FRI = date(2026, 10, 9)
NEXT_MON = date(2026, 10, 12)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_voice(self, chat_id, voice, **kw):
        self.sent.append(kw.get("caption"))
        return SimpleNamespace(message_id=len(self.sent), voice=SimpleNamespace(file_id="F"))


@pytest.fixture
def world(conn, monkeypatch):
    async def fake_tts(text, voice_name, out_path):
        with open(out_path, "wb") as fh:
            fh.write(b"ID3")
        return out_path
    monkeypatch.setattr(tts, "synthesize", fake_tts)
    monkeypatch.setattr(sentences, "make_sentence", lambda llm, p, card, kind, avoid: {
        "sentence": f"I use {card['word']} daily.", "sentence_ru": "р", "phrase_form": card["word"]})
    monkeypatch.setattr(sentences, "check_sentence",
                        lambda *a, **k: {"verdict": "good", "note": "", "reply_sentence": None})
    monkeypatch.setattr(grading, "grade",
                        lambda *a, **k: {"verdict": "wrong", "correct": "x", "note": "n"})

    def card(word, created, interval=0, due=None):
        cid = db.add_card(conn, user_id=U, kind="word", word=word, translation="перевод",
                          transcription="/x/", example=f"Example with {word}.",
                          example_translation="Пример.", enriched=True, today=created)
        if interval:
            db.update_review(conn, cid, interval_days=interval, due_at=due, remembered=True)
        return cid

    for i in range(13):
        card(f"new{i:02d}", date(2026, 9, 20) + timedelta(days=i % 10))
    for i in range(10):
        card(f"rep{i:02d}", date(2026, 9, 10), interval=[1, 3, 7][i % 3],
             due=date(2026, 9, 29) + timedelta(days=i % 8))
    for i in range(6):
        day = date(2026, 10, 3) + timedelta(days=i)
        cid = card(f"shown{i}", date(2026, 9, 28))
        conn.execute("INSERT INTO daily_tasks (user_id, card_id, kind, sent_on, morning, status,"
                     " issued_at) VALUES (?,?,?,?,1,?,?)",
                     (U, cid, "compose_hinted", day.isoformat(),
                      "open" if day == DEPLOY else "answered", f"{day.isoformat()}T07:30:00+00:00"))
        if day != DEPLOY:
            db.update_review(conn, cid, interval_days=1, due_at=day + timedelta(days=1),
                             remembered=True)
    conn.commit()
    db.init_db(conn)   # рестарт после выката: backfill is_new из compose_hinted
    return SimpleNamespace(conn=conn, bot=FakeBot(), rng=random.Random(1), mp=monkeypatch,
                           days={})


def _due_snapshot(conn, today):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM cards WHERE user_id = ? AND interval_days > 0 AND due_at <= ?"
        " ORDER BY due_at, id", (U, today.isoformat()))]


def _answer_for(conn, task):
    c = db.get_card(conn, task["card_id"])
    if task["kind"] == "recall":
        return c["word"]
    if task["kind"] == "gap":
        return task["phrase_form"] or c["word"]
    if task["kind"] == "listen":
        return task["sentence"] or c["word"]
    return f"My own sentence about {c['word']} and my life."


async def _run_day(w, today, *, wrong=(), stop_after=None):
    """Утро + ответы по цепочке, пока задания идут. wrong — card_id, на которые «не помню»."""
    conn = w.conn
    w.mp.setattr(clock, "today", lambda: today)
    snapshot = _due_snapshot(conn, today)
    morning = await daily.send_daily_task(w.bot, conn, None, EN, U, today, w.rng, morning=True)
    missed_after_morning = db.get_daily_state(conn, U)["missed_streak"]
    answered = 0
    while (task := db.open_task(conn, U)) is not None:
        if stop_after is not None and answered >= stop_after:
            break
        giveup = task["card_id"] in wrong
        res = await daily.answer_task(w.bot, conn, None, EN, U,
                                      "не помню" if giveup else _answer_for(conn, task),
                                      today, giveup=giveup, task_id=task["id"])
        assert res == "done"
        answered += 1
    tasks = conn.execute("SELECT * FROM daily_tasks WHERE user_id = ? AND sent_on = ? ORDER BY id",
                         (U, today.isoformat())).fetchall()
    day = SimpleNamespace(morning=morning, snapshot=snapshot, tasks=tasks,
                          repeats=[t["card_id"] for t in tasks if not t["is_new"]],
                          new=[t["card_id"] for t in tasks if t["is_new"]],
                          missed_after_morning=missed_after_morning)
    w.days[today] = day
    return day


async def test_ten_days_from_deploy_shape(world):
    w = world
    assert db.count_new_since(w.conn, U, daily.week_start(DEPLOY), DEPLOY) == 4   # backfill
    days = [FRI + timedelta(days=k) for k in range(10)]                           # пт..вс+1
    for d in days:
        await _run_day(w, d)

    fri = w.days[FRI]
    legacy_open = w.conn.execute("SELECT status FROM daily_tasks WHERE sent_on = ?",
                                 (DEPLOY.isoformat(),)).fetchone()
    assert legacy_open["status"] == "expired" and fri.missed_after_morning == 1   # утренняя → bump

    new_days = []
    for d in days:
        day = w.days[d]
        assert len(day.repeats) <= daily.MAX_REPEATS_PER_DAY
        # повторы — самые просроченные первыми (верный ответ уводит карточку в будущее)
        assert day.repeats == day.snapshot[:daily.MAX_REPEATS_PER_DAY]
        if len(day.snapshot) > daily.MAX_REPEATS_PER_DAY:
            assert day.new == [], f"{d}: новое слово в день долга"
        assert len(day.new) <= 1
        if day.new:
            new_days.append(d)
            assert day.tasks[-1]["is_new"] == 1          # новое — только после последнего повтора
    assert all(not w.days[d].new for d in days if d < NEXT_MON)   # норма прошлой недели
    assert new_days and new_days[0] == NEXT_MON                    # первое новое — в понедельник
    assert len(new_days) >= 2 and (new_days[1] - new_days[0]).days >= daily.NEW_MIN_GAP_DAYS
    per_week = {}
    for d in new_days:
        per_week.setdefault(daily.week_start(d), []).append(d)
    assert all(len(v) <= daily.NEW_PER_WEEK for v in per_week.values())
    for a, b in zip(new_days, new_days[1:]):
        assert (b - a).days >= daily.NEW_MIN_GAP_DAYS
    # долг 15 повторов разобран за три дня (пт/сб/вс по 5), пропусков после пятницы нет
    assert [len(w.days[FRI + timedelta(days=k)].repeats) for k in range(3)] == [5, 5, 5]
    assert len(w.days[NEXT_MON].snapshot) <= daily.MAX_REPEATS_PER_DAY
    assert db.get_daily_state(w.conn, U)["missed_streak"] == 0


async def test_wrong_answer_comes_back_next_day(world):
    w = world
    for d in (FRI, FRI + timedelta(days=1), FRI + timedelta(days=2)):   # разбор долга
        await _run_day(w, d)
    mon = await _run_day(w, NEXT_MON, wrong={_due_snapshot(w.conn, NEXT_MON)[0]})
    wrong_card = mon.repeats[0]
    assert db.get_card(w.conn, wrong_card)["interval_days"] == 1
    tue = await _run_day(w, NEXT_MON + timedelta(days=1))
    assert wrong_card in tue.repeats


async def test_chain_task_left_open_expires_next_morning_without_bump(world):
    w = world
    fri = await _run_day(w, FRI, stop_after=2)
    left = db.open_task(w.conn, U)
    assert left is not None and left["morning"] == 0 and len(fri.repeats) == 3
    assert db.get_daily_state(w.conn, U)["missed_streak"] == 0   # ответы сбросили пятничный bump
    sat = await _run_day(w, FRI + timedelta(days=1))
    assert db.get_task(w.conn, left["id"])["status"] == "expired"
    assert sat.missed_after_morning == 0                          # задача цепочки — без bump
    assert sat.repeats[0] == left["card_id"]                      # и она самая просроченная
