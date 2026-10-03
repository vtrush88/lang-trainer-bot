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
