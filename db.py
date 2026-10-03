from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

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
CREATE TABLE IF NOT EXISTS daily_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    card_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    sentence       TEXT,
    sentence_ru    TEXT,
    phrase_form    TEXT,
    reply_sentence TEXT,
    from_example   INTEGER NOT NULL DEFAULT 0,
    sent_on        TEXT NOT NULL,
    morning        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',
    answered_ok    INTEGER,
    issued_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS daily_tasks_one_open
    ON daily_tasks(user_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS daily_state (
    user_id        INTEGER PRIMARY KEY,
    missed_streak  INTEGER NOT NULL DEFAULT 0,
    last_sent_on   TEXT
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
    ("cards", "context", "TEXT"),
    ("daily_tasks", "issued_at", "TEXT"),   # ISO UTC; NULL у старых строк = «не stale»
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Добавление колонок, появившихся после первого деплоя (2026-10, daily practice).

    Идемпотентно: гард по PRAGMA, DDL автокоммитится — транзакции нет.
    """
    for table, name, ddl in _ADDED_COLUMNS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue  # таблицы ещё нет: CREATE TABLE из SCHEMA создаст её сразу с колонкой
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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


# --- daily practice: задания -------------------------------------------------

TASK_OPEN, TASK_GRADING, TASK_ANSWERED, TASK_EXPIRED = (
    "open", "grading", "answered", "expired")


def create_task(
    conn: sqlite3.Connection, *, user_id: int, card_id: int, kind: str,
    sentence: str | None, sentence_ru: str | None, phrase_form: str | None,
    from_example: bool, today: date, morning: bool,
    issued_at: datetime | None = None,
) -> int:
    """issued_at — момент выдачи (aware UTC); по умолчанию сейчас. Пишется ISO-строкой."""
    stamp = (issued_at or datetime.now(timezone.utc)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO daily_tasks (user_id, card_id, kind, sentence, sentence_ru,
                                 phrase_form, from_example, sent_on, morning, issued_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, card_id, kind, sentence, sentence_ru, phrase_form,
         int(from_example), today.isoformat(), int(morning), stamp),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM daily_tasks WHERE id = ?",
                        (task_id,)).fetchone()


def open_task(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Активная задача пользователя: open или grading (grading живёт секунды)."""
    return conn.execute(
        "SELECT * FROM daily_tasks WHERE user_id = ? AND status IN (?, ?)"
        " ORDER BY id DESC LIMIT 1",
        (user_id, TASK_OPEN, TASK_GRADING),
    ).fetchone()


def _transition(conn: sqlite3.Connection, task_id: int, src: str, dst: str,
                extra_sql: str = "", extra_params: tuple = ()) -> bool:
    cur = conn.execute(
        f"UPDATE daily_tasks SET status = ?{extra_sql} WHERE id = ? AND status = ?",
        (dst, *extra_params, task_id, src),
    )
    conn.commit()
    return cur.rowcount == 1


def claim_task(conn: sqlite3.Connection, task_id: int) -> bool:
    return _transition(conn, task_id, TASK_OPEN, TASK_GRADING)


def release_task(conn: sqlite3.Connection, task_id: int) -> bool:
    return _transition(conn, task_id, TASK_GRADING, TASK_OPEN)


def finish_task(conn: sqlite3.Connection, task_id: int, *, ok: bool,
                reply_sentence: str | None = None) -> bool:
    return _transition(conn, task_id, TASK_GRADING, TASK_ANSWERED,
                       ", answered_ok = ?, reply_sentence = ?",
                       (int(ok), reply_sentence))


def expire_task(conn: sqlite3.Connection, task_id: int, *,
                include_grading: bool = False) -> bool | None:
    """open → expired. Возвращает флаг morning истёкшей задачи, None если не истекла.

    include_grading=True (только утро, под локом пользователя): grading тоже истекает —
    под локом оценки в полёте нет, такой grading — зомби после сбоя без рестарта.
    """
    sources = (TASK_OPEN, TASK_GRADING) if include_grading else (TASK_OPEN,)
    row = conn.execute(
        f"SELECT morning, status FROM daily_tasks WHERE id = ?"
        f" AND status IN ({', '.join('?' * len(sources))})",
        (task_id, *sources)).fetchone()
    if row is None:
        return None
    if not _transition(conn, task_id, row["status"], TASK_EXPIRED):
        return None
    return bool(row["morning"])


def set_task_kind(conn: sqlite3.Connection, task_id: int, kind: str) -> None:
    conn.execute("UPDATE daily_tasks SET kind = ? WHERE id = ?", (kind, task_id))
    conn.commit()


def release_stale_grading(conn: sqlite3.Connection) -> int:
    """На старте бота: оценок в полёте нет, любой grading — зомби после падения."""
    cur = conn.execute("UPDATE daily_tasks SET status = ? WHERE status = ?",
                       (TASK_OPEN, TASK_GRADING))
    conn.commit()
    return cur.rowcount


def pick_due_card(conn: sqlite3.Connection, user_id: int, today: date) -> sqlite3.Row | None:
    """Карточка для задания: новые первыми (тёплый контекст), потом самая просроченная.

    Только обогащённые с переводом и созданные ДО сегодня («придёт завтра утром»).
    """
    iso = today.isoformat()
    return conn.execute(
        """
        SELECT * FROM cards
        WHERE user_id = ? AND due_at <= ? AND enriched = 1
          AND translation IS NOT NULL AND translation != ''
          AND created_at < ?
        ORDER BY (interval_days = 0) DESC, due_at, id
        LIMIT 1
        """,
        (user_id, iso, iso),
    ).fetchone()


def recent_sentences(conn: sqlite3.Connection, card_id: int, n: int = 6) -> list[str]:
    rows = conn.execute(
        "SELECT CASE WHEN from_example = 0 THEN sentence END AS sentence, reply_sentence"
        " FROM daily_tasks WHERE card_id = ? ORDER BY id DESC",
        (card_id,),
    ).fetchall()
    out: list[str] = []
    for row in rows:
        for s in (row["sentence"], row["reply_sentence"]):
            if s and s not in out:
                out.append(s)
            if len(out) >= n:
                return out
    return out


def count_tasks_on(conn: sqlite3.Connection, user_id: int, today: date) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_tasks WHERE user_id = ? AND sent_on = ?",
        (user_id, today.isoformat()))
    return int(cur.fetchone()["n"])


def get_daily_state(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO daily_state (user_id) VALUES (?)", (user_id,))
    conn.commit()
    return conn.execute("SELECT * FROM daily_state WHERE user_id = ?",
                        (user_id,)).fetchone()


def bump_missed(conn: sqlite3.Connection, user_id: int) -> None:
    get_daily_state(conn, user_id)
    conn.execute("UPDATE daily_state SET missed_streak = missed_streak + 1 WHERE user_id = ?",
                 (user_id,))
    conn.commit()


def reset_missed(conn: sqlite3.Connection, user_id: int) -> None:
    get_daily_state(conn, user_id)
    conn.execute("UPDATE daily_state SET missed_streak = 0 WHERE user_id = ?", (user_id,))
    conn.commit()


def set_last_sent(conn: sqlite3.Connection, user_id: int, today: date) -> None:
    get_daily_state(conn, user_id)
    conn.execute("UPDATE daily_state SET last_sent_on = ? WHERE user_id = ?",
                 (today.isoformat(), user_id))
    conn.commit()
