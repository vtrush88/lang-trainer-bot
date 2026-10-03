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
