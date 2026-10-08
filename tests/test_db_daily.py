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
          phrase_form="a heads-up", from_example=False, user_id=U, is_new=False):
    return db.create_task(
        conn, user_id=user_id, card_id=card_id, kind=kind, sentence=sentence,
        sentence_ru=sentence_ru, phrase_form=phrase_form,
        from_example=from_example, today=today, morning=morning, is_new=is_new)


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


def test_expire_with_include_grading_flips_zombie_grading(conn):
    cid = _add(conn, "a heads-up")
    tid = _task(conn, cid, morning=True)
    db.claim_task(conn, tid)
    assert db.expire_task(conn, tid) is None                        # контракт по умолчанию
    assert db.expire_task(conn, tid, include_grading=True) is True  # утро: зомби-grading
    assert db.get_task(conn, tid)["status"] == "expired"
    tid2 = _task(conn, cid, morning=False)
    assert db.expire_task(conn, tid2, include_grading=True) is False   # open тоже истекает
    assert db.get_task(conn, tid2)["status"] == "expired"
    tid3 = _task(conn, cid)
    db.claim_task(conn, tid3); db.finish_task(conn, tid3, ok=True)
    assert db.expire_task(conn, tid3, include_grading=True) is None    # answered не трогаем
    assert db.get_task(conn, tid3)["status"] == "answered"


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


# ---- дельта (р): issued_at ----

OLD_DAILY_TASKS_NO_ISSUED_AT = """
CREATE TABLE daily_tasks (
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
    answered_ok    INTEGER
);
"""


def test_fresh_db_has_issued_at(conn):
    assert "issued_at" in _columns(conn, "daily_tasks")


def test_migration_adds_issued_at_to_old_daily_tasks(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    conn.executescript(OLD_SCHEMA_NO_CONTEXT + OLD_DAILY_TASKS_NO_ISSUED_AT)
    conn.execute("INSERT INTO daily_tasks (user_id, card_id, kind, sent_on)"
                 " VALUES (1, 1, 'gap', '2026-10-01')")
    conn.commit()
    db.init_db(conn)
    assert "issued_at" in _columns(conn, "daily_tasks")
    assert conn.execute("SELECT issued_at FROM daily_tasks").fetchone()["issued_at"] is None
    db.init_db(conn)  # идемпотентно
    assert "issued_at" in _columns(conn, "daily_tasks")


def test_migration_on_db_without_daily_tasks_is_noop(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    conn.executescript(OLD_SCHEMA_NO_CONTEXT)   # прод до daily practice: таблицы задач нет
    db.init_db(conn)
    assert "issued_at" in _columns(conn, "daily_tasks")


def test_create_task_writes_iso_utc_issued_at(conn):
    from datetime import datetime, timedelta, timezone
    cid = _add(conn, "a heads-up")
    before = datetime.now(timezone.utc)
    tid = _task(conn, cid)
    stamp = datetime.fromisoformat(db.get_task(conn, tid)["issued_at"])
    assert stamp.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=1) <= stamp <= datetime.now(timezone.utc)


def test_create_task_accepts_explicit_issued_at(conn):
    from datetime import datetime, timezone
    cid = _add(conn, "a heads-up")
    at = datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc)
    tid = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence=None,
                         sentence_ru=None, phrase_form=None, from_example=False,
                         today=D0, morning=False, issued_at=at)
    assert db.get_task(conn, tid)["issued_at"] == at.isoformat()


# ---- Task 1 (2026-10-08): расписание повторов и новых слов ----

def _tasks_sql(conn, card_id, kind, sent_on):
    conn.execute(
        "INSERT INTO daily_tasks (user_id, card_id, kind, sent_on) VALUES (?,?,?,?)",
        (U, card_id, kind, sent_on))
    conn.commit()


def _legacy_db(conn_path):
    c = db.connect(conn_path)
    db.init_db(c)
    c.execute("ALTER TABLE daily_tasks DROP COLUMN is_new")
    c.commit()
    return c


def test_is_new_column_migration_idempotent(tmp_path):
    c = _legacy_db(str(tmp_path / "l.db"))
    assert "is_new" not in _columns(c, "daily_tasks")
    db.init_db(c)
    db.init_db(c)
    assert "is_new" in _columns(c, "daily_tasks")
    info = [r for r in c.execute("PRAGMA table_info(daily_tasks)") if r[1] == "is_new"][0]
    assert info["notnull"] == 1 and info["dflt_value"] == "0"


def test_backfill_marks_only_compose_hinted_and_counts_legacy_week(tmp_path):
    c = _legacy_db(str(tmp_path / "l.db"))
    c1 = _add(c, "a"); c2 = _add(c, "b")
    c.execute("INSERT INTO daily_tasks (user_id, card_id, kind, sent_on) VALUES (?,?,?,?)",
              (U, c1, "compose_hinted", "2026-10-07"))
    c.execute("UPDATE daily_tasks SET status = 'answered'")
    c.execute("INSERT INTO daily_tasks (user_id, card_id, kind, sent_on) VALUES (?,?,?,?)",
              (U, c2, "gap", "2026-10-07"))
    c.commit()
    db.init_db(c)
    kinds = {r["kind"]: r["is_new"] for r in c.execute("SELECT kind, is_new FROM daily_tasks")}
    assert kinds == {"compose_hinted": 1, "gap": 0}
    assert db.count_new_since(c, U, date(2026, 10, 5), date(2026, 10, 8)) == 1


def test_backfill_reruns_on_interrupted_migration(conn):
    # колонка уже есть, но UPDATE не отработал (процесс упал после ALTER)
    cid = _add(conn, "a")
    _tasks_sql(conn, cid, "compose_hinted", "2026-10-07")
    assert conn.execute("SELECT is_new FROM daily_tasks").fetchone()[0] == 0
    db.init_db(conn)
    assert conn.execute("SELECT is_new FROM daily_tasks").fetchone()[0] == 1
    db.init_db(conn)
    assert conn.execute("SELECT is_new FROM daily_tasks").fetchone()[0] == 1


def test_count_new_since_counts_card_once(conn):
    cid = _add(conn, "a")
    _task(conn, cid, is_new=True, today=date(2026, 10, 6), morning=True)
    conn.execute("UPDATE daily_tasks SET status='expired'"); conn.commit()
    _task(conn, cid, is_new=True, today=date(2026, 10, 7))
    assert db.count_new_since(conn, U, date(2026, 10, 5), date(2026, 10, 8)) == 1
    assert db.count_new_since(conn, U, date(2026, 10, 7), date(2026, 10, 8)) == 1
    assert db.count_new_since(conn, U, date(2026, 10, 8), date(2026, 10, 8)) == 0


def test_pick_due_repeat_order_and_filters(conn):
    a = _add(conn, "a"); b = _add(conn, "b"); c = _add(conn, "c"); n = _add(conn, "n")
    d = _add(conn, "d", translation="")
    for cid, iv, due in ((a, 1, "2026-10-06"), (b, 3, "2026-10-05"), (c, 3, "2026-10-05"),
                         (d, 1, "2026-10-01")):
        conn.execute("UPDATE cards SET interval_days=?, due_at=? WHERE id=?", (iv, due, cid))
    conn.execute("UPDATE cards SET due_at='2026-10-01' WHERE id=?", (n,))  # interval 0
    conn.commit()
    assert db.pick_due_repeat(conn, U, date(2026, 10, 8))["id"] == b  # самый просроченный, tie по id
    assert db.pick_due_repeat(conn, U, date(2026, 10, 4)) is None


def test_pick_new_card_order(conn):
    old = _add(conn, "old", today=date(2026, 9, 1))
    shown = _add(conn, "shown", today=date(2026, 9, 2))
    fresh = _add(conn, "fresh", today=date(2026, 9, 3))
    todayc = _add(conn, "todayc", today=date(2026, 10, 8))
    _task(conn, shown, is_new=True, today=date(2026, 10, 1))
    conn.execute("UPDATE daily_tasks SET status='expired'"); conn.commit()
    t = date(2026, 10, 8)
    assert db.pick_new_card(conn, U, t)["id"] == fresh      # не показанные первыми, свежие первыми
    conn.execute("UPDATE cards SET interval_days=1 WHERE id=?", (fresh,)); conn.commit()
    assert db.pick_new_card(conn, U, t)["id"] == old
    conn.execute("UPDATE cards SET interval_days=1 WHERE id=?", (old,)); conn.commit()
    assert db.pick_new_card(conn, U, t)["id"] == shown      # затем показанные
    assert todayc not in {db.pick_new_card(conn, U, t)["id"]}


def test_has_new_cards_created_on(conn):
    t = date(2026, 10, 8)
    assert not db.has_new_cards_created_on(conn, U, t)
    cid = _add(conn, "x", today=t)
    assert db.has_new_cards_created_on(conn, U, t)
    conn.execute("UPDATE cards SET interval_days=1 WHERE id=?", (cid,)); conn.commit()
    assert not db.has_new_cards_created_on(conn, U, t)


def test_counts_and_last_new_on(conn):
    a = _add(conn, "a"); b = _add(conn, "b")
    assert db.last_new_on(conn, U) is None
    _task(conn, a, is_new=True, today=date(2026, 10, 5))
    conn.execute("UPDATE daily_tasks SET status='answered'"); conn.commit()
    _task(conn, b, today=date(2026, 10, 8))
    assert db.count_new_on(conn, U, date(2026, 10, 5)) == 1
    assert db.count_repeats_on(conn, U, date(2026, 10, 5)) == 0
    assert db.count_repeats_on(conn, U, date(2026, 10, 8)) == 1
    assert db.count_new_on(conn, U, date(2026, 10, 8)) == 0
    assert db.last_new_on(conn, U) == date(2026, 10, 5)


def test_touch_task_issued_at(conn):
    from datetime import datetime, timezone
    tid = _task(conn, _add(conn, "a"))
    at = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    db.touch_task_issued_at(conn, tid, at)
    assert db.get_task(conn, tid)["issued_at"] == at.isoformat()


def test_pick_new_card_skips_unenriched_untranslated_and_foreign(conn):
    """Фильтры бывшего pick_due_card (Task 3 его удалил) — теперь у pick_new_card."""
    t = date(2026, 10, 8)
    _add(conn, "raw", enriched=False)
    _add(conn, "notr", translation="")
    assert db.pick_new_card(conn, U, t) is None
    ok = _add(conn, "ok")
    assert db.pick_new_card(conn, U, t)["id"] == ok
    assert db.pick_new_card(conn, 999, t) is None


def test_requested_column_migration_idempotent_and_create_task(tmp_path, conn):
    c = db.connect(str(tmp_path / "l.db"))
    db.init_db(c)
    c.execute("ALTER TABLE daily_tasks DROP COLUMN requested")   # БД до Round 1
    c.commit()
    assert "requested" not in _columns(c, "daily_tasks")
    db.init_db(c)
    db.init_db(c)
    info = [r for r in c.execute("PRAGMA table_info(daily_tasks)") if r[1] == "requested"][0]
    assert info["notnull"] == 1 and info["dflt_value"] == "0"
    cid = _add(conn, "a")
    plain = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence=None,
                           sentence_ru=None, phrase_form=None, from_example=False,
                           today=D0, morning=False)
    assert db.get_task(conn, plain)["requested"] == 0
    db.expire_task(conn, plain)
    req = db.create_task(conn, user_id=U, card_id=cid, kind="gap", sentence=None,
                         sentence_ru=None, phrase_form=None, from_example=False,
                         today=D0, morning=False, requested=True)
    assert db.get_task(conn, req)["requested"] == 1


def test_has_new_cards_created_on_ignores_unenriched_and_untranslated(conn):
    """Round 2: тот же фильтр _ENRICHED, что у pick_new_card."""
    t = date(2026, 10, 8)
    _add(conn, "raw", today=t, enriched=False)
    _add(conn, "notr", today=t, translation="")
    assert db.has_new_cards_created_on(conn, U, t) is False
    _add(conn, "ok", today=t)
    assert db.has_new_cards_created_on(conn, U, t) is True
