"""Таблицы и функции ежедневной практики (спека 2026-09-30-daily-practice)."""
import sqlite3
from datetime import date

import pytest

import db

OLD_SCHEMA_NO_CONTEXT = """
CREATE TABLE cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    word           TEXT NOT NULL,
    translation    TEXT,
    transcription  TEXT,
    example        TEXT,
    example_translation TEXT,
    audio_file_id  TEXT,
    enriched       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    due_at         TEXT NOT NULL,
    interval_days  INTEGER NOT NULL DEFAULT 0,
    reps           INTEGER NOT NULL DEFAULT 0,
    lapses         INTEGER NOT NULL DEFAULT 0
);
"""

U = 111
D0 = date(2026, 10, 1)


def _columns(conn, table="cards"):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add(conn, word, *, today=D0, context=None, translation="перевод",
         example=None, example_translation="Пример.", enriched=True):
    """Карточка пользователя U; пример по умолчанию содержит слово (нужно fallback-тестам)."""
    return db.add_card(
        conn, user_id=U, kind="phrase", word=word, translation=translation,
        transcription="/x/", example=f"An example with {word}." if example is None else example,
        example_translation=example_translation, enriched=enriched,
        today=today, context=context,
    )


def test_migration_adds_context_column_to_old_db(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    conn.executescript(OLD_SCHEMA_NO_CONTEXT)
    conn.execute(
        "INSERT INTO cards (user_id, kind, word, created_at, due_at)"
        " VALUES (1, 'word', 'mesa', '2026-06-01', '2026-06-01')")
    conn.commit()
    assert "context" not in _columns(conn)
    db.init_db(conn)
    assert "context" in _columns(conn)
    row = conn.execute("SELECT word, context FROM cards").fetchone()
    assert row["word"] == "mesa" and row["context"] is None
    db.init_db(conn)  # идемпотентно
    assert "context" in _columns(conn)


def test_fresh_db_has_context_and_daily_tables(conn):
    assert "context" in _columns(conn)


def test_add_card_without_context_keeps_old_signature(conn):
    cid = db.add_card(
        conn, user_id=U, kind="word", word="mesa", translation="стол",
        transcription="мЭса", example="e", example_translation="э",
        enriched=True, today=D0)
    assert db.get_card(conn, cid)["context"] is None


def test_add_card_stores_context(conn):
    cid = _add(conn, "a heads-up", context="рабочий созвон, релиз")
    assert db.get_card(conn, cid)["context"] == "рабочий созвон, релиз"


def _task(conn, card_id, *, today=D0, morning=True, kind="gap",
          sentence="Just a heads-up, tests are late.",
          sentence_ru="Предупреждаю: тесты опаздывают.",
          phrase_form="a heads-up", from_example=False, user_id=U):
    return db.create_task(
        conn, user_id=user_id, card_id=card_id, kind=kind, sentence=sentence,
        sentence_ru=sentence_ru, phrase_form=phrase_form,
        from_example=from_example, today=today, morning=morning)


def test_daily_tables_exist(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily_tasks", "daily_state"} <= tables
    assert {"morning", "from_example", "reply_sentence"} <= _columns(conn, "daily_tasks")


def test_create_and_open_task(conn):
    cid = _add(conn, "a heads-up")
    assert db.open_task(conn, U) is None
    tid = _task(conn, cid)
    t = db.open_task(conn, U)
    assert t["id"] == tid and t["status"] == "open" and t["morning"] == 1
    assert t["sent_on"] == "2026-10-01"
    assert db.open_task(conn, 999) is None  # чужого не видно


def test_second_open_task_is_rejected_by_unique_index(conn):
    cid = _add(conn, "a heads-up")
    _task(conn, cid)
    with pytest.raises(sqlite3.IntegrityError):
        _task(conn, cid)


def test_claim_then_second_claim_fails(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid)
    assert db.claim_task(conn, tid) is True
    assert db.get_task(conn, tid)["status"] == "grading"
    assert db.claim_task(conn, tid) is False
    # grading всё ещё считается активной
    assert db.open_task(conn, U)["id"] == tid


def test_release_only_from_grading(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid)
    assert db.release_task(conn, tid) is False      # open → нельзя
    db.claim_task(conn, tid)
    assert db.release_task(conn, tid) is True       # grading → open
    assert db.get_task(conn, tid)["status"] == "open"


def test_finish_only_from_grading_and_writes_fields(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid)
    assert db.finish_task(conn, tid, ok=True) is False
    db.claim_task(conn, tid)
    assert db.finish_task(conn, tid, ok=True, reply_sentence="Appreciate the heads-up.") is True
    t = db.get_task(conn, tid)
    assert t["status"] == "answered" and t["answered_ok"] == 1
    assert t["reply_sentence"] == "Appreciate the heads-up."
    assert db.open_task(conn, U) is None
    # после answered можно открыть новую
    _task(conn, cid, morning=False)


def test_expire_returns_morning_flag_and_only_from_open(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid, morning=True)
    assert db.expire_task(conn, tid) is True
    assert db.get_task(conn, tid)["status"] == "expired"
    assert db.expire_task(conn, tid) is None          # уже не open
    tid2 = _task(conn, cid, morning=False)
    assert db.expire_task(conn, tid2) is False        # не утренняя
    tid3 = _task(conn, cid)
    db.claim_task(conn, tid3)
    assert db.expire_task(conn, tid3) is None         # grading не истекает


def test_set_task_kind(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid, kind="listen")
    db.set_task_kind(conn, tid, "gap")
    assert db.get_task(conn, tid)["kind"] == "gap"


def test_release_stale_grading_on_startup(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid)
    db.claim_task(conn, tid)
    assert db.release_stale_grading(conn) == 1
    assert db.get_task(conn, tid)["status"] == "open"
    assert db.release_stale_grading(conn) == 0


def test_pick_due_card_prefers_new_then_most_overdue(conn):
    today = date(2026, 10, 5)
    old = _add(conn, "old phrase", today=date(2026, 9, 1))
    db.update_review(conn, old, interval_days=3, due_at=date(2026, 9, 4), remembered=True)
    older = _add(conn, "older phrase", today=date(2026, 8, 1))
    db.update_review(conn, older, interval_days=7, due_at=date(2026, 8, 8), remembered=True)
    new = _add(conn, "new phrase", today=date(2026, 10, 4))
    assert db.pick_due_card(conn, U, today)["id"] == new           # новая первой
    db.update_review(conn, new, interval_days=1, due_at=date(2026, 10, 6), remembered=True)
    assert db.pick_due_card(conn, U, today)["id"] == older         # самая просроченная


def test_pick_due_card_skips_today_created_unenriched_and_untranslated(conn):
    today = date(2026, 10, 5)
    _add(conn, "created today", today=today)                       # created_at == today
    _add(conn, "not enriched", today=date(2026, 10, 1), enriched=False)
    _add(conn, "no translation", today=date(2026, 10, 1), translation=None)
    _add(conn, "empty translation", today=date(2026, 10, 1), translation="")
    assert db.pick_due_card(conn, U, today) is None
    ok = _add(conn, "fine", today=date(2026, 10, 4))
    assert db.pick_due_card(conn, U, today)["id"] == ok
    assert db.pick_due_card(conn, 999, today) is None


def test_pick_due_card_respects_due_at(conn):
    today = date(2026, 10, 5)
    cid = _add(conn, "fine", today=date(2026, 10, 1))
    db.update_review(conn, cid, interval_days=30, due_at=date(2026, 11, 1), remembered=True)
    assert db.pick_due_card(conn, U, today) is None


def test_recent_sentences_includes_replies_excludes_examples(conn):
    cid = _add(conn, "a heads-up")
    t1 = _task(conn, cid, sentence="S1", sentence_ru="r", morning=False)
    db.claim_task(conn, t1); db.finish_task(conn, t1, ok=True, reply_sentence="R1")
    t2 = _task(conn, cid, sentence="EX", sentence_ru="r", from_example=True, morning=False)
    db.claim_task(conn, t2); db.finish_task(conn, t2, ok=True, reply_sentence="R2")
    t3 = _task(conn, cid, sentence="S3", sentence_ru="r", morning=False)
    db.claim_task(conn, t3); db.finish_task(conn, t3, ok=False, reply_sentence="S1")
    assert db.recent_sentences(conn, cid) == ["S3", "S1", "R2", "R1"]   # EX исключён, R2 — нет
    assert db.recent_sentences(conn, cid, n=2) == ["S3", "S1"]
    assert db.recent_sentences(conn, 12345) == []


def test_count_tasks_on(conn):
    cid = _add(conn, "a heads-up")
    t = _task(conn, cid, today=date(2026, 10, 5)); db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    t = _task(conn, cid, today=date(2026, 10, 5), morning=False); db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    _task(conn, cid, today=date(2026, 10, 4))
    assert db.count_tasks_on(conn, U, date(2026, 10, 5)) == 2
    assert db.count_tasks_on(conn, U, date(2026, 10, 6)) == 0


def test_daily_state_counters(conn):
    st = db.get_daily_state(conn, U)
    assert st["missed_streak"] == 0 and st["last_sent_on"] is None
    db.bump_missed(conn, U); db.bump_missed(conn, U)
    assert db.get_daily_state(conn, U)["missed_streak"] == 2
    db.set_last_sent(conn, U, date(2026, 10, 5))
    assert db.get_daily_state(conn, U)["last_sent_on"] == "2026-10-05"
    db.reset_missed(conn, U)
    st = db.get_daily_state(conn, U)
    assert st["missed_streak"] == 0 and st["last_sent_on"] == "2026-10-05"
    db.reset_missed(conn, 777)  # на несуществующем — не падает
    assert db.get_daily_state(conn, 777)["missed_streak"] == 0
