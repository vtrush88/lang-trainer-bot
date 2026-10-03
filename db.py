from __future__ import annotations

import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    word           TEXT NOT NULL,
    translation    TEXT,
    transcription  TEXT,
    example        TEXT,
    example_translation TEXT,
    context        TEXT,
    audio_file_id  TEXT,
    enriched       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    due_at         TEXT NOT NULL,
    interval_days  INTEGER NOT NULL DEFAULT 0,
    reps           INTEGER NOT NULL DEFAULT 0,
    lapses         INTEGER NOT NULL DEFAULT 0
);
"""

_COLUMN_RENAMES = (
    ("spanish", "word"),
    ("russian", "translation"),
    ("example_es", "example"),
    ("example_ru", "example_translation"),
)


def _migrate_column_names(conn: sqlite3.Connection) -> None:
    """Разовое переименование испаноязычных колонок (деплой 2026-08).

    Поколоночный гард, НЕ транзакция: DDL в python-sqlite3 автокоммитится,
    поэтому атомарности всё равно нет — зато каждая проверка идемпотентна,
    и прерванная миграция дозавершается при следующем старте.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    if not cols:
        return  # свежая база: таблицы ещё нет, создастся сразу с новыми именами
    for old, new in _COLUMN_RENAMES:
        if old in cols:
            conn.execute(f"ALTER TABLE cards RENAME COLUMN {old} TO {new}")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


_ADDED_COLUMNS = (
    ("context", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Добавление колонок, появившихся после первого деплоя (2026-10, daily practice).

    Идемпотентно: гард по PRAGMA, DDL автокоммитится — транзакции нет.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    if not cols:
        return  # свежая база: CREATE TABLE уже содержит новые колонки
    for name, ddl in _ADDED_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_column_names(conn)
    _add_missing_columns(conn)
    conn.executescript(SCHEMA)
    conn.commit()


def add_card(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    kind: str,
    word: str,
    translation: str | None,
    transcription: str | None,
    example: str | None,
    example_translation: str | None,
    enriched: bool,
    today: date,
    context: str | None = None,
) -> int:
    iso = today.isoformat()
    cur = conn.execute(
        """
        INSERT INTO cards (user_id, kind, word, translation, transcription,
                           example, example_translation, context, enriched,
                           created_at, due_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, kind, word, translation, transcription, example,
         example_translation, context, int(enriched), iso, iso),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,))
    return cur.fetchone()


def update_review(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    interval_days: int,
    due_at: date,
    remembered: bool,
) -> None:
    conn.execute(
        """
        UPDATE cards
        SET interval_days = ?, due_at = ?, reps = reps + 1,
            lapses = lapses + ?
        WHERE id = ?
        """,
        (interval_days, due_at.isoformat(), 0 if remembered else 1, card_id),
    )
    conn.commit()


def get_due_cards(
    conn: sqlite3.Connection, user_id: int, today: date
) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM cards WHERE user_id = ? AND due_at <= ? ORDER BY due_at",
        (user_id, today.isoformat()),
    )
    return cur.fetchall()


def list_cards(
    conn: sqlite3.Connection, user_id: int, limit: int, offset: int
) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM cards WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    return cur.fetchall()


def count_cards(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM cards WHERE user_id = ?", (user_id,)
    )
    return int(cur.fetchone()["n"])


def delete_card(conn: sqlite3.Connection, card_id: int, user_id: int) -> None:
    """Delete a card only if it belongs to user_id (no cross-user deletes)."""
    conn.execute("DELETE FROM cards WHERE id = ? AND user_id = ?",
                 (card_id, user_id))
    conn.commit()


def set_audio_file_id(
    conn: sqlite3.Connection, card_id: int, file_id: str
) -> None:
    conn.execute(
        "UPDATE cards SET audio_file_id = ? WHERE id = ?", (file_id, card_id)
    )
    conn.commit()


def update_enrichment(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    translation: str,
    transcription: str,
    example: str,
    example_translation: str,
) -> None:
    conn.execute(
        """
        UPDATE cards
        SET translation = ?, transcription = ?, example = ?, example_translation = ?,
            enriched = 1
        WHERE id = ?
        """,
        (translation, transcription, example, example_translation, card_id),
    )
    conn.commit()


def card_exists(conn: sqlite3.Connection, user_id: int, word: str) -> bool:
    """Case-insensitive check whether the user already has this target-language word.

    Compares in Python so accented letters (á, ñ, …) fold correctly, which
    sqlite's ASCII-only lower() would miss.
    """
    target = word.strip().lower()
    rows = conn.execute(
        "SELECT word FROM cards WHERE user_id = ?", (user_id,)
    ).fetchall()
    return any((r["word"] or "").strip().lower() == target for r in rows)
