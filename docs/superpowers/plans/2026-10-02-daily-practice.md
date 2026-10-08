# Daily Practice («своё слово в день») Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Англо-бот сам пишет раз в день одно задание по одной фразе пользователя (своё предложение / пропуск / вспомнить / услышать), каждый раз в новом предложении в контексте, откуда фраза пришла; фразы попадают в бот пересланным сообщением с контекстом. Мамин es-бот не меняется ни одним текстом и ни одним запросом к Gemini.

**Architecture:** Новый чистый модуль `daily.py` (выбор задания, тихий режим, расписание, проверка слуха, классификация входящего текста, рендер текстов) + его IO-часть (`send_daily_task`, `answer_task`, `daily_loop`) под per-user `asyncio.Lock`; два новых Gemini-сервиса (`services/sentences.py`, `services/capture.py`); новая таблица `daily_tasks` с жизненным циклом open→grading→answered|expired в SQLite (не в FSM — переживает рестарт); тонкий роутер `handlers/daily.py`, подключаемый последним и только для профиля с `daily_practice=True`. Таймер — in-process asyncio-цикл по образцу спеки reminder (stdlib `zoneinfo`, delay в абсолютном времени).

**Tech Stack:** Python 3.12 · aiogram 3.13.1 (long-polling, MemoryStorage FSM) · SQLite (stdlib) · google-genai 2.8.0 (`generate_json`, structured JSON) · edge-tts 7.2.8 · stdlib `zoneinfo`, `difflib` · pytest + pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-09-30-daily-practice-design.md` — продуктовая спека простым языком (3 раунда кросс-модельного ревью, утверждена Victoria 2026-10-02; переписана продуктовым языком 2026-10-04). **Технический дизайн, перенесённый из неё, — приложение A в конце этого плана.** Интент: `docs/superpowers/intent/daily-practice.md`.

## Global Constraints

- **Инвариант мамы:** es-профиль байт-в-байт равен текущему (`tests/test_languages.py`). Новые поля `LanguageProfile` — только с дефолтами; ES их не задаёт. Ни один существующий текст/промпт/схема не меняется. `handlers/add.py` правится в одном месте и под флагом `profile.daily_practice`.
- **Два гейта:** хендлеры daily подключаются только при `profile.daily_practice`; таймер стартует только при `profile.daily_practice and cfg.daily_at is not None`.
- **Зависимости не трогаем:** `google-genai==2.8.0`, ничего нового в `requirements.txt`; `zoneinfo`/`difflib` — stdlib.
- **`generate_json` возвращает только `dict`** — все схемы верхнего уровня `OBJECT`; массив заворачивается в `{"items": [...]}`.
- **Одно задание = одно сообщение Telegram;** голос — `send_voice` с `caption` (≤ 1024 символов). HTML только через `parse_mode="HTML"` с экранированием подстановок (`html.escape(..., quote=False)`); у бота нет дефолтного parse mode.
- **Тексты — гендер-нейтральные** (нет «написала», «умница», нет прошедшего времени 1-го лица у бота).
- **`due_at`/`created_at`/`sent_on` в БД — ISO-строки;** сравнивать со строкой `today.isoformat()`, не с `date`.
- **Абсолютный delay таймера:** `target.timestamp() - now.timestamp()`, target строится свежим aware `datetime` на календарный день (DST-грабля из спеки reminder).
- **Миграция `cards.context`:** `ALTER TABLE ... ADD COLUMN` под гардом `PRAGMA table_info`, идемпотентно; DDL автокоммитится — без транзакции.
- **Отклонения от спеки, принятые в плане** (внести в спеку в Task 14): (а) `send_daily_task` возвращает ещё `"failed"` (ошибка отправки — иначе `/next` соврал бы «нечего повторять») и `"limit"` (потолок дня проверяется внутри лока по параметру `limit`, а не в хендлере); (б) fallback-пример карточки **пишется** в `daily_tasks.sentence` (нужен для оценки gap/listen и для повтора по `/next`), но помечается `from_example=1` и исключается из `recent_sentences`; (в) `compose_hinted` при недоступной озвучке показывает предложение текстом с пометкой 🔇.
- Работаем в этом репо на ветке `daily-practice`, БЕЗ git-worktree (репо вложено в Obsidian-vault). Создать в начале: `git checkout -b daily-practice`.
- После каждой задачи: `.venv/bin/pytest -q` зелёный И `.venv/bin/python -c "import bot"` без ошибок. Базовая линия — **124 passed**.
- Коммиты БЕЗ подписи (глобальный `gpgsign=false`, репо не lidofinance; `-S` не добавлять). Разрешение Victoria на коммиты этого плана получается батчем при запуске исполнения; permission-хук всё равно может спрашивать — это нормально.
- **Живой бот на VPS не трогать** до отдельного решения о деплое (один поллер на токен). Локально с боевым токеном не запускать.

## Review Focus

Что спека подразумевает, но ни один тест по умолчанию не ловит — у каждого пункта ниже тест добавлен в задачу-владельца:

1. **Ответ при открытом задании, когда карточка уже удалена пользователем через «Мой словарь»** — ожидание: вежливое «фраза удалена», задача закрыта, никакого SRS и никакого исключения в логе (Task 12).
2. **Пересланное сообщение с `+` в начале или пустое после `+`** («+» без слова) — ожидание: не падаем в `extract` с пустой строкой, просим написать слово (Task 13).
3. **`phrase_form` с другим регистром в предложении** («Heads-up» в начале предложения, `phrase_form="heads-up"`) — ожидание: `blank_out` и `answers_match` принимают, пропуск ставится корректно (Task 4).
4. **Два пользователя в один утренний прогон, у одного `TelegramForbiddenError`** — ожидание: второй получает задание, у первого нет созданной задачи и нет роста пропусков (Task 11).
5. **Повтор `/next` для голосового задания при падении TTS во второй раз** — ожидание: задание повторяется текстом (listen → gap по тексту), а не молчанием (Task 11).

---

### Task 1: `cards.context` — колонка, миграция, `add_card`, `card_preview`

**Files:**
- Modify: `db.py` (SCHEMA, `init_db`, новая `_add_missing_columns`, `add_card`)
- Modify: `formatting.py` (`card_preview`)
- Test: `tests/test_db_daily.py` (новый), `tests/test_formatting.py` (дополнить)

**Interfaces:**
- Consumes: существующие `db.connect/init_db/add_card/get_card`, `formatting.card_preview`.
- Produces: `db.add_card(..., context: str | None = None) -> int`; колонка `cards.context TEXT`; `formatting.card_preview(card)` печатает строку `📍 контекст: …` если контекст непустой (единственное место печати контекста); `formatting.field(card, key) -> Any | None` — безопасное чтение ключа из `dict` или `sqlite3.Row`; `formatting.esc(value) -> str` — HTML-экранирование (модульное, используется и в `daily.py`).

- [ ] **Step 1: Падающие тесты миграции и `add_card`**

`tests/test_db_daily.py` (новый файл, начало):

```python
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
```

Дополнить `tests/test_formatting.py`:

```python
import sqlite3

import db
from formatting import card_preview, esc, field

CARD = {"word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up.",
        "example_translation": "Просто предупреждаю."}


def test_card_preview_without_context_is_unchanged():
    text = card_preview(CARD)
    assert "📍" not in text
    assert text == (
        "🔤 <b>a heads-up</b>\n"
        "🇷🇺 предупреждение заранее\n"
        "🗣 произношение: /ˈhedz ʌp/\n"
        "📝 пример: Just a heads-up. — Просто предупреждаю."
    )


def test_card_preview_with_context_adds_line():
    text = card_preview({**CARD, "context": "созвон <QA>"})
    assert text.endswith("\n📍 контекст: созвон &lt;QA&gt;")


def test_card_preview_empty_context_is_skipped():
    assert "📍" not in card_preview({**CARD, "context": ""})


def test_esc_escapes_html_but_not_quotes():
    assert esc("<a&b>") == "&lt;a&amp;b&gt;"
    assert esc('say "hi"') == 'say "hi"'


def test_field_reads_dict_and_row_safely():
    assert field(CARD, "context") is None
    assert field({**CARD, "context": "x"}, "context") == "x"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 'w' AS word").fetchone()
    assert field(row, "word") == "w"
    assert field(row, "context") is None
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/test_db_daily.py tests/test_formatting.py -q`
Expected: FAIL — `add_card() got an unexpected keyword argument 'context'`, `ImportError: cannot import name 'field'`.

- [ ] **Step 3: Реализация в `db.py`**

В `SCHEMA` добавить колонку в `CREATE TABLE cards` (после `example_translation`):

```python
    example_translation TEXT,
    context        TEXT,
    audio_file_id  TEXT,
```

Добавить после `_migrate_column_names`:

```python
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
```

`init_db`:

```python
def init_db(conn: sqlite3.Connection) -> None:
    _migrate_column_names(conn)
    _add_missing_columns(conn)
    conn.executescript(SCHEMA)
    conn.commit()
```

`add_card` — новый необязательный параметр (последним):

```python
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
```

- [ ] **Step 4: Реализация в `formatting.py`**

```python
def esc(value) -> str:
    """HTML-экранирование подстановок для parse_mode="HTML" (кавычки не трогаем)."""
    return html.escape(str(value), quote=False)


def field(card, key: str):
    """Безопасно прочитать ключ из dict ИЛИ sqlite3.Row (у Row нет .get)."""
    try:
        return card[key]
    except (KeyError, IndexError):
        return None


def card_preview(card: dict) -> str:
    """Word card as Telegram HTML — send with parse_mode="HTML".

    The target-language word is bold; every interpolated field is
    HTML-escaped so a literal <, > or & in the data can't break Telegram's
    HTML parser. «📍 контекст» печатается только здесь и только если он есть.
    """
    text = (
        f"🔤 <b>{esc(card['word'])}</b>\n"
        f"🇷🇺 {esc(card['translation'])}\n"
        f"🗣 произношение: {esc(card['transcription'])}\n"
        f"📝 пример: {esc(card['example'])} — {esc(card['example_translation'])}"
    )
    context = field(card, "context")
    if context:
        text += f"\n📍 контекст: {esc(context)}"
    return text
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/pytest -q`
Expected: все зелёные (124 + 9 новых = 133 passed), `.venv/bin/python -c "import bot"` без ошибок.

- [ ] **Step 6: Commit**

```bash
git add db.py formatting.py tests/test_db_daily.py tests/test_formatting.py
git commit -m "db: cards.context column (idempotent migration) + card_preview context line"
```

---

### Task 2: Таблицы `daily_tasks` / `daily_state` и жизненный цикл задачи

**Files:**
- Modify: `db.py` (SCHEMA + функции)
- Test: `tests/test_db_daily.py` (дополнить)

**Interfaces:**
- Produces (все в `db.py`):
  - `create_task(conn, *, user_id, card_id, kind, sentence, sentence_ru, phrase_form, from_example: bool, today: date, morning: bool) -> int`
  - `get_task(conn, task_id) -> sqlite3.Row | None`
  - `open_task(conn, user_id) -> sqlite3.Row | None` — задача в статусе `open` ИЛИ `grading`
  - `claim_task(conn, task_id) -> bool` — `open → grading`
  - `release_task(conn, task_id) -> bool` — `grading → open`
  - `finish_task(conn, task_id, *, ok: bool, reply_sentence: str | None = None) -> bool` — `grading → answered`, пишет `answered_ok`, `reply_sentence`
  - `expire_task(conn, task_id) -> bool | None` — `open → expired`; возвращает `morning` истёкшей задачи (`True/False`) или `None`, если задача не была `open`
  - `release_stale_grading(conn) -> int` — все `grading → open`, число строк
  - `set_task_kind(conn, task_id, kind: str) -> None` — понижение вида при повторе (`listen → gap`, если озвучка снова не удалась), чтобы оценка в Task 12 шла по тому виду, который пользователь реально увидел
  - Статусы: строки `"open" | "grading" | "answered" | "expired"`.

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_db_daily.py`:

```python
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
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_db_daily.py -q`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'create_task'` и т.п.

- [ ] **Step 3: Реализация**

В `SCHEMA` (после `cards`):

```sql
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
    answered_ok    INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS daily_tasks_one_open
    ON daily_tasks(user_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS daily_state (
    user_id        INTEGER PRIMARY KEY,
    missed_streak  INTEGER NOT NULL DEFAULT 0,
    last_sent_on   TEXT
);
```

Функции (в конец `db.py`):

```python
# --- daily practice: задания -------------------------------------------------

TASK_OPEN, TASK_GRADING, TASK_ANSWERED, TASK_EXPIRED = (
    "open", "grading", "answered", "expired")


def create_task(
    conn: sqlite3.Connection, *, user_id: int, card_id: int, kind: str,
    sentence: str | None, sentence_ru: str | None, phrase_form: str | None,
    from_example: bool, today: date, morning: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO daily_tasks (user_id, card_id, kind, sentence, sentence_ru,
                                 phrase_form, from_example, sent_on, morning)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, card_id, kind, sentence, sentence_ru, phrase_form,
         int(from_example), today.isoformat(), int(morning)),
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


def expire_task(conn: sqlite3.Connection, task_id: int) -> bool | None:
    """open → expired. Возвращает флаг morning истёкшей задачи, None если не была open."""
    row = conn.execute("SELECT morning FROM daily_tasks WHERE id = ? AND status = ?",
                       (task_id, TASK_OPEN)).fetchone()
    if row is None:
        return None
    if not _transition(conn, task_id, TASK_OPEN, TASK_EXPIRED):
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
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q`
Expected: зелёные (133 + 9 = 142 passed).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db_daily.py
git commit -m "db: daily_tasks/daily_state tables + task lifecycle (open/grading/answered/expired)"
```

---

### Task 3: Выбор карточки, история предложений, счётчики дня и тихого режима

**Files:**
- Modify: `db.py`
- Test: `tests/test_db_daily.py` (дополнить)

**Interfaces:**
- Produces (в `db.py`):
  - `pick_due_card(conn, user_id, today: date) -> sqlite3.Row | None` — `due_at <= today`, `enriched = 1`, непустой `translation`, `created_at < today`; порядок: новые (`interval_days = 0`) первыми по `id`, затем по `due_at, id`.
  - `recent_sentences(conn, card_id, n: int = 6) -> list[str]` — последние `sentence` (только у задач с `from_example = 0`) и `reply_sentence` (у всех задач) карточки, непустые, новые первыми, без дублей.
  - `count_tasks_on(conn, user_id, today: date) -> int`
  - `get_daily_state(conn, user_id) -> sqlite3.Row` — создаёт строку, если нет (`missed_streak=0`, `last_sent_on=None`)
  - `bump_missed(conn, user_id) -> None`, `reset_missed(conn, user_id) -> None`, `set_last_sent(conn, user_id, today: date) -> None`

- [ ] **Step 1: Падающие тесты**

```python
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
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_db_daily.py -q` → FAIL (`no attribute 'pick_due_card'`…).

- [ ] **Step 3: Реализация**

```python
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
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные (140 + 6 = 146 passed).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db_daily.py
git commit -m "db: pick_due_card, recent_sentences, count_tasks_on, daily_state counters"
```

---

### Task 4: `daily.py` — чистая логика: вид задания, тихий режим, понижение, пропуск, слух, классификация текста

**Files:**
- Create: `daily.py`
- Test: `tests/test_daily.py` (новый)

**Interfaces:**
- Consumes: `services.srs.LADDER`, `intents.is_giveup`.
- Produces (в `daily.py`):
  - константы `KINDS = ("compose_hinted", "gap", "recall", "listen", "compose")`, `VOICE_KINDS = frozenset({"compose_hinted", "listen"})`, `MAX_TASKS_PER_DAY = 3`, `MISSED_QUIET_AFTER = 3`, `QUIET_PERIOD_DAYS = 7`, `WORD_RATIO = 0.8`
  - `task_kind(interval_days: int, rng: random.Random) -> str`
  - `should_send(missed_streak: int, last_sent_on: str | None, today: date) -> bool`
  - `downgrade_kind(kind: str, *, has_sentence: bool, has_voice: bool) -> str`
  - `blank_out(sentence: str, phrase_form: str) -> str` (ValueError, если нет вхождения)
  - `listen_ok(answer: str, sentence: str) -> bool`
  - `classify_incoming(text: str, *, forwarded: bool, has_open_task: bool) -> str` — одно из `"ignore" | "capture" | "giveup" | "clarify" | "answer"`
  - `strip_capture_prefix(text: str) -> str`

- [ ] **Step 1: Падающие тесты**

`tests/test_daily.py` (новый файл):

```python
"""Чистая логика ежедневной практики (daily.py)."""
import random
from datetime import date

import pytest

import daily


@pytest.mark.parametrize("interval, kind", [
    (0, "compose_hinted"), (1, "gap"), (3, "recall"), (7, "listen"), (14, "compose"),
    (2, "gap"), (5, "recall"), (10, "listen"), (20, "compose"),   # округление вниз
])
def test_task_kind_by_rung(interval, kind):
    assert daily.task_kind(interval, random.Random(1)) == kind


def test_task_kind_mixed_from_30_days_is_seeded_choice():
    kinds = {daily.task_kind(30, random.Random(s)) for s in range(50)}
    assert kinds == {"recall", "gap", "listen", "compose"}
    assert daily.task_kind(60, random.Random(3)) in ("recall", "gap", "listen", "compose")
    assert daily.task_kind(30, random.Random(7)) == daily.task_kind(30, random.Random(7))


@pytest.mark.parametrize("missed, last, today, expected", [
    (0, None, date(2026, 10, 5), True),
    (2, "2026-10-04", date(2026, 10, 5), True),
    (3, "2026-10-04", date(2026, 10, 5), False),      # тихий режим
    (3, "2026-09-28", date(2026, 10, 5), True),       # 7 дней прошло
    (3, "2026-09-29", date(2026, 10, 5), False),      # 6 дней
    (5, None, date(2026, 10, 5), True),               # ни разу не слали
])
def test_should_send(missed, last, today, expected):
    assert daily.should_send(missed, last, today) is expected


@pytest.mark.parametrize("kind, has_sentence, has_voice, expected", [
    ("gap", False, True, "recall"),
    ("listen", False, True, "recall"),
    ("listen", True, False, "gap"),
    ("compose_hinted", False, True, "compose_hinted"),
    ("compose_hinted", True, False, "compose_hinted"),
    ("recall", False, False, "recall"),
    ("compose", False, False, "compose"),
    ("gap", True, True, "gap"),
])
def test_downgrade_kind(kind, has_sentence, has_voice, expected):
    assert daily.downgrade_kind(kind, has_sentence=has_sentence, has_voice=has_voice) == expected


def test_blank_out_case_insensitive_first_occurrence():
    assert daily.blank_out("Heads-up: the heads-up came late.", "heads-up") == \
        "___: the heads-up came late."
    assert daily.blank_out("Give me a heads-up.", "a heads-up") == "Give me ___."


def test_blank_out_raises_when_missing():
    with pytest.raises(ValueError):
        daily.blank_out("No phrase here.", "heads-up")
    with pytest.raises(ValueError):
        daily.blank_out("Anything.", "")


@pytest.mark.parametrize("answer, sentence, ok", [
    ("can you give me a heads up before you merge", "Can you give me a heads-up before you merge?", True),
    ("The release slips to Thursady.", "The release slips to Thursday.", True),   # опечатка в слове
    ("the release slips to thursday", "Just the release slips to Thursday.", False),  # выпало слово
    ("I can approve the release", "I can't approve the release.", False),          # can/can't
    ("I can approve the release", "I can’t approve the release.", False),          # типографский апостроф
    ("I cannot approve it", "I can not approve it.", False),                       # известный компромисс
    ("something completely different here", "Just a heads-up, tests are late.", False),
    ("", "Just a heads-up.", False),
])
def test_listen_ok(answer, sentence, ok):
    assert daily.listen_ok(answer, sentence) is ok


@pytest.mark.parametrize("text, forwarded, has_task, expected", [
    ("", False, True, "ignore"),
    ("/stats", False, True, "ignore"),
    ("anything", True, True, "capture"),                       # пересланное — всегда сбор
    ("+ heads-up", False, True, "capture"),                    # явный префикс
    ("hello there", False, False, "capture"),                  # нет задания — сбор
    ("не помню", False, True, "giveup"),
    ("ой, не знаю", False, True, "giveup"),
    ("сказали heads-up на созвоне, не поняла", False, True, "clarify"),  # длинная русская заметка
    ("Just a heads-up, tests aren't ready", False, True, "answer"),
    ("heads-up", False, True, "answer"),
])
def test_classify_incoming(text, forwarded, has_task, expected):
    assert daily.classify_incoming(text, forwarded=forwarded, has_open_task=has_task) == expected


def test_strip_capture_prefix():
    assert daily.strip_capture_prefix("+ heads-up") == "heads-up"
    assert daily.strip_capture_prefix("+heads-up") == "heads-up"
    assert daily.strip_capture_prefix("heads-up") == "heads-up"
    assert daily.strip_capture_prefix("+") == ""
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily.py -q` → FAIL (`ModuleNotFoundError: daily`).

- [ ] **Step 3: Реализация `daily.py` (чистая часть)**

```python
"""Ежедневная практика: чистая логика (эта часть) + IO (добавляется в Task 10–12).

Спека: docs/superpowers/specs/2026-09-30-daily-practice-design.md.
"""
from __future__ import annotations

import difflib
import random
import re
from datetime import date

import intents
from services import srs

KINDS = ("compose_hinted", "gap", "recall", "listen", "compose")
VOICE_KINDS = frozenset({"compose_hinted", "listen"})
MAX_TASKS_PER_DAY = 3
MISSED_QUIET_AFTER = 3
QUIET_PERIOD_DAYS = 7
WORD_RATIO = 0.8
GIVEUP_MAX_WORDS = 4

_KIND_BY_RUNG = {0: "compose_hinted", 1: "gap", 3: "recall", 7: "listen", 14: "compose"}
_MIXED_KINDS = ("recall", "gap", "listen", "compose")
_MIXED_FROM = 30


def task_kind(interval_days: int, rng: random.Random) -> str:
    """Вид задания по ступени лесенки; нестандартный interval округляется вниз."""
    rung = 0
    for r in (0, *srs.LADDER):
        if r <= interval_days:
            rung = r
    if rung >= _MIXED_FROM:
        return rng.choice(_MIXED_KINDS)
    return _KIND_BY_RUNG[rung]


def should_send(missed_streak: int, last_sent_on: str | None, today: date) -> bool:
    """Тихий режим: после 3 пропущенных утренних — раз в 7 дней."""
    if missed_streak < MISSED_QUIET_AFTER or not last_sent_on:
        return True
    return (today - date.fromisoformat(last_sent_on)).days >= QUIET_PERIOD_DAYS


def downgrade_kind(kind: str, *, has_sentence: bool, has_voice: bool) -> str:
    if not has_sentence and kind in ("gap", "listen"):
        return "recall"
    if not has_voice and kind == "listen":
        return "gap"
    return kind


def blank_out(sentence: str, phrase_form: str) -> str:
    if not phrase_form:
        raise ValueError("empty phrase_form")
    idx = sentence.lower().find(phrase_form.lower())
    if idx < 0:
        raise ValueError(f"{phrase_form!r} not in sentence")
    return sentence[:idx] + "___" + sentence[idx + len(phrase_form):]


_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


def _norm_words(text: str) -> list[str]:
    t = text.lower().replace("’", "'").replace("-", " ")
    return _PUNCT.sub("", t).split()


def listen_ok(answer: str, sentence: str) -> bool:
    """Пословно: то же число слов и каждое слово ≥ WORD_RATIO (опечатка — ok, can/can't — нет)."""
    a, b = _norm_words(answer), _norm_words(sentence)
    if not a or len(a) != len(b):
        return False
    return all(x == y or difflib.SequenceMatcher(None, x, y).ratio() >= WORD_RATIO
               for x, y in zip(a, b))


_CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)


def classify_incoming(text: str, *, forwarded: bool, has_open_task: bool) -> str:
    """Что делать со свободным текстом вне режимов (см. спеку, «Хендлеры»)."""
    t = text.strip()
    if not t or t.startswith("/"):
        return "ignore"
    if forwarded or t.startswith("+"):
        return "capture"
    if not has_open_task:
        return "capture"
    if intents.is_giveup(t) and len(t.split()) <= GIVEUP_MAX_WORDS:
        return "giveup"
    if _CYRILLIC.search(t):
        return "clarify"
    return "answer"


def strip_capture_prefix(text: str) -> str:
    t = text.strip()
    return t[1:].strip() if t.startswith("+") else t
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные (146 + 52 параметризованных кейсов ≈ 198 passed).

- [ ] **Step 5: Commit**

```bash
git add daily.py tests/test_daily.py
git commit -m "daily: pure logic — task_kind, quiet mode, downgrade, blank_out, listen_ok, classify_incoming"
```

---

### Task 5: Расписание (`next_fire`/`fire_delay`/`should_start_loop`) и конфиг `DAILY_*`

**Files:**
- Modify: `daily.py`, `config.py`, `.env.example`
- Test: `tests/test_daily.py`, `tests/test_config.py` (дополнить)

**Interfaces:**
- Produces:
  - `daily.next_fire(now: datetime, hour: int, minute: int) -> datetime` (aware, тот же tzinfo)
  - `daily.fire_delay(now: datetime, target: datetime) -> float` (секунды, через `timestamp()`)
  - `daily.should_start_loop(profile, cfg) -> bool` — `profile.daily_practice and cfg.daily_at is not None`
  - `config.Config` + поля `daily_at: datetime.time | None`, `daily_tz: str`, `daily_exclude_ids: set[int]`; `config.MORNING_HOURS = range(5, 14)`.
- Consumes: `languages.LanguageProfile.daily_practice` (появится в Task 6; в тесте — `SimpleNamespace`).

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_daily.py`:

```python
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")


def test_next_fire_today_if_still_ahead():
    now = datetime(2026, 10, 5, 8, 0, tzinfo=MADRID)
    assert daily.next_fire(now, 9, 30) == datetime(2026, 10, 5, 9, 30, tzinfo=MADRID)


def test_next_fire_tomorrow_if_passed():
    now = datetime(2026, 10, 5, 9, 30, tzinfo=MADRID)
    assert daily.next_fire(now, 9, 30) == datetime(2026, 10, 6, 9, 30, tzinfo=MADRID)


def test_fire_delay_is_absolute_across_dst():
    # 2026-03-29 02:00 → 03:00 в Мадриде: до 09:00 следующего дня реально 23 часа
    now = datetime(2026, 3, 28, 9, 0, tzinfo=MADRID)
    target = daily.next_fire(now, 9, 0)
    assert target == datetime(2026, 3, 29, 9, 0, tzinfo=MADRID)
    assert daily.fire_delay(now, target) == 23 * 3600
    assert (target - now).total_seconds() == 24 * 3600  # вот почему не так


def test_fire_delay_never_negative():
    now = datetime(2026, 10, 5, 9, 0, tzinfo=MADRID)
    assert daily.fire_delay(now, now) == 0.0


def test_should_start_loop_needs_both_gates():
    en = SimpleNamespace(daily_practice=True)
    es = SimpleNamespace(daily_practice=False)
    on = SimpleNamespace(daily_at=time(9, 30))
    off = SimpleNamespace(daily_at=None)
    assert daily.should_start_loop(en, on) is True
    assert daily.should_start_loop(en, off) is False
    assert daily.should_start_loop(es, on) is False   # DAILY_AT в маминой .env — ничего
    assert daily.should_start_loop(es, off) is False
```

Дополнить `tests/test_config.py`:

```python
import logging
from datetime import time


def _base_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    for var in ("DAILY_AT", "DAILY_TZ", "DAILY_EXCLUDE_IDS"):
        monkeypatch.delenv(var, raising=False)


def test_daily_off_by_default_and_other_vars_not_validated(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_TZ", "Mars/Olympus")   # мусор не должен мешать выключенной фиче
    cfg = config.load()
    assert cfg.daily_at is None
    assert cfg.daily_exclude_ids == set()


def test_daily_at_parsed_with_defaults(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    cfg = config.load()
    assert cfg.daily_at == time(9, 30)
    assert cfg.daily_tz == "Europe/Madrid"
    assert cfg.daily_exclude_ids == set()


def test_daily_exclude_ids_parsed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    monkeypatch.setenv("DAILY_EXCLUDE_IDS", "5, 6")
    assert config.load().daily_exclude_ids == {5, 6}


@pytest.mark.parametrize("raw", ["9h", "25:00", "09:60", "", "nine", "9:30", "09:3"])
def test_malformed_daily_at_fails_fast(monkeypatch, raw):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", raw)
    if raw == "":
        assert config.load().daily_at is None   # пустая строка = выключено
        return
    with pytest.raises(ValueError, match="DAILY_AT"):
        config.load()


def test_bad_daily_tz_fails_fast_when_enabled(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "09:30")
    monkeypatch.setenv("DAILY_TZ", "Mars/Olympus")
    with pytest.raises(Exception):   # ZoneInfoNotFoundError
        config.load()


def test_evening_daily_at_warns_but_loads(monkeypatch, caplog):
    _base_env(monkeypatch)
    monkeypatch.setenv("DAILY_AT", "20:00")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = config.load()
    assert cfg.daily_at == time(20, 0)
    assert any("DAILY_AT" in r.message for r in caplog.records)
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily.py tests/test_config.py -q` → FAIL.

- [ ] **Step 3: Реализация — `daily.py`**

Добавить импорты и функции:

```python
from datetime import date, datetime, timedelta


def next_fire(now: datetime, hour: int, minute: int) -> datetime:
    """Ближайший hour:minute в зоне now — сегодня или завтра, свежим aware datetime."""
    tz = now.tzinfo
    target = datetime(now.year, now.month, now.day, hour, minute, tzinfo=tz)
    if target <= now:
        d = now.date() + timedelta(days=1)
        target = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
    return target


def fire_delay(now: datetime, target: datetime) -> float:
    """Секунды до target в АБСОЛЮТНОМ времени (вычитание aware-дат идёт по стенке, DST-грабля)."""
    return max(0.0, target.timestamp() - now.timestamp())


def should_start_loop(profile, cfg) -> bool:
    """Оба гейта: профиль с daily_practice И DAILY_AT в .env."""
    return bool(getattr(profile, "daily_practice", False)) and cfg.daily_at is not None
```

- [ ] **Step 4: Реализация — `config.py`**

```python
import logging
import re
from dataclasses import dataclass, field   # заменяет прежний `from dataclasses import dataclass`
from datetime import time
from zoneinfo import ZoneInfo

MORNING_HOURS = range(5, 14)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    allowed_user_ids: set[int]
    db_path: str
    bot_lang: str
    daily_at: time | None = None
    daily_tz: str = "Europe/Madrid"
    daily_exclude_ids: set[int] = field(default_factory=set)


def _parse_ids(raw: str) -> set[int]:
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


_HHMM = re.compile(r"^(\d{2}):(\d{2})$")


def _parse_hhmm(raw: str) -> time:
    m = _HHMM.match(raw.strip())
    if not m:
        raise ValueError(f"DAILY_AT must be HH:MM, got {raw!r}")
    try:
        value = time(int(m.group(1)), int(m.group(2)))
    except ValueError as exc:
        raise ValueError(f"DAILY_AT must be HH:MM, got {raw!r}") from exc
    if value.hour not in MORNING_HOURS:
        log.warning("DAILY_AT=%s вне утреннего окна 05:00–13:59 — намеренно?", raw)
    return value
```

В `load()`:

```python
    raw_daily_at = os.environ.get("DAILY_AT") or ""
    daily_at = _parse_hhmm(raw_daily_at) if raw_daily_at else None
    daily_tz = "Europe/Madrid"
    daily_exclude: set[int] = set()
    if daily_at is not None:
        daily_tz = os.environ.get("DAILY_TZ") or "Europe/Madrid"
        ZoneInfo(daily_tz)  # ZoneInfoNotFoundError — громко на старте
        daily_exclude = _parse_ids(os.environ.get("DAILY_EXCLUDE_IDS", ""))
    return Config(
        ...  # существующие поля без изменений
        daily_at=daily_at,
        daily_tz=daily_tz,
        daily_exclude_ids=daily_exclude,
    )
```

`ALLOWED_USER_IDS` тоже переводится на `_parse_ids(raw_ids)` (поведение то же).

`.env.example` — добавить в конец:

```
# Ежедневная практика (только BOT_LANG=en). Нет переменной — выключено.
# DAILY_AT=09:30
# DAILY_TZ=Europe/Madrid
# DAILY_EXCLUDE_IDS=
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные; `.venv/bin/python -c "import bot"` ок.

- [ ] **Step 6: Commit**

```bash
git add daily.py config.py .env.example tests/test_daily.py tests/test_config.py
git commit -m "daily: schedule helpers (next_fire/fire_delay, DST-safe) + DAILY_AT/TZ/EXCLUDE config"
```

---

### Task 6: Языковой профиль — флаг `daily_practice` и три промпта EN

**Files:**
- Modify: `languages.py`
- Test: `tests/test_languages.py` (дополнить)

**Interfaces:**
- Produces: новые поля `LanguageProfile` **с дефолтами** (ES их не задаёт):
  `daily_practice: bool = False`, `sentence_system: str = ""`, `sentence_schema: dict | None = None`,
  `sentence_user_template: str = ""` (`.format(word=, translation=, context=, kind=, avoid=)`),
  `sentence_check_system: str = ""`, `sentence_check_schema: dict | None = None`,
  `sentence_check_user_template: str = ""` (`.format(word=, translation=, context=, answer=, avoid=)`),
  `capture_system: str = ""`, `capture_schema: dict | None = None`.
- Схемы (ключи — контракт для Task 7–8):
  - `sentence_schema`: OBJECT `{sentence, sentence_ru, phrase_form}` (все required, STRING)
  - `sentence_check_schema`: OBJECT `{verdict ∈ good|fix|off, corrected, note, reply_sentence, reply_sentence_ru, reply_phrase_form}` (все required)
  - `capture_schema`: OBJECT `{items: ARRAY of OBJECT {kind ∈ word|phrase, word, translation, transcription, example, example_translation, context, usage}}`, required `["items"]`; у элемента required всё, кроме `usage`.

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_languages.py`:

```python
def _schema_ok(schema):
    assert schema["type"] == "OBJECT"
    assert set(schema["required"]) <= set(schema["properties"])


def test_es_profile_has_no_daily_practice():
    es = PROFILES["es"]
    assert es.daily_practice is False
    assert es.sentence_system == "" and es.sentence_schema is None
    assert es.capture_system == "" and es.capture_schema is None


def test_en_daily_prompts_and_schemas():
    en = PROFILES["en"]
    assert en.daily_practice is True
    for text in (en.sentence_system, en.sentence_check_system, en.capture_system,
                 en.sentence_user_template, en.sentence_check_user_template):
        assert text.strip()
    _schema_ok(en.sentence_schema)
    assert set(en.sentence_schema["required"]) == {"sentence", "sentence_ru", "phrase_form"}
    _schema_ok(en.sentence_check_schema)
    assert en.sentence_check_schema["properties"]["verdict"]["enum"] == ["good", "fix", "off"]
    assert {"reply_sentence", "reply_phrase_form"} <= set(en.sentence_check_schema["required"])


def test_en_capture_schema_is_object_with_items_array():
    en = PROFILES["en"]
    _schema_ok(en.capture_schema)
    assert en.capture_schema["required"] == ["items"]
    items = en.capture_schema["properties"]["items"]
    assert items["type"] == "ARRAY"
    item = items["items"]
    assert item["type"] == "OBJECT"
    assert {"word", "translation", "transcription", "example",
            "example_translation", "context"} <= set(item["required"])
    assert "usage" in item["properties"] and "usage" not in item["required"]


def test_en_templates_format_with_expected_keys():
    en = PROFILES["en"]
    s = en.sentence_user_template.format(word="a heads-up", translation="п", context="к",
                                         kind="gap", avoid="—")
    assert "a heads-up" in s and "gap" in s
    c = en.sentence_check_user_template.format(word="a heads-up", translation="п", context="к",
                                               answer="I gave a heads-up.", avoid="—")
    assert "I gave a heads-up." in c


def test_daily_prompts_are_gender_neutral():
    en = PROFILES["en"]
    for text in (en.sentence_system, en.sentence_check_system, en.capture_system):
        assert "Пол ученика неизвестен" in text
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_languages.py -q` → FAIL (`no attribute 'daily_practice'`).

- [ ] **Step 3: Реализация**

В `LanguageProfile` (после `translate_question`, все с дефолтами — ES не трогаем):

```python
    translate_question: str  # шаблон с {}
    # --- ежедневная практика (только en; у es дефолты) ---
    daily_practice: bool = False
    sentence_system: str = ""
    sentence_schema: dict | None = None
    # .format(word=…, translation=…, context=…, kind=…, avoid=…)
    sentence_user_template: str = ""
    sentence_check_system: str = ""
    sentence_check_schema: dict | None = None
    # .format(word=…, translation=…, context=…, answer=…, avoid=…)
    sentence_check_user_template: str = ""
    capture_system: str = ""
    capture_schema: dict | None = None
```

Константы-схемы (перед `EN = …`):

```python
_EN_SENTENCE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sentence": {"type": "STRING"},
        "sentence_ru": {"type": "STRING"},
        "phrase_form": {"type": "STRING"},
    },
    "required": ["sentence", "sentence_ru", "phrase_form"],
}

_EN_SENTENCE_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["good", "fix", "off"]},
        "corrected": {"type": "STRING"},
        "note": {"type": "STRING"},
        "reply_sentence": {"type": "STRING"},
        "reply_sentence_ru": {"type": "STRING"},
        "reply_phrase_form": {"type": "STRING"},
    },
    "required": ["verdict", "corrected", "note", "reply_sentence",
                 "reply_sentence_ru", "reply_phrase_form"],
}

_EN_CAPTURE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING", "enum": ["word", "phrase"]},
                    "word": {"type": "STRING"},
                    "translation": {"type": "STRING"},
                    "transcription": {"type": "STRING"},
                    "example": {"type": "STRING"},
                    "example_translation": {"type": "STRING"},
                    "context": {"type": "STRING"},
                    "usage": {"type": "STRING"},
                },
                "required": ["kind", "word", "translation", "transcription",
                             "example", "example_translation", "context"],
            },
        },
    },
    "required": ["items"],
}
```

Поля в `EN = LanguageProfile(...)` (добавить после `translate_question=...`):

```python
    daily_practice=True,
    sentence_system=(
        "Ты пишешь ОДНО предложение на американском английском уровня B2 для "
        "русскоязычного ученика, который учит фразу. Тебе дают фразу, её перевод, "
        "контекст, откуда она пришла (тема), вид задания и список предложений, "
        "которые НЕЛЬЗЯ повторять. Правила: предложение живое и естественное, "
        "8–16 слов, фраза употреблена в нём точно и уместно; тема предложения — "
        "из указанного контекста (если контекст — это пример-предложение, держись "
        "его темы), не абстрактная и не «про кота»; для вида задания gap фраза "
        "должна стоять в предложении дословно, чтобы её можно было вырезать. "
        "Верни: sentence — предложение; sentence_ru — его естественный русский "
        "перевод; phrase_form — ТОЧНАЯ подстрока sentence, в которой употреблена "
        "фраза (с теми же буквами и формой слов, как в sentence). Не повторяй "
        "предложения из списка «не повторять» и не делай их перефразом. "
        "Пол ученика неизвестен — без гендерных форм в его адрес."
    ),
    sentence_schema=_EN_SENTENCE_SCHEMA,
    sentence_user_template=(
        "Фраза: {word}\n"
        "Перевод: {translation}\n"
        "Контекст (тема): {context}\n"
        "Вид задания: {kind}\n"
        "Не повторять:\n{avoid}"
    ),
    sentence_check_system=(
        "Ты мягко проверяешь предложение, которое русскоязычный ученик уровня B2 "
        "сам составил на американском английском с заданной фразой. Тебе дают "
        "фразу, её перевод, контекст и предложение ученика. Оцени verdict: 'good' "
        "— предложение естественное и фраза употреблена верно; 'fix' — фраза на "
        "месте, но есть грамматическая/лексическая ошибка или неестественность; "
        "'off' — фраза не использована, использована в другом смысле или "
        "предложение не на английском. В corrected — исправленный вариант "
        "предложения ученика (для 'good' повтори его как есть). В note — короткая "
        "ДОБАВЛЯЮЩАЯ подсказка по-русски: для 'fix'/'off' — что именно не так "
        "(«нужен артикль», «после … идёт герундий»); для 'good' — крошечный факт "
        "или ободрение. НЕ дублируй вердикт словами «верно», «почти». Затем "
        "напиши ответное предложение: reply_sentence — НОВОЕ предложение с той же "
        "фразой, в теме контекста, не повторяющее ни предложение ученика, ни "
        "список «не повторять»; reply_sentence_ru — его русский перевод; "
        "reply_phrase_form — ТОЧНАЯ подстрока reply_sentence с фразой. "
        "Пол ученика неизвестен — без гендерных форм в его адрес "
        "(не «написала», «умница»)."
    ),
    sentence_check_schema=_EN_SENTENCE_CHECK_SCHEMA,
    sentence_check_user_template=(
        "Фраза: {word}\n"
        "Перевод: {translation}\n"
        "Контекст (тема): {context}\n"
        "Предложение ученика: {answer}\n"
        "Не повторять:\n{avoid}"
    ),
    capture_system=(
        "Ты помогаешь русскоязычному ученику уровня B2 собирать английские фразы "
        "из его реальной жизни. На вход — свободный текст: пересланное сообщение, "
        "кусок переписки, заметка после созвона, возможно с пояснением по-русски. "
        "Задача — вернуть items: от 0 до 3 фраз, которые стоит выучить. Если в "
        "тексте есть явный указатель (одиночное слово/фраза, кавычки, «не поняла "
        "X», «что значит X») — верни РОВНО эту фразу, одну. Иначе выбери до 3 "
        "самых полезных для B2 кусков: коллокации, фразовые глаголы, устойчивые "
        "обороты — НЕ одиночные частотные слова. Для каждого: kind ('word' или "
        "'phrase'); word — фраза в словарной форме на американском английском "
        "(a heads-up, give someone a heads-up); translation — русский перевод; "
        "transcription — IPA в слэшах, General American; example — пример-"
        "предложение B2+ с этой фразой; example_translation — его перевод; "
        "context — ДО 60 символов по-русски, откуда/о чём была фраза, строго из "
        "текста, без выдумок (например «рабочий созвон, перенос релиза»); если "
        "источник не назван — тема самого сообщения; context не может быть "
        "пустым. usage — короткая строка «обычно: …» с типичным употреблением "
        "(можно пустую). Если учить нечего — items пустой. "
        "Пол ученика неизвестен — без гендерных форм в его адрес."
    ),
    capture_schema=_EN_CAPTURE_SCHEMA,
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные, включая старый байт-инвариант ES.

- [ ] **Step 5: Commit**

```bash
git add languages.py tests/test_languages.py
git commit -m "languages: daily_practice flag + EN prompts/schemas for sentences, sentence check, capture"
```

---

### Task 7: `services/sentences.py` — генерация и проверка предложений

**Files:**
- Create: `services/sentences.py`
- Test: `tests/test_sentences.py` (новый)

**Interfaces:**
- Consumes: `services.llm.generate_json(llm, *, system, schema, text, max_output_tokens) -> dict | None`, `services.llm.QuotaExceededError`, поля EN-профиля из Task 6, `formatting.field`.
- Produces:
  - `class SentenceError(Exception)`
  - `contains(haystack: str, needle: str) -> bool` — регистронезависимое вхождение, `False` для пустого needle
  - `make_sentence(llm, profile, card, kind: str, avoid: list[str]) -> dict` → `{"sentence", "sentence_ru", "phrase_form"}`; до 2 попыток; `SentenceError` при провале; `QuotaExceededError` пробрасывается
  - `fallback_sentence(card) -> dict | None` — пример карточки, если `example`/`example_translation` непустые и `card.word` входит в `example`; `phrase_form = card.word`
  - `check_sentence(llm, profile, card, answer: str, avoid: list[str]) -> dict` → `{"verdict", "corrected", "note", "reply_sentence", "reply_sentence_ru"}`; `reply_*` могут быть `None`; verdict первого валидного ответа фиксируется; при невалидном ответном предложении — один `make_sentence(kind="reply")`, потом `fallback_sentence`, потом `None`.
  - Контекст для промпта: `card.context` или `"пример: " + card.example`, если контекста нет (`_topic(card)`).

- [ ] **Step 1: Падающие тесты**

`tests/test_sentences.py`:

```python
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors

from languages import PROFILES
from services import llm, sentences

EN = PROFILES["en"]
CARD = {"id": 1, "word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up: tests are late.",
        "example_translation": "Предупреждаю: тесты опаздывают.",
        "context": "рабочий созвон, релиз"}


def _llm(client):
    return llm.LLM(client=client, models=("flash",))


def _resp(payload):
    return SimpleNamespace(text=json.dumps(payload))


GOOD = {"sentence": "Can you give me a heads-up before you merge?",
        "sentence_ru": "Предупредишь меня перед мержем?", "phrase_form": "a heads-up"}


def test_make_sentence_returns_validated_fields():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(GOOD)
    out = sentences.make_sentence(_llm(client), EN, CARD, "gap", ["Old one."])
    assert out == GOOD
    cfg = client.models.generate_content.call_args.kwargs["config"]
    assert cfg.system_instruction == EN.sentence_system
    assert cfg.response_schema == EN.sentence_schema
    sent = client.models.generate_content.call_args.kwargs["contents"]
    assert "a heads-up" in sent and "gap" in sent and "Old one." in sent
    assert "рабочий созвон, релиз" in sent


def test_make_sentence_uses_example_as_topic_when_no_context():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(GOOD)
    sentences.make_sentence(_llm(client), EN, {**CARD, "context": None}, "recall", [])
    sent = client.models.generate_content.call_args.kwargs["contents"]
    assert "пример: Just a heads-up: tests are late." in sent


def test_make_sentence_retries_when_phrase_form_not_in_sentence():
    bad = {**GOOD, "phrase_form": "heads up please"}
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp(bad), _resp(GOOD)]
    assert sentences.make_sentence(_llm(client), EN, CARD, "gap", []) == GOOD
    assert client.models.generate_content.call_count == 2


def test_make_sentence_raises_after_two_bad_answers():
    client = MagicMock()
    client.models.generate_content.side_effect = [SimpleNamespace(text="x"),
                                                  _resp({"sentence": "", "sentence_ru": "", "phrase_form": ""})]
    with pytest.raises(sentences.SentenceError):
        sentences.make_sentence(_llm(client), EN, CARD, "gap", [])


def test_make_sentence_propagates_quota():
    client = MagicMock()
    client.models.generate_content.side_effect = [errors.ClientError(429, {"message": "quota"})]
    with pytest.raises(llm.QuotaExceededError):
        sentences.make_sentence(_llm(client), EN, CARD, "gap", [])


def test_fallback_sentence_requires_word_in_example():
    assert sentences.fallback_sentence(CARD) == {
        "sentence": CARD["example"], "sentence_ru": CARD["example_translation"],
        "phrase_form": "a heads-up"}
    assert sentences.fallback_sentence({**CARD, "example": "No phrase here."}) is None
    assert sentences.fallback_sentence({**CARD, "example_translation": None}) is None
    assert sentences.fallback_sentence({**CARD, "example": ""}) is None


CHECK = {"verdict": "fix", "corrected": "I gave the team a heads-up.",
         "note": "нужен артикль", "reply_sentence": "Thanks for the heads-up!",
         "reply_sentence_ru": "Спасибо, что предупредили!", "reply_phrase_form": "heads-up"}


def test_check_sentence_returns_verdict_and_reply():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(CHECK)
    out = sentences.check_sentence(_llm(client), EN, CARD, "I gave team a heads-up.", ["Old."])
    assert out == {"verdict": "fix", "corrected": "I gave the team a heads-up.",
                   "note": "нужен артикль", "reply_sentence": "Thanks for the heads-up!",
                   "reply_sentence_ru": "Спасибо, что предупредили!"}
    sent = client.models.generate_content.call_args.kwargs["contents"]
    assert "I gave team a heads-up." in sent and "Old." in sent


def test_check_sentence_keeps_first_verdict_and_regenerates_bad_reply():
    bad_reply = {**CHECK, "verdict": "good", "reply_phrase_form": "not there"}
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp(bad_reply), _resp(GOOD)]
    out = sentences.check_sentence(_llm(client), EN, CARD, "I gave a heads-up.", [])
    assert out["verdict"] == "good"                       # зафиксирован первый
    assert out["reply_sentence"] == GOOD["sentence"]      # ответ — из make_sentence
    assert client.models.generate_content.call_count == 2
    cfg2 = client.models.generate_content.call_args.kwargs["config"]
    assert cfg2.response_schema == EN.sentence_schema     # второй вызов — НЕ check


def test_check_sentence_keeps_verdict_when_reply_regeneration_crashes():
    bad_reply = {**CHECK, "reply_phrase_form": "nope"}
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp(bad_reply),
                                                  errors.ClientError(400, {"message": "bad request"})]
    out = sentences.check_sentence(_llm(client), EN, CARD, "I gave a heads-up.", [])
    assert out["verdict"] == "fix" and out["reply_sentence"] == CARD["example"]


def test_check_sentence_falls_back_to_example_then_none_for_reply():
    bad_reply = {**CHECK, "reply_phrase_form": "nope"}
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp(bad_reply), SimpleNamespace(text="x"),
                                                  SimpleNamespace(text="x")]
    out = sentences.check_sentence(_llm(client), EN, CARD, "I gave a heads-up.", [])
    assert out["verdict"] == "fix"
    assert out["reply_sentence"] == CARD["example"]
    client.models.generate_content.side_effect = [_resp(bad_reply), SimpleNamespace(text="x"),
                                                  SimpleNamespace(text="x")]
    out = sentences.check_sentence(_llm(client), EN, {**CARD, "example": "none"},
                                   "I gave a heads-up.", [])
    assert out["reply_sentence"] is None


def test_check_sentence_raises_on_two_invalid_verdicts():
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp({**CHECK, "verdict": "meh"}),
                                                  SimpleNamespace(text="x")]
    with pytest.raises(sentences.SentenceError):
        sentences.check_sentence(_llm(client), EN, CARD, "x", [])


def test_check_sentence_fix_requires_corrected():
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp({**CHECK, "corrected": ""}),
                                                  _resp(CHECK)]
    out = sentences.check_sentence(_llm(client), EN, CARD, "x", [])
    assert out["corrected"] == "I gave the team a heads-up."
    assert client.models.generate_content.call_count == 2


def test_check_sentence_propagates_quota():
    client = MagicMock()
    client.models.generate_content.side_effect = [errors.ClientError(429, {"message": "quota"})]
    with pytest.raises(llm.QuotaExceededError):
        sentences.check_sentence(_llm(client), EN, CARD, "x", [])
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_sentences.py -q` → FAIL (`cannot import name 'sentences'`).

- [ ] **Step 3: Реализация**

`services/sentences.py`:

```python
"""Предложения для ежедневной практики: новое предложение с фразой и проверка
своего предложения ученика. По образцу services/grading.py: generate_json + до
двух попыток + валидация. Фолбэк на пример карточки — у вызывающего через
fallback_sentence()."""
from __future__ import annotations

import logging

from formatting import field
from languages import LanguageProfile
from services import llm as llm_service

log = logging.getLogger(__name__)
SENTENCE_KEYS = ("sentence", "sentence_ru", "phrase_form")
CHECK_VERDICTS = ("good", "fix", "off")


class SentenceError(Exception):
    pass


def contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle.lower() in (haystack or "").lower()


def _topic(card) -> str:
    return field(card, "context") or f"пример: {field(card, 'example') or ''}"


def _avoid_text(avoid: list[str]) -> str:
    return "\n".join(f"- {s}" for s in avoid) if avoid else "—"


def make_sentence(llm: llm_service.LLM, profile: LanguageProfile, card, kind: str,
                  avoid: list[str]) -> dict:
    user = profile.sentence_user_template.format(
        word=card["word"], translation=card["translation"], context=_topic(card),
        kind=kind, avoid=_avoid_text(avoid))
    for _ in range(2):
        data = llm_service.generate_json(
            llm, system=profile.sentence_system, schema=profile.sentence_schema,
            text=user, max_output_tokens=256)
        if (data is not None and all(data.get(k) for k in SENTENCE_KEYS)
                and contains(data["sentence"], data["phrase_form"])):
            return {k: data[k] for k in SENTENCE_KEYS}
    raise SentenceError("модель не дала предложение с фразой")


def fallback_sentence(card) -> dict | None:
    example = field(card, "example")
    example_ru = field(card, "example_translation")
    if example and example_ru and contains(example, card["word"]):
        return {"sentence": example, "sentence_ru": example_ru, "phrase_form": card["word"]}
    return None


def check_sentence(llm: llm_service.LLM, profile: LanguageProfile, card, answer: str,
                   avoid: list[str]) -> dict:
    user = profile.sentence_check_user_template.format(
        word=card["word"], translation=card["translation"], context=_topic(card),
        answer=answer, avoid=_avoid_text(avoid))
    data = None
    for _ in range(2):
        candidate = llm_service.generate_json(
            llm, system=profile.sentence_check_system,
            schema=profile.sentence_check_schema, text=user, max_output_tokens=512)
        if (candidate is not None and candidate.get("verdict") in CHECK_VERDICTS
                and candidate.get("note")
                and (candidate["verdict"] != "fix" or candidate.get("corrected"))):
            data = candidate
            break
    if data is None:
        raise SentenceError("модель не дала валидную оценку предложения")

    result = {
        "verdict": data["verdict"],
        "corrected": data.get("corrected") or "",
        "note": data["note"],
        "reply_sentence": None,
        "reply_sentence_ru": None,
    }
    reply, reply_form = data.get("reply_sentence"), data.get("reply_phrase_form")
    if reply and contains(reply, reply_form):
        result["reply_sentence"] = reply
        result["reply_sentence_ru"] = data.get("reply_sentence_ru") or ""
        return result
    # Вердикт зафиксирован; ответное предложение добираем отдельно, check не повторяем.
    try:
        fresh = make_sentence(llm, profile, card, "reply", [*avoid, answer])
        result["reply_sentence"], result["reply_sentence_ru"] = fresh["sentence"], fresh["sentence_ru"]
    except Exception as exc:   # noqa: BLE001 — вердикт уже есть, ответное предложение — best-effort
        log.warning("reply sentence regeneration failed: %s", exc)
        fb = fallback_sentence(card)
        if fb is not None:
            result["reply_sentence"], result["reply_sentence_ru"] = fb["sentence"], fb["sentence_ru"]
    return result
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные.

- [ ] **Step 5: Commit**

```bash
git add services/sentences.py tests/test_sentences.py
git commit -m "services: sentences.make_sentence/check_sentence with validation and example fallback"
```

---

### Task 8: `services/capture.py` — извлечение фраз из свободного текста

**Files:**
- Create: `services/capture.py`
- Test: `tests/test_capture.py` (новый)

**Interfaces:**
- Consumes: `services.llm.generate_json`, `profile.capture_system/capture_schema`.
- Produces: `class CaptureError(Exception)`; `MAX_ITEMS = 3`, `MAX_TEXT = 2000`, `MAX_CONTEXT = 60`; `extract(llm, profile, text: str) -> list[dict]` — каждый элемент `{kind, word, translation, transcription, example, example_translation, context, usage}`; `context` всегда непустой и ≤ 60 символов (ответ модели обрезается; подстановка `default_context(text)` = `из сообщения: «<первые 40 символов>»`, ≤ 58 символов); `usage` — строка, возможно пустая; пустой `items` → `[]`; до 2 попыток, потом `CaptureError`; `QuotaExceededError` пробрасывается.

- [ ] **Step 1: Падающие тесты**

`tests/test_capture.py`:

```python
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors

from languages import PROFILES
from services import capture, llm

EN = PROFILES["en"]


def _llm(client):
    return llm.LLM(client=client, models=("flash",))


def _resp(payload):
    return SimpleNamespace(text=json.dumps(payload))


ITEM = {"kind": "phrase", "word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up.",
        "example_translation": "Просто предупреждаю.", "context": "созвон, релиз",
        "usage": "обычно: give someone a heads-up"}


def test_extract_returns_items_with_context_and_usage():
    client = MagicMock()
    client.models.generate_content.return_value = _resp({"items": [ITEM]})
    out = capture.extract(_llm(client), EN, "just a heads-up, release slips — не поняла heads-up")
    assert out == [ITEM]
    cfg = client.models.generate_content.call_args.kwargs["config"]
    assert cfg.system_instruction == EN.capture_system
    assert cfg.response_schema == EN.capture_schema


def test_extract_caps_at_three_and_skips_incomplete():
    items = [{**ITEM, "word": f"w{i}"} for i in range(5)]
    items.insert(1, {"kind": "word", "word": "broken"})   # без обязательных полей
    client = MagicMock()
    client.models.generate_content.return_value = _resp({"items": items})
    out = capture.extract(_llm(client), EN, "text")
    assert [i["word"] for i in out] == ["w0", "w1", "w2"]


def test_extract_fills_empty_context_and_usage():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(
        {"items": [{**ITEM, "context": "  ", "usage": None}]})
    long_text = "x" * 80
    out = capture.extract(_llm(client), EN, long_text)
    assert out[0]["context"] == "из сообщения: «" + "x" * 40 + "»"
    assert len(out[0]["context"]) <= capture.MAX_CONTEXT
    assert out[0]["usage"] == ""


def test_extract_truncates_long_model_context():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(
        {"items": [{**ITEM, "context": "к" * 100}]})
    out = capture.extract(_llm(client), EN, "text")
    assert out[0]["context"] == "к" * 60


def test_extract_empty_items_is_empty_list():
    client = MagicMock()
    client.models.generate_content.return_value = _resp({"items": []})
    assert capture.extract(_llm(client), EN, "привет") == []


def test_extract_truncates_long_text():
    client = MagicMock()
    client.models.generate_content.return_value = _resp({"items": []})
    capture.extract(_llm(client), EN, "a" * 5000)
    sent = client.models.generate_content.call_args.kwargs["contents"]
    assert len(sent) == capture.MAX_TEXT


def test_extract_retries_then_raises():
    client = MagicMock()
    client.models.generate_content.side_effect = [SimpleNamespace(text="x"),
                                                  _resp({"nope": 1})]
    with pytest.raises(capture.CaptureError):
        capture.extract(_llm(client), EN, "text")
    assert client.models.generate_content.call_count == 2


def test_extract_propagates_quota():
    client = MagicMock()
    client.models.generate_content.side_effect = [errors.ClientError(429, {"message": "q"})]
    with pytest.raises(llm.QuotaExceededError):
        capture.extract(_llm(client), EN, "text")
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_capture.py -q` → FAIL.

- [ ] **Step 3: Реализация**

`services/capture.py`:

```python
"""Сбор фраз из свободного текста (пересланное сообщение / заметка после созвона).

Схема — объект {"items": [...]}: generate_json отдаёт только dict."""
from __future__ import annotations

from languages import LanguageProfile
from services import llm as llm_service

MAX_ITEMS = 3
MAX_TEXT = 2000
MAX_CONTEXT = 60
ITEM_KEYS = ("kind", "word", "translation", "transcription", "example",
             "example_translation")


class CaptureError(Exception):
    pass


def default_context(text: str) -> str:
    return f"из сообщения: «{text.strip()[:40]}»"   # ≤ 58 символов


def extract(llm: llm_service.LLM, profile: LanguageProfile, text: str) -> list[dict]:
    text = text.strip()[:MAX_TEXT]
    for _ in range(2):
        data = llm_service.generate_json(
            llm, system=profile.capture_system, schema=profile.capture_schema,
            text=text, max_output_tokens=1024)
        if data is None or not isinstance(data.get("items"), list):
            continue
        items: list[dict] = []
        for raw in data["items"]:
            if not isinstance(raw, dict) or not all(raw.get(k) for k in ITEM_KEYS):
                continue
            item = {k: raw[k] for k in ITEM_KEYS}
            item["context"] = ((raw.get("context") or "").strip() or default_context(text))[:MAX_CONTEXT]
            item["usage"] = (raw.get("usage") or "").strip()
            items.append(item)
            if len(items) == MAX_ITEMS:
                break
        return items
    raise CaptureError("модель не вернула список фраз")
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные.

- [ ] **Step 5: Commit**

```bash
git add services/capture.py tests/test_capture.py
git commit -m "services: capture.extract — up to 3 phrases with mandatory context from free text"
```

---

### Task 9: `voice.py` — отправка готовой озвучки и озвучка слова с подписью

**Files:**
- Modify: `voice.py`
- Test: `tests/test_voice.py` (новый)

**Interfaces:**
- Consumes: `services.tts.synthesize`, `db.set_audio_file_id`.
- Produces:
  - `send_text_voice(bot, chat_id: int, mp3_path: str, caption: str | None = None, parse_mode: str | None = None, reply_markup=None) -> Message` — отправляет готовый файл через `bot.send_voice`, исключения Telegram пробрасывает, файл НЕ удаляет (владелец — вызывающий).
  - `send_card_voice_to(bot, chat_id, conn, card, voice, caption=None, parse_mode=None, reply_markup=None) -> Message | None` — кэш по `audio_file_id`, иначе синтез + кэширование; best-effort (возвращает `None` при `TTSError/OSError/TelegramBadRequest`).
  - `send_card_voice(message, conn, card, voice, caption=None, parse_mode=None, reply_markup=None)` — существующая сигнатура + необязательные параметры; делегирует в `send_card_voice_to(message.bot, message.chat.id, …)`.
  - `reply_markup` нужен Task 12: кнопка «Ещё одно» едет на голосовом сообщении результата.

- [ ] **Step 1: Падающие тесты**

`tests/test_voice.py`:

```python
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
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_voice.py -q` → FAIL (`no attribute 'send_text_voice'`).

- [ ] **Step 3: Реализация `voice.py`** (файл целиком)

```python
from __future__ import annotations

import os
import sqlite3
import tempfile

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message

import db
from services import tts


async def send_text_voice(bot, chat_id: int, mp3_path: str, caption: str | None = None,
                          parse_mode: str | None = None, reply_markup=None) -> Message:
    """Отправить ГОТОВЫЙ mp3 голосовым (sendVoice принимает MP3 с Bot API 7.2).

    Без кэша и без синтеза: каждое предложение новое, синтез делает вызывающий,
    он же удаляет файл. Ошибки Telegram пробрасываются — решение о деградации
    принимает вызывающий.
    """
    with open(mp3_path, "rb") as fh:
        return await bot.send_voice(
            chat_id, BufferedInputFile(fh.read(), filename="произношение.mp3"),
            caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_card_voice_to(bot, chat_id: int, conn: sqlite3.Connection, card, voice: str,
                             caption: str | None = None, parse_mode: str | None = None,
                             reply_markup=None) -> Message | None:
    """Озвучка слова карточки: кэш по file_id, иначе синтез + кэширование.

    Голосовое, не audio: voice-пузыри не склеиваются клиентом в плейлист.
    file_id привязан к типу сообщения — при смене voice↔audio кэш сбрасывать.
    Best-effort: при сбое возвращает None (текст уже показан).
    """
    if card["audio_file_id"]:
        try:
            return await bot.send_voice(chat_id, card["audio_file_id"], caption=caption,
                                        parse_mode=parse_mode, reply_markup=reply_markup)
        except TelegramBadRequest:
            return None   # протухший file_id — best-effort, как и раньше
    tmp = os.path.join(tempfile.gettempdir(), f"tts_{os.getpid()}_{card['id']}.mp3")
    try:
        await tts.synthesize(card["word"], voice, tmp)
        sent = await send_text_voice(bot, chat_id, tmp, caption=caption, parse_mode=parse_mode,
                                     reply_markup=reply_markup)
        db.set_audio_file_id(conn, card["id"], sent.voice.file_id)
        return sent
    except (tts.TTSError, OSError, TelegramBadRequest):
        return None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def send_card_voice(message: Message, conn: sqlite3.Connection, card, voice: str,
                          caption: str | None = None, parse_mode: str | None = None,
                          reply_markup=None) -> Message | None:
    """Прежняя сигнатура (menu/training/add) + необязательные подпись/клавиатура."""
    return await send_card_voice_to(message.bot, message.chat.id, conn, card, voice,
                                    caption=caption, parse_mode=parse_mode,
                                    reply_markup=reply_markup)
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные; `.venv/bin/python -c "import bot"` ок (старые вызовы `send_card_voice(message, conn, card, voice)` совместимы).

- [ ] **Step 5: Commit**

```bash
git add voice.py tests/test_voice.py
git commit -m "voice: send_text_voice (ready mp3, caption) + send_card_voice_to with cached file_id"
```

---

### Task 10: `daily.py` — тексты заданий и результатов (чистый рендер)

**Files:**
- Modify: `daily.py`
- Test: `tests/test_daily.py` (дополнить)

**Interfaces:**
- Consumes: `formatting.field`, `html.escape`, `blank_out`.
- Produces (в `daily.py`, все возвращают HTML-строку для `parse_mode="HTML"`):
  - `card_block(card) -> str` — фраза жирным, перевод, IPA, контекст (без примера)
  - `render_task(kind, card, sentence, sentence_ru, phrase_form, *, voice_ok: bool) -> str`
  - `render_compose_result(check: dict | None) -> str` — `check=None` означает «квота: принято без разбора»
  - `render_grade_result(verdict: dict | None, *, exact: bool, expected: str, quota: bool) -> str` — для recall/gap (`verdict` из `grading.grade`: `{verdict, correct, note}`)
  - `render_listen_result(ok: bool, sentence: str) -> str`
  - `render_giveup(card, sentence: str | None) -> str`
  - `with_sentence(text: str, sentence: str | None) -> str` — добавляет `\n\n🔊 <i>{sentence}</i>`
  - `CAPTION_LIMIT = 1024`, `fit_caption(text: str) -> str` — подпись голосового ≤ 1024 символов гарантированно: короткий текст — как есть; длинный — теги снимаются, сущности раскрываются, текст режется и заново экранируется (деградация в plain text без разорванных тегов/сущностей), в конце «…»
  - `TEXT_NOTHING_NEXT`, `TEXT_MORE_STALE`, `TEXT_MORE_LIMIT`, `TEXT_MORE_EMPTY`, `TEXT_CARD_DELETED`, `TEXT_GRADE_FAILED`, `TEXT_SAVED`, `TEXT_ONLY_TEXT`, `TEXT_NOTHING_FOUND`, `TEXT_CLARIFY`, `TEXT_CLARIFY_STALE`, `TEXT_EMPTY_CAPTURE`, `VOICE_UNAVAILABLE` — константы строк (точные значения в Step 3).

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_daily.py`:

```python
CARD = {"id": 1, "word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up.",
        "example_translation": "Просто предупреждаю.", "context": "созвон <QA>"}
S = "Can you give me a heads-up before you merge?"
S_RU = "Предупредишь меня перед мержем?"


def test_card_block_has_no_example_and_escapes():
    text = daily.card_block(CARD)
    assert "<b>a heads-up</b>" in text and "предупреждение заранее" in text
    assert "/ˈhedz ʌp/" in text and "📍 созвон &lt;QA&gt;" in text
    assert "Just a heads-up." not in text
    assert "📍" not in daily.card_block({**CARD, "context": None})


def test_render_task_compose_hinted_voice_and_text_fallback():
    t = daily.render_task("compose_hinted", CARD, S, S_RU, "a heads-up", voice_ok=True)
    assert "Напиши своё предложение с <b>a heads-up</b>" in t and S not in t
    t2 = daily.render_task("compose_hinted", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert daily.VOICE_UNAVAILABLE in t2 and f"<i>{S}</i>" in t2
    t3 = daily.render_task("compose_hinted", CARD, None, None, None, voice_ok=True)
    assert daily.VOICE_UNAVAILABLE not in t3 and "Напиши своё предложение" in t3
    t4 = daily.render_task("compose_hinted", CARD, None, None, None, voice_ok=False)
    assert daily.VOICE_UNAVAILABLE in t4 and "<i>" not in t4   # без предложения — только пометка


def test_render_task_gap_recall_listen_compose():
    gap = daily.render_task("gap", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert "<i>Can you give me ___ before you merge?</i>" in gap and S_RU in gap
    recall = daily.render_task("recall", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert "«предупреждение заранее»" in recall and "контекст: созвон &lt;QA&gt;" in recall
    assert S not in recall                                   # текст прячется до ответа
    recall2 = daily.render_task("recall", {**CARD, "context": None}, None, None, None, voice_ok=False)
    assert "контекст" not in recall2 and "None" not in recall2
    assert daily.render_task("listen", CARD, S, S_RU, "a heads-up", voice_ok=True) == "Напиши то, что услышишь."
    assert daily.render_task("compose", CARD, None, None, None, voice_ok=False) == \
        "Напиши своё предложение с <b>a heads-up</b>."


def test_render_compose_result():
    good = daily.render_compose_result({"verdict": "good", "corrected": "", "note": "ок",
                                        "reply_sentence": "Thanks for the heads-up!",
                                        "reply_sentence_ru": "Спасибо!"})
    assert good.startswith("✅ Отлично, звучит естественно.")
    assert "Моё в ответ: <i>Thanks for the heads-up!</i>" in good
    fix = daily.render_compose_result({"verdict": "fix", "corrected": "I gave the team a heads-up.",
                                       "note": "нужен артикль", "reply_sentence": None,
                                       "reply_sentence_ru": None})
    assert fix == "✅ Почти. Лучше так: I gave the team a heads-up. (нужен артикль)"
    off = daily.render_compose_result({"verdict": "off", "corrected": "", "note": "фраза не использована",
                                       "reply_sentence": None, "reply_sentence_ru": None})
    assert off == "❌ Фраза тут не сработала: фраза не использована."
    assert "лимит" in daily.render_compose_result(None)


def test_render_grade_result():
    assert daily.render_grade_result(None, exact=True, expected="a heads-up", quota=False) == "✅ Верно!"
    typo = daily.render_grade_result({"verdict": "typo", "correct": "a heads-up", "note": "дефис"},
                                     exact=False, expected="a heads-up", quota=False)
    assert typo == "✅ Почти! Правильно: a heads-up (дефис)"
    wrong = daily.render_grade_result({"verdict": "wrong", "correct": "a heads-up", "note": "это другое"},
                                      exact=False, expected="a heads-up", quota=False)
    assert wrong == "❌ Не совсем. Правильно: a heads-up (это другое)"
    quota = daily.render_grade_result(None, exact=False, expected="a heads-up", quota=True)
    assert quota.startswith("❌ Правильно: a heads-up") and "лимит" in quota


def test_render_listen_and_giveup_and_with_sentence():
    assert daily.render_listen_result(True, S) == f"✅ Всё верно: <i>{S}</i>"
    assert daily.render_listen_result(False, S) == f"Почти. Было: <i>{S}</i>"
    g = daily.render_giveup(CARD, S)
    assert g.startswith("Ничего 🙂") and "<b>a heads-up</b>" in g and f"<i>{S}</i>" in g
    assert "📝 пример: Just a heads-up." in g          # полная карточка, с примером (спека)
    assert g.endswith("Вернусь с ней завтра.")
    assert "<i>" not in daily.render_giveup(CARD, None)
    assert daily.with_sentence("x", S) == f"x\n\n🔊 <i>{S}</i>"
    assert daily.with_sentence("x", None) == "x"


def test_fit_caption_guarantees_limit_without_broken_html():
    short = "<b>ok</b>"
    assert daily.fit_caption(short) == short
    long = "<b>" + "a" * 600 + "</b> &amp; <i>" + "b" * 600 + "</i>"
    out = daily.fit_caption(long)
    assert len(out) <= daily.CAPTION_LIMIT and out.endswith("…")
    assert "<" not in out and "&amp;" in out            # теги сняты, амперсанд экранирован заново
    assert len(daily.fit_caption("&" * 1030)) <= daily.CAPTION_LIMIT
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily.py -q` → FAIL (`no attribute 'card_block'`).

- [ ] **Step 3: Реализация (добавить в `daily.py`)**

```python
import html

import formatting
from formatting import field

VOICE_UNAVAILABLE = "🔇 (озвучка временно недоступна)"
TEXT_NOTHING_NEXT = "Пока нечего повторять: все фразы ещё не подошли 🙂 Перешли что-нибудь новое."
TEXT_MORE_STALE = "Это было вчера 🙂 Утром пришлю новое."
TEXT_MORE_LIMIT = "На сегодня хватит, завтра продолжим 🙂"
TEXT_MORE_EMPTY = "Пока всё повторили 🎉"
TEXT_CARD_DELETED = "Эта фраза уже удалена 🙂"
TEXT_GRADE_FAILED = "Не получилось проверить сейчас 😕 Напиши ещё раз через минутку."
TEXT_SAVED = "Сохранено ✅ — придёт завтра утром."
TEXT_ONLY_TEXT = "Пока понимаю только текст: перешли сообщение или напиши фразу 🙂"
TEXT_NOTHING_FOUND = "Не вижу, что тут взять 🙂 Напиши слово или фразу явно."
TEXT_CLARIFY = "Это ответ на задание или новое слово?"
TEXT_CLARIFY_STALE = "Это задание уже истекло, напиши ответ на новое 🙂"
TEXT_EMPTY_CAPTURE = "После «+» напиши слово или фразу 🙂"
TEXT_QUOTA_COMPOSE = ("✅ Принято! (умная проверка пока недоступна — лимит бесплатных "
                      "запросов; своё предложение всё равно засчитано)")


def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def card_block(card) -> str:
    text = (f"🔤 <b>{_esc(card['word'])}</b>\n"
            f"🇷🇺 {_esc(card['translation'])}\n"
            f"🗣 {_esc(card['transcription'])}")
    context = field(card, "context")
    if context:
        text += f"\n📍 {_esc(context)}"
    return text


def render_task(kind: str, card, sentence: str | None, sentence_ru: str | None,
                phrase_form: str | None, *, voice_ok: bool) -> str:
    word = _esc(card["word"])
    if kind == "compose_hinted":
        text = card_block(card)
        if not voice_ok:
            text += f"\n{VOICE_UNAVAILABLE}"
            if sentence:
                text += f"\n<i>{_esc(sentence)}</i>"
        return text + (f"\n\nНапиши своё предложение с <b>{word}</b> — "
                       "про что-нибудь из твоей жизни.")
    if kind == "gap":
        return (f"Вставь пропуск:\n<i>{_esc(blank_out(sentence, phrase_form))}</i>\n"
                f"({_esc(sentence_ru)})")
    if kind == "recall":
        text = f"Как сказать по-английски: «{_esc(card['translation'])}»?"
        context = field(card, "context")
        return text + (f" (контекст: {_esc(context)})" if context else "")
    if kind == "listen":
        return "Напиши то, что услышишь."
    if kind == "compose":
        return f"Напиши своё предложение с <b>{word}</b>."
    raise ValueError(f"unknown kind {kind!r}")


def with_sentence(text: str, sentence: str | None) -> str:
    return f"{text}\n\n🔊 <i>{_esc(sentence)}</i>" if sentence else text


CAPTION_LIMIT = 1024
_TAG_RE = re.compile(r"<[^>]+>")


def fit_caption(text: str) -> str:
    """Подпись к голосовому ≤ 1024 символов — гарантированно.

    Длинный текст деградирует в plain: теги снимаем, сущности раскрываем, режем,
    экранируем заново (экранирование может удлинить — поэтому цикл) и ставим «…».
    """
    if len(text) <= CAPTION_LIMIT:
        return text
    plain = html.unescape(_TAG_RE.sub("", text))
    budget = CAPTION_LIMIT - 1
    out = _esc(plain[:budget])
    while len(out) > budget:
        budget -= max(1, len(out) - budget)
        out = _esc(plain[:budget])
    return out + "…"


def render_compose_result(check: dict | None) -> str:
    if check is None:
        return TEXT_QUOTA_COMPOSE
    v = check["verdict"]
    if v == "good":
        text = "✅ Отлично, звучит естественно."
    elif v == "fix":
        text = f"✅ Почти. Лучше так: {_esc(check['corrected'])} ({_esc(check['note'])})"
    else:
        text = f"❌ Фраза тут не сработала: {_esc(check['note'])}."
    if check.get("reply_sentence"):
        text += f"\n\nМоё в ответ: <i>{_esc(check['reply_sentence'])}</i>"
    return text


def render_grade_result(verdict: dict | None, *, exact: bool, expected: str, quota: bool) -> str:
    if exact:
        return "✅ Верно!"
    if quota or verdict is None:
        return (f"❌ Правильно: {_esc(expected)}\n(умная проверка пока недоступна — "
                "лимит бесплатных запросов; сравни свой ответ с правильным)")
    if verdict["verdict"] == "correct":
        return "✅ Верно!"
    if verdict["verdict"] == "typo":
        return f"✅ Почти! Правильно: {_esc(verdict['correct'])} ({_esc(verdict['note'])})"
    return f"❌ Не совсем. Правильно: {_esc(verdict['correct'])} ({_esc(verdict['note'])})"


def render_listen_result(ok: bool, sentence: str) -> str:
    return (f"✅ Всё верно: <i>{_esc(sentence)}</i>" if ok
            else f"Почти. Было: <i>{_esc(sentence)}</i>")


def render_giveup(card, sentence: str | None) -> str:
    text = f"Ничего 🙂\n\n{formatting.card_preview(card)}"   # полная карточка — с примером
    if sentence:
        text += f"\n\n<i>{_esc(sentence)}</i>"
    return text + "\n\nВернусь с ней завтра."
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные.

- [ ] **Step 5: Commit**

```bash
git add daily.py tests/test_daily.py
git commit -m "daily: HTML renderers for tasks, results, give-up and shared bot texts"
```

---

### Task 11: `daily.py` — выдача задания (`send_daily_task`), утренний прогон, цикл

**Files:**
- Modify: `daily.py`
- Test: `tests/test_daily_send.py` (новый)

**Interfaces:**
- Consumes: `db.*` из Task 2–3, `sentences.make_sentence/fallback_sentence/SentenceError`, `tts.synthesize/TTSError`, `voice.send_text_voice/send_card_voice_to`, рендеры Task 10, `QuotaExceededError`.
- Produces (в `daily.py`):
  - `user_lock(user_id: int) -> asyncio.Lock` (модульный dict)
  - `@dataclass Prepared: kind: str; sentence: str | None; sentence_ru: str | None; phrase_form: str | None; from_example: bool`
  - `async prepare_task(conn, llm, profile, card, rng) -> Prepared` — вид по ступени, предложение (Gemini → fallback-пример → без предложения), понижение по `has_sentence`
  - `async deliver(bot, conn, profile, chat_id, prepared, card) -> str` — TTS (если нужен), понижение по `has_voice`, ОДНО сообщение; возвращает фактический `kind`; ошибки Telegram пробрасывает
  - `async send_daily_task(bot, conn, llm, profile, user_id, today: date, rng, *, morning: bool, limit: int | None = None) -> str` — `"sent" | "resent" | "nothing" | "failed" | "limit"`, целиком под `user_lock`; `limit` — потолок заданий за сегодня (кнопка «Ещё одно» передаёт `MAX_TASKS_PER_DAY`, `/next` и утро — `None`), проверяется ВНУТРИ лока; при повторе (`resent`) понижённый из-за TTS вид сохраняется в задачу (`db.set_task_kind`)
  - `async run_morning(bot, conn, llm, profile, user_ids: Iterable[int], today: date, rng) -> None`
  - `async daily_loop(bot, conn, llm, profile, cfg) -> None`
  - `TELEGRAM_SEND_ERRORS = (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError)`

- [ ] **Step 1: Падающие тесты**

`tests/test_daily_send.py`:

```python
"""Выдача задания: fake bot, in-memory SQLite, замоканные Gemini и TTS."""
import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError

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
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily_send.py -q` → FAIL (`no attribute 'send_daily_task'`).

- [ ] **Step 3: Реализация (добавить в `daily.py`)**

```python
import asyncio
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError)

import db
import voice
from services import sentences, tts
from services.llm import QuotaExceededError

log = logging.getLogger(__name__)
TELEGRAM_SEND_ERRORS = (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError)

_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


@dataclass
class Prepared:
    kind: str
    sentence: str | None
    sentence_ru: str | None
    phrase_form: str | None
    from_example: bool


async def prepare_task(conn, llm, profile, card, rng: random.Random) -> Prepared:
    """Вид задания + предложение: Gemini → пример карточки → без предложения."""
    kind = task_kind(card["interval_days"], rng)
    sent: dict | None = None
    from_example = False
    if kind != "compose":
        avoid = db.recent_sentences(conn, card["id"])
        try:
            sent = await asyncio.to_thread(sentences.make_sentence, llm, profile, card, kind, avoid)
        except (sentences.SentenceError, QuotaExceededError) as exc:
            log.warning("make_sentence failed for card %s: %s", card["id"], exc)
            sent = sentences.fallback_sentence(card)
            from_example = sent is not None
        except Exception:
            log.exception("make_sentence crashed for card %s", card["id"])
            sent = sentences.fallback_sentence(card)
            from_example = sent is not None
    kind = downgrade_kind(kind, has_sentence=sent is not None, has_voice=True)
    return Prepared(kind=kind,
                    sentence=sent["sentence"] if sent else None,
                    sentence_ru=sent["sentence_ru"] if sent else None,
                    phrase_form=sent["phrase_form"] if sent else None,
                    from_example=from_example)


async def _synthesize_tmp(text: str, voice_name: str) -> str | None:
    fh = tempfile.NamedTemporaryFile(prefix="daily_", suffix=".mp3", delete=False)
    fh.close()
    tmp = fh.name   # уникальное имя: два пользователя с одним предложением не делят файл
    try:
        await tts.synthesize(text, voice_name, tmp)
        return tmp
    except (tts.TTSError, OSError) as exc:
        log.warning("tts failed: %s", exc)
        if os.path.exists(tmp):
            os.remove(tmp)
        return None


async def deliver(bot, conn, profile, chat_id: int, prepared: Prepared, card) -> str:
    """Одно сообщение на задание. Возвращает фактический kind (после понижения по голосу)."""
    kind = prepared.kind
    mp3 = None
    if kind in VOICE_KINDS and prepared.sentence:
        mp3 = await _synthesize_tmp(prepared.sentence, profile.tts_voice)
        kind = downgrade_kind(kind, has_sentence=True, has_voice=mp3 is not None)
    word_voice_case = kind == "compose_hinted" and prepared.sentence is None
    text = render_task(kind, card, prepared.sentence, prepared.sentence_ru,
                       prepared.phrase_form, voice_ok=(mp3 is not None) or word_voice_case)
    try:
        if mp3 is not None:
            await voice.send_text_voice(bot, chat_id, mp3, caption=fit_caption(text),
                                        parse_mode="HTML")
        elif word_voice_case:
            sent = await voice.send_card_voice_to(bot, chat_id, conn, card, profile.tts_voice,
                                                  caption=fit_caption(text), parse_mode="HTML")
            if sent is None:   # и озвучка слова не удалась — текст с пометкой 🔇
                await bot.send_message(
                    chat_id, render_task(kind, card, None, None, None, voice_ok=False),
                    parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    finally:
        if mp3 is not None and os.path.exists(mp3):
            os.remove(mp3)
    return kind


def _prepared_from_task(task) -> Prepared:
    return Prepared(kind=task["kind"], sentence=task["sentence"], sentence_ru=task["sentence_ru"],
                    phrase_form=task["phrase_form"], from_example=bool(task["from_example"]))


async def send_daily_task(bot, conn, llm, profile, user_id: int, today: date,
                          rng: random.Random, *, morning: bool,
                          limit: int | None = None) -> str:
    """Выдать задание. "sent" | "resent" | "nothing" | "failed" | "limit". Целиком под локом."""
    async with user_lock(user_id):
        if limit is not None and db.count_tasks_on(conn, user_id, today) >= limit:
            return "limit"          # потолок дня — раньше любого повтора (контракт «Ещё одно»)
        active = db.open_task(conn, user_id)
        if active is not None:
            if morning:
                if active["sent_on"] == today.isoformat():
                    return "nothing"   # сегодняшнее уже выдано (/next перед DAILY_AT) — не истекаем
                was_morning = db.expire_task(conn, active["id"])
                if was_morning:
                    db.bump_missed(conn, user_id)
            else:
                card = db.get_card(conn, active["card_id"])
                if card is not None:
                    try:
                        final_kind = await deliver(bot, conn, profile, user_id,
                                                   _prepared_from_task(active), card)
                    except TELEGRAM_SEND_ERRORS as exc:
                        log.warning("resend to %s failed: %s", user_id, exc)
                        return "failed"
                    if final_kind != active["kind"]:
                        db.set_task_kind(conn, active["id"], final_kind)
                    return "resent"
                db.expire_task(conn, active["id"])   # карточка удалена — задача мертва
        if morning:
            st = db.get_daily_state(conn, user_id)
            if not should_send(st["missed_streak"], st["last_sent_on"], today):
                return "nothing"
        card = db.pick_due_card(conn, user_id, today)
        if card is None:
            return "nothing"
        prepared = await prepare_task(conn, llm, profile, card, rng)
        try:
            final_kind = await deliver(bot, conn, profile, user_id, prepared, card)
        except TELEGRAM_SEND_ERRORS as exc:
            log.warning("send to %s failed: %s", user_id, exc)
            return "failed"
        db.create_task(conn, user_id=user_id, card_id=card["id"], kind=final_kind,
                       sentence=prepared.sentence, sentence_ru=prepared.sentence_ru,
                       phrase_form=prepared.phrase_form, from_example=prepared.from_example,
                       today=today, morning=morning)
        if morning:
            db.set_last_sent(conn, user_id, today)
        return "sent"


async def run_morning(bot, conn, llm, profile, user_ids: Iterable[int], today: date,
                      rng: random.Random) -> None:
    for uid in sorted(set(user_ids)):
        try:
            await send_daily_task(bot, conn, llm, profile, uid, today, rng, morning=True)
        except Exception:
            log.exception("daily task for %s failed", uid)


async def daily_loop(bot, conn, llm, profile, cfg) -> None:
    tz = ZoneInfo(cfg.daily_tz)
    rng = random.Random()
    while True:
        now = datetime.now(tz)
        target = next_fire(now, cfg.daily_at.hour, cfg.daily_at.minute)
        await asyncio.sleep(fire_delay(now, target))
        try:
            await run_morning(bot, conn, llm, profile,
                              cfg.allowed_user_ids - cfg.daily_exclude_ids, date.today(), rng)
        except Exception:
            log.exception("daily run failed")
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные. Если `test_concurrent_next_yields_one_sent_one_resent` проходит слишком «легко» (оба `sent`), значит лок не покрывает проверку открытой задачи — это и есть баг P1 из ревью спеки, чинить, не ослаблять тест.

- [ ] **Step 5: Commit**

```bash
git add daily.py tests/test_daily_send.py
git commit -m "daily: send_daily_task under per-user lock, prepare/deliver with fallbacks, run_morning, daily_loop"
```

---

### Task 12: `daily.py` — ответ на задание (`answer_task`) с оценкой и SRS

**Files:**
- Modify: `daily.py`, `keyboards.py`
- Test: `tests/test_daily_answer.py` (новый), `tests/test_keyboards.py` (дополнить)

**Interfaces:**
- Consumes: `db.claim_task/release_task/finish_task/get_card/update_review/reset_missed/count_tasks_on/pick_due_card`, `sentences.check_sentence`, `grading.grade/answers_match/GradingError`, `srs.next_interval/due_on`, рендеры Task 10, `voice.*`.
- Produces:
  - `keyboards.more_keyboard(today: date) -> InlineKeyboardMarkup` — одна кнопка «➕ Ещё одно», `callback_data=f"more:{today.isoformat()}"`
  - `@dataclass Graded: ok: bool; text: str; speak: str | None; reply_sentence: str | None`
  - `async grade_answer(llm, profile, task, card, answer: str, *, giveup: bool, avoid: list[str]) -> Graded` — чистая по отношению к БД (`avoid` = `db.recent_sentences` передаёт вызывающий); может бросить `SentenceError`/`GradingError`
  - `async answer_task(bot, conn, llm, profile, user_id: int, text: str, today: date, *, giveup: bool, task_id: int | None = None) -> bool` — `True`, если ответ принят к оценке; целиком под `user_lock`. `task_id` — задача, которую видел хендлер ДО лока: внутри лока берётся именно она (`db.get_task`) и только если она всё ещё `open`; утро, успевшее истечь её и выдать новую, не получит чужой ответ. `None` (тесты) — текущая открытая.
  - `more_button_allowed(conn, user_id, today) -> bool`

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_keyboards.py`:

```python
from datetime import date

def test_more_keyboard_carries_date():
    kb = keyboards.more_keyboard(date(2026, 10, 5))
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "more:2026-10-05" and "Ещё" in btn.text
```

`tests/test_daily_answer.py`:

```python
"""Ответ на задание: claim → оценка → перечитать карточку → SRS → finish → отправка."""
import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest

import daily
import db
from languages import PROFILES
from services import grading, sentences, tts
from services.llm import QuotaExceededError
from tests.test_daily_send import FakeBot, _card, fake_tts  # noqa: F401  (fixture reuse)

EN = PROFILES["en"]
U = 111
TODAY = date(2026, 10, 5)
S = "Can you give me a heads-up before you merge?"


def _open(conn, cid, kind, *, sentence=S, phrase_form="a heads-up", morning=True, today=TODAY):
    return db.create_task(conn, user_id=U, card_id=cid, kind=kind, sentence=sentence,
                          sentence_ru="Предупредишь перед мержем?", phrase_form=phrase_form,
                          from_example=False, today=today, morning=morning)


CHECK = {"verdict": "good", "corrected": "", "note": "ок",
         "reply_sentence": "Thanks for the heads-up!", "reply_sentence_ru": "Спасибо!"}


async def test_compose_good_updates_srs_and_sends_reply_voice(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: dict(CHECK))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "I gave the team a heads-up.", TODAY, giveup=False)
    task = db.get_task(conn, tid)
    assert task["status"] == "answered" and task["answered_ok"] == 1
    assert task["reply_sentence"] == "Thanks for the heads-up!"
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 1 and card["due_at"] == "2026-10-06" and card["reps"] == 1
    assert len(bot.sent) == 1 and bot.sent[0][0] == "voice"
    assert "✅ Отлично" in bot.sent[0][2] and "Thanks for the heads-up!" in bot.sent[0][2]
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_compose_off_resets_interval(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence",
                        lambda *a, **k: {**CHECK, "verdict": "off", "note": "фразы нет", "reply_sentence": None})
    cid = _card(conn, interval=14, due=TODAY)
    _open(conn, cid, "compose", sentence=None, phrase_form=None)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "Nothing relevant.", TODAY, giveup=False)
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 1 and card["lapses"] == 1
    assert bot.sent[0][0] == "message" and bot.sent[0][2].startswith("❌ Фраза тут не сработала")


async def test_compose_quota_is_accepted_as_ok(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(sentences, "check_sentence", boom)
    cid = _card(conn)
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "I gave a heads-up.", TODAY, giveup=False)
    assert db.get_card(conn, cid)["interval_days"] == 1
    assert "лимит" in bot.sent[0][2]


async def test_recall_exact_match_skips_gemini_and_speaks_sentence(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=3, due=TODAY)
    _open(conn, cid, "recall")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "A heads-up", TODAY, giveup=False)
    assert bot.sent[0][0] == "voice" and bot.sent[0][2].startswith("✅ Верно!") and S in bot.sent[0][2]
    assert db.get_card(conn, cid)["interval_days"] == 7


async def test_recall_without_sentence_speaks_word(conn, fake_tts, monkeypatch):
    cid = _card(conn, interval=3, due=TODAY)
    _open(conn, cid, "recall", sentence=None, phrase_form=None)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "a heads-up", TODAY, giveup=False)
    assert bot.sent[0][0] == "voice"                       # озвучка слова через send_card_voice_to
    assert db.get_card(conn, cid)["audio_file_id"] == "FID"  # и закэширована


async def test_gap_accepts_only_phrase_form_exactly_else_grades(conn, fake_tts, monkeypatch):
    calls = []
    monkeypatch.setattr(grading, "grade",
                        lambda llm, p, *, prompt_ru, expected, answer: calls.append((prompt_ru, expected, answer))
                        or {"verdict": "wrong", "correct": expected, "note": "форма"})
    cid = _card(conn, interval=1, due=TODAY)
    _open(conn, cid, "gap", phrase_form="gave him a heads-up")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "give someone a heads-up", TODAY, giveup=False)
    assert calls == [("Предупредишь перед мержем?", "gave him a heads-up", "give someone a heads-up")]
    assert bot.sent[0][2].startswith("❌ Не совсем")
    assert db.get_card(conn, cid)["interval_days"] == 1 and db.get_card(conn, cid)["lapses"] == 1


async def test_listen_uses_listen_ok_no_gemini(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=7, due=TODAY)
    _open(conn, cid, "listen")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "can you give me a heads up before you merge", TODAY, giveup=False)
    assert bot.sent[0][0] == "message" and bot.sent[0][2].startswith("✅ Всё верно")
    assert db.get_card(conn, cid)["interval_days"] == 14


async def test_giveup_resets_and_shows_card(conn, fake_tts):
    cid = _card(conn, interval=7, due=TODAY)
    _open(conn, cid, "listen")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "не помню", TODAY, giveup=True)
    assert db.get_card(conn, cid)["interval_days"] == 1
    assert bot.sent[0][0] == "voice" and bot.sent[0][2].startswith("Ничего 🙂")


async def test_answer_for_stale_task_id_is_refused(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("оценки не должно быть"))
    cid = _card(conn)
    old = _open(conn, cid, "compose_hinted", today=date(2026, 10, 4))
    db.expire_task(conn, old)
    _open(conn, cid, "compose", sentence=None, phrase_form=None)      # новая открытая
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False, task_id=old) is False
    assert db.open_task(conn, U)["status"] == "open" and bot.sent == []


async def test_second_answer_is_ignored(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: dict(CHECK))
    cid = _card(conn)
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "first", TODAY, giveup=False) is True
    assert await daily.answer_task(bot, conn, None, EN, U, "second", TODAY, giveup=False) is False
    assert len(bot.sent) == 1 and db.get_card(conn, cid)["reps"] == 1


async def _inline_to_thread(fn, *args, **kwargs):
    """sqlite3-коннект тестов однопоточный (check_same_thread) — мок, трогающий БД,
    должен исполниться в потоке теста, а не в worker-е to_thread."""
    return fn(*args, **kwargs)


async def test_card_reviewed_manually_meanwhile_keeps_srs(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    cid = _card(conn, interval=3, due=TODAY)
    tid = _open(conn, cid, "recall")

    def grade_and_shift(*a, **k):
        # пока «думает Gemini», ручная тренировка сдвинула карточку
        db.update_review(conn, cid, interval_days=7, due_at=date(2026, 10, 12), remembered=True)
        return {"verdict": "correct", "correct": "a heads-up", "note": "ок"}
    monkeypatch.setattr(grading, "grade", grade_and_shift)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "heads up", TODAY, giveup=False)
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 7 and card["due_at"] == "2026-10-12" and card["reps"] == 1
    assert db.get_task(conn, tid)["status"] == "answered"


async def test_card_deleted_during_grading(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")

    def check_and_delete(*a, **k):
        db.delete_card(conn, cid, user_id=U)
        return dict(CHECK)
    monkeypatch.setattr(sentences, "check_sentence", check_and_delete)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "answered" and db.get_task(conn, tid)["answered_ok"] == 0
    assert bot.sent[-1][2] == daily.TEXT_CARD_DELETED
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_card_deleted_before_answer(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("оценки не должно быть"))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    db.delete_card(conn, cid, user_id=U)
    db.bump_missed(conn, U)
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False) is True
    t = db.get_task(conn, tid)
    assert t["status"] == "answered" and t["answered_ok"] == 0
    assert bot.sent[-1][2] == daily.TEXT_CARD_DELETED
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_morning_during_grading_waits_for_lock(conn, fake_tts, monkeypatch):
    """Утренняя выдача, пришедшая во время оценки, ждёт лок: задачу не истекает, пропуск не растёт."""
    async def slow_to_thread(fn, *args, **kwargs):
        await asyncio.sleep(0.02)
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", slow_to_thread)
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    monkeypatch.setattr(sentences, "make_sentence", lambda *a, **k: {"sentence": "S2", "sentence_ru": "r",
                                                                      "phrase_form": "second"})
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted", today=date(2026, 10, 4))
    _card(conn, word="second", example="A second one.")
    bot = FakeBot()
    answer = asyncio.create_task(daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False))
    await asyncio.sleep(0)   # answer_task успел взять лок и уйти в оценку
    morning = asyncio.create_task(daily.send_daily_task(bot, conn, None, EN, U, TODAY,
                                                        random.Random(1), morning=True))
    await asyncio.gather(answer, morning)
    assert db.get_task(conn, tid)["status"] == "answered"       # не expired
    assert db.get_daily_state(conn, U)["missed_streak"] == 0
    assert db.open_task(conn, U)["card_id"] != cid               # утро выдало следующую карточку


async def test_grading_failure_releases_task(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise sentences.SentenceError("junk")
    monkeypatch.setattr(sentences, "check_sentence", boom)
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "open"
    assert db.get_card(conn, cid)["reps"] == 0
    assert bot.sent[-1][2] == daily.TEXT_GRADE_FAILED


async def test_send_failure_after_finish_keeps_srs(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot(fail_for={U})
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "answered"
    assert db.get_card(conn, cid)["reps"] == 1


async def test_more_button_only_when_queue_and_limit_allow(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    _card(conn, word="another one", example="Another one here.")
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert bot.sent[-1][3]["reply_markup"].inline_keyboard[0][0].callback_data == "more:2026-10-05"
    # очередь пуста → кнопки нет
    cid3 = _card(conn, word="third", example="Third one here.")
    for w in ("another one",):
        row = conn.execute("SELECT id FROM cards WHERE word = ?", (w,)).fetchone()
        db.update_review(conn, row["id"], interval_days=30, due_at=date(2026, 11, 5), remembered=True)
    db.update_review(conn, cid3, interval_days=30, due_at=date(2026, 11, 5), remembered=True)
    _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)
    await daily.answer_task(bot, conn, None, EN, U, "y", TODAY, giveup=False)
    assert bot.sent[-1][3].get("reply_markup") is None


async def test_more_button_hidden_at_daily_limit(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    _card(conn, word="another one", example="Another one here.")
    for _ in range(2):   # две уже выданы и закрыты сегодня
        t = _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)
        db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)   # третья
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert bot.sent[-1][3].get("reply_markup") is None
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily_answer.py tests/test_keyboards.py -q` → FAIL.

- [ ] **Step 3: Реализация — `keyboards.py`**

```python
from datetime import date


def more_keyboard(today: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё одно", callback_data=f"more:{today.isoformat()}")
    ]])
```

- [ ] **Step 4: Реализация — `daily.py`**

```python
import keyboards
from services import grading, srs


@dataclass
class Graded:
    ok: bool
    text: str
    speak: str | None          # что озвучить вместе с ответом (предложение) или None
    reply_sentence: str | None


async def grade_answer(llm, profile, task, card, answer: str, *, giveup: bool,
                       avoid: list[str]) -> Graded:
    kind = task["kind"]
    sentence = task["sentence"]
    if giveup:
        return Graded(False, render_giveup(card, sentence), sentence or card["word"], None)
    if kind in ("compose_hinted", "compose"):
        try:
            check = await asyncio.to_thread(sentences.check_sentence, llm, profile, card, answer, avoid)
        except QuotaExceededError:
            return Graded(True, render_compose_result(None), None, None)
        ok = check["verdict"] in ("good", "fix")
        return Graded(ok, render_compose_result(check), check.get("reply_sentence"),
                      check.get("reply_sentence"))
    if kind in ("recall", "gap"):
        expected = card["word"] if kind == "recall" else (task["phrase_form"] or card["word"])
        prompt_ru = card["translation"] if kind == "recall" else (task["sentence_ru"] or card["translation"])
        if grading.answers_match(answer, expected):
            text = render_grade_result(None, exact=True, expected=expected, quota=False)
            ok = True
        else:
            try:
                verdict = await asyncio.to_thread(grading.grade, llm, profile, prompt_ru=prompt_ru,
                                                  expected=expected, answer=answer.strip())
                ok = verdict["verdict"] in ("correct", "typo")
                text = render_grade_result(verdict, exact=False, expected=expected, quota=False)
            except QuotaExceededError:
                ok = False
                text = render_grade_result(None, exact=False, expected=expected, quota=True)
        return Graded(ok, with_sentence(text, sentence), sentence or card["word"], None)
    if kind == "listen":
        ok = listen_ok(answer, sentence or "")
        return Graded(ok, render_listen_result(ok, sentence or card["word"]), None, None)
    raise ValueError(f"unknown kind {kind!r}")


def more_button_allowed(conn, user_id: int, today: date) -> bool:
    return (db.count_tasks_on(conn, user_id, today) < MAX_TASKS_PER_DAY
            and db.pick_due_card(conn, user_id, today) is not None)


async def _send_result(bot, conn, profile, chat_id: int, card, graded: Graded, markup) -> None:
    if graded.speak and graded.speak != card["word"]:
        mp3 = await _synthesize_tmp(graded.speak, profile.tts_voice)
        if mp3 is not None:
            try:
                await voice.send_text_voice(bot, chat_id, mp3, caption=fit_caption(graded.text),
                                            parse_mode="HTML", reply_markup=markup)
                return
            finally:
                if os.path.exists(mp3):
                    os.remove(mp3)
    if graded.speak == card["word"]:
        sent = await voice.send_card_voice_to(bot, chat_id, conn, card, profile.tts_voice,
                                              caption=fit_caption(graded.text), parse_mode="HTML",
                                              reply_markup=markup)
        if sent is not None:
            return
    await bot.send_message(chat_id, graded.text, parse_mode="HTML", reply_markup=markup)


async def answer_task(bot, conn, llm, profile, user_id: int, text: str, today: date,
                      *, giveup: bool, task_id: int | None = None) -> bool:
    """claim → оценка → перечитать карточку → SRS → finish → отправка. Под локом.

    task_id — задача, которую хендлер видел до лока; если её уже истекло утро и висит
    новая, ответ НЕ применяется к новой (возвращаем False, пользователь увидит новое задание).
    """
    async with user_lock(user_id):
        task = db.get_task(conn, task_id) if task_id is not None else db.open_task(conn, user_id)
        if task is None or task["user_id"] != user_id or task["status"] != db.TASK_OPEN:
            return False
        if not db.claim_task(conn, task["id"]):
            return False
        card = db.get_card(conn, task["card_id"])
        if card is None:
            db.finish_task(conn, task["id"], ok=False)
            db.reset_missed(conn, user_id)          # ответ был — тихий режим снимается
            await _safe_send(bot, user_id, TEXT_CARD_DELETED)
            return True
        try:
            graded = await grade_answer(llm, profile, task, card, text, giveup=giveup,
                                        avoid=db.recent_sentences(conn, card["id"], n=3))
        except Exception:
            log.exception("grading failed for task %s", task["id"])
            db.release_task(conn, task["id"])
            await _safe_send(bot, user_id, TEXT_GRADE_FAILED)
            return True
        fresh = db.get_card(conn, card["id"])
        if fresh is None:
            db.finish_task(conn, task["id"], ok=False)
            db.reset_missed(conn, user_id)
            await _safe_send(bot, user_id, TEXT_CARD_DELETED)
            return True
        if fresh["due_at"] <= today.isoformat():
            interval = srs.next_interval(fresh["interval_days"], graded.ok)
            db.update_review(conn, fresh["id"], interval_days=interval,
                             due_at=srs.due_on(today, interval), remembered=graded.ok)
        db.finish_task(conn, task["id"], ok=graded.ok, reply_sentence=graded.reply_sentence)
        db.reset_missed(conn, user_id)
        markup = keyboards.more_keyboard(today) if more_button_allowed(conn, user_id, today) else None
        try:
            await _send_result(bot, conn, profile, user_id, fresh, graded, markup)
        except TELEGRAM_SEND_ERRORS as exc:
            log.warning("result to %s not delivered: %s", user_id, exc)
        return True


async def _safe_send(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TELEGRAM_SEND_ERRORS as exc:
        log.warning("message to %s not delivered: %s", chat_id, exc)
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/pytest -q` → зелёные. `test_card_deleted_during_grading`: `FakeBot.sent[-1]` — текст про удаление ушёл через `send_message` без parse_mode, это ок.

- [ ] **Step 6: Commit**

```bash
git add daily.py keyboards.py tests/test_daily_answer.py tests/test_keyboards.py
git commit -m "daily: answer_task — claim/grade/re-read/SRS/finish under lock, more-button, voice replies"
```

---

### Task 13: Сбор фраз, роутер `handlers/daily.py`, проводка в `bot.py` и `handlers/add.py`

**Files:**
- Create: `handlers/daily.py`
- Modify: `keyboards.py` (`take_keyboard`, `clarify_keyboard`), `daily.py` (`capture_items`), `bot.py`, `handlers/add.py` (`save_yes`), `tests/test_handlers_quota.py` (образец вызова хендлеров напрямую — посмотреть перед написанием)
- Test: `tests/test_daily_capture.py` (новый), `tests/test_keyboards.py`, `tests/test_bot_wiring.py` (новый)

**Interfaces:**
- Produces:
  - `keyboards.take_keyboard(seq: int)` — «✅ Беру» `take:yes:{seq}` / «❌ Не надо» `take:no:{seq}`; `keyboards.clarify_keyboard(seq: int)` — «✍️ Ответ» `clarify:answer:{seq}` / «➕ Новое слово» `clarify:capture:{seq}`
  - `daily.capture_items(conn, llm, profile, user_id, text) -> tuple[list[dict], str | None]` — вызывает `capture.extract` в `to_thread`; возвращает (элементы без дублей по `card_exists`, текст-ошибка для пользователя или `None`); квота/`CaptureError`/прочее → те же тексты, что в `handlers/add.py`
  - `handlers/daily.router` с хендлерами: `cmd_next` (`/next`), `on_more` (`more:`), `on_take_yes/on_take_no` (`take:yes:`/`take:no:`), `on_clarify_answer/on_clarify_capture` (`clarify:`), `on_free_text` (`StateFilter(None)`, `F.text`, не меню), `reject_non_text` (`StateFilter(None)`, `~F.text`)
  - `bot.py`: `db.release_stale_grading(conn)` после `init_db`; `if profile.daily_practice: dp.include_router(daily_handlers.router)` ПОСЛЕДНИМ; `if daily.should_start_loop(profile, cfg): task = asyncio.create_task(daily.daily_loop(...))` + `finally: cancel`
  - `handlers/add.py::save_yes` получает `profile: LanguageProfile` и после `db.add_card` делает `if profile.daily_practice: db.reset_missed(conn, call.from_user.id)`
- Consumes: всё из Task 1–12.

- [ ] **Step 1: Падающие тесты**

Дополнить `tests/test_keyboards.py`:

```python
def test_take_and_clarify_keyboards_carry_seq():
    take = keyboards.take_keyboard(7).inline_keyboard[0]
    assert [b.callback_data for b in take] == ["take:yes:7", "take:no:7"]
    clar = keyboards.clarify_keyboard(3).inline_keyboard[0]
    assert [b.callback_data for b in clar] == ["clarify:answer:3", "clarify:capture:3"]
```

`tests/test_daily_capture.py`:

```python
from datetime import date
from types import SimpleNamespace

import daily
import db
from languages import PROFILES
from services import capture
from services.llm import QuotaExceededError

EN = PROFILES["en"]
U = 111
ITEM = {"kind": "phrase", "word": "a heads-up", "translation": "п", "transcription": "/x/",
        "example": "Just a heads-up.", "example_translation": "э", "context": "созвон", "usage": "обычно: …"}


async def test_capture_items_dedups_against_vocab(conn, monkeypatch):
    db.add_card(conn, user_id=U, kind="phrase", word="A Heads-Up", translation="п", transcription="x",
                example="e", example_translation="э", enriched=True, today=date(2026, 10, 1))
    monkeypatch.setattr(capture, "extract", lambda llm, p, text: [dict(ITEM), {**ITEM, "word": "fresh one"},
                                                                  {**ITEM, "word": "Fresh One"}])
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert err is None and [i["word"] for i in items] == ["fresh one"]   # и дубль внутри ответа снят


async def test_capture_items_quota_and_error_texts(conn, monkeypatch):
    def quota(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(capture, "extract", quota)
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert items == [] and "Лимит" in err

    def broken(*a, **k):
        raise capture.CaptureError("x")
    monkeypatch.setattr(capture, "extract", broken)
    items, err = await daily.capture_items(conn, None, EN, U, "text")
    assert items == [] and "Не получилось" in err


async def test_capture_items_empty_is_not_an_error(conn, monkeypatch):
    monkeypatch.setattr(capture, "extract", lambda *a, **k: [])
    assert await daily.capture_items(conn, None, EN, U, "привет") == ([], None)
```

`tests/test_bot_wiring.py`:

```python
"""Проводка в bot.py: роутер daily — последним и только для en; цикл — только при двух гейтах."""
import asyncio
from datetime import time
from types import SimpleNamespace

from aiogram import Router

import bot as bot_module
from handlers import daily as daily_handlers
from languages import PROFILES


def _fresh_routers():
    # Router нельзя подключить к двум Dispatcher'ам (aiogram: «Router is already attached»),
    # поэтому в тестах — свежие экземпляры; порядок и гейт проверяем на них.
    return [Router(), Router(), Router()], Router()


def test_build_dispatcher_includes_daily_router_last_for_en():
    base, daily_router = _fresh_routers()
    dp = bot_module.build_dispatcher(conn=None, llm=None, profile=PROFILES["en"],
                                     base_routers=base, daily_router=daily_router)
    assert dp.sub_routers[:3] == base and dp.sub_routers[-1] is daily_router
    assert dp["profile"] is PROFILES["en"]


def test_build_dispatcher_excludes_daily_router_for_es():
    base, daily_router = _fresh_routers()
    dp = bot_module.build_dispatcher(conn=None, llm=None, profile=PROFILES["es"],
                                     base_routers=base, daily_router=daily_router)
    assert daily_router not in dp.sub_routers and dp.sub_routers == base


def test_build_dispatcher_defaults_are_the_real_routers():
    assert bot_module.BASE_ROUTERS[-1] is bot_module.training.router
    assert bot_module.DAILY_ROUTER is daily_handlers.router


async def test_start_daily_loop_gates(monkeypatch):
    started = []

    async def fake_loop(*a, **k):
        started.append(1)
        await asyncio.sleep(3600)
    monkeypatch.setattr(bot_module.daily, "daily_loop", fake_loop)
    on = SimpleNamespace(daily_at=time(9, 30))
    off = SimpleNamespace(daily_at=None)
    assert bot_module.start_daily_loop(None, None, None, PROFILES["es"], on) is None
    assert bot_module.start_daily_loop(None, None, None, PROFILES["en"], off) is None
    task = bot_module.start_daily_loop(None, None, None, PROFILES["en"], on)
    assert task is not None
    await asyncio.sleep(0)
    assert started == [1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_release_stale_grading_called_on_startup(conn, monkeypatch):
    called = []
    monkeypatch.setattr(bot_module.db, "release_stale_grading", lambda c: called.append(c) or 0)
    bot_module.prepare_db(conn)
    assert called == [conn]
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_daily_capture.py tests/test_bot_wiring.py tests/test_keyboards.py -q` → FAIL.

- [ ] **Step 3: `keyboards.py`**

```python
def take_keyboard(seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Беру", callback_data=f"take:yes:{seq}"),
        InlineKeyboardButton(text="❌ Не надо", callback_data=f"take:no:{seq}"),
    ]])


def clarify_keyboard(seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответ", callback_data=f"clarify:answer:{seq}"),
        InlineKeyboardButton(text="➕ Новое слово", callback_data=f"clarify:capture:{seq}"),
    ]])
```

- [ ] **Step 4: `daily.py` — `capture_items`**

```python
from services import capture

TEXT_QUOTA = "Лимит бесплатных ИИ-запросов пока исчерпан 😕 Попробуй позже или завтра."
TEXT_CAPTURE_FAILED = ("Не получилось обработать сейчас 😕 Попробуй ещё раз через минутку "
                       "или пришли другое слово.")


async def capture_items(conn, llm, profile, user_id: int, text: str) -> tuple[list[dict], str | None]:
    try:
        items = await asyncio.to_thread(capture.extract, llm, profile, text)
    except QuotaExceededError:
        return [], TEXT_QUOTA
    except capture.CaptureError:
        return [], TEXT_CAPTURE_FAILED
    except Exception:
        log.exception("capture failed unexpectedly")
        return [], TEXT_CAPTURE_FAILED
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        key = item["word"].strip().lower()
        if key in seen or db.card_exists(conn, user_id, item["word"]):
            continue          # дубль в словаре ИЛИ внутри одного ответа модели
        seen.add(key)
        out.append(item)
    return out, None
```

- [ ] **Step 5: `handlers/daily.py`** (файл целиком)

```python
"""Ежедневная практика: /next, ответы на задание, сбор фраз из свободного текста.

Роутер подключается ПОСЛЕДНИМ и только для профиля с daily_practice=True.
Все хендлеры — вне режимов (StateFilter(None)); меню/add/training имеют приоритет.
Логика — в daily.py; здесь только разбор апдейта и FSM-`pending`."""
from __future__ import annotations

import random
import sqlite3
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import daily
import db
import formatting
import keyboards
from languages import LanguageProfile
from services.llm import LLM

router = Router()
_rng = random.Random()


@router.message(Command("next"), StateFilter(None))
async def cmd_next(message: Message, conn: sqlite3.Connection, llm: LLM,
                   profile: LanguageProfile) -> None:
    result = await daily.send_daily_task(message.bot, conn, llm, profile, message.from_user.id,
                                         date.today(), _rng, morning=False)
    if result == "nothing":
        await message.answer(daily.TEXT_NOTHING_NEXT)
    elif result == "failed":
        await message.answer(daily.TEXT_CAPTURE_FAILED)


async def _drop_markup(call: CallbackQuery) -> None:
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(StateFilter(None), F.data.startswith("more:"))
async def on_more(call: CallbackQuery, conn: sqlite3.Connection, llm: LLM,
                  profile: LanguageProfile) -> None:
    await _drop_markup(call)
    today = date.today()
    if call.data.split(":", 1)[1] != today.isoformat():
        await call.message.answer(daily.TEXT_MORE_STALE)
    else:
        result = await daily.send_daily_task(call.message.bot, conn, llm, profile,
                                             call.from_user.id, today, _rng, morning=False,
                                             limit=daily.MAX_TASKS_PER_DAY)
        if result == "limit":
            await call.message.answer(daily.TEXT_MORE_LIMIT)
        elif result == "nothing":
            await call.message.answer(daily.TEXT_MORE_EMPTY)
        elif result == "failed":
            await call.message.answer(daily.TEXT_CAPTURE_FAILED)
    await call.answer()


async def _capture(message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                   profile: LanguageProfile, text: str, user_id: int) -> None:
    """`message` — куда отвечать; `user_id` — чей словарь (у call.message from_user = бот)."""
    if not text:
        await message.answer(daily.TEXT_EMPTY_CAPTURE)
        return
    items, error = await daily.capture_items(conn, llm, profile, user_id, text)
    if error:
        await message.answer(error)
        return
    if not items:
        await message.answer(daily.TEXT_NOTHING_FOUND)
        return
    async with daily.user_lock(user_id):   # два пересланных подряд не должны делить seq
        data = await state.get_data()
        seq = data.get("seq", 0)
        pending = data.get("pending", {})
        numbered = []
        for item in items:
            seq += 1
            pending[str(seq)] = item
            numbered.append((seq, item))
        await state.update_data(pending=pending, seq=seq)   # резервируем токены ДО отправки
    for seq, item in numbered:
        text_out = formatting.card_preview(item)
        if item.get("usage"):
            text_out += f"\n💬 {formatting.esc(item['usage'])}"
        await message.answer(text_out, parse_mode="HTML", reply_markup=keyboards.take_keyboard(seq))


@router.callback_query(StateFilter(None), F.data.startswith("take:yes:"))
async def on_take_yes(call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
                      profile: LanguageProfile) -> None:
    data = await state.get_data()
    pending = data.get("pending", {})
    item = pending.pop(call.data.split(":")[2], None)
    await state.update_data(pending=pending)
    if item is None:
        await call.answer("Эта карточка уже неактивна 🙂")
        return
    if db.card_exists(conn, call.from_user.id, item["word"]):
        await _finish(call, f"«{item['word']}» уже есть в твоём словаре 🙂")
        return
    db.add_card(conn, user_id=call.from_user.id, kind=item["kind"], word=item["word"],
                translation=item["translation"], transcription=item["transcription"],
                example=item["example"], example_translation=item["example_translation"],
                enriched=True, today=date.today(), context=item["context"])
    db.reset_missed(conn, call.from_user.id)
    await _finish(call, daily.TEXT_SAVED)


@router.callback_query(StateFilter(None), F.data.startswith("take:no:"))
async def on_take_no(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending", {})
    if pending.pop(call.data.split(":")[2], None) is None:
        await call.answer("Эта карточка уже неактивна 🙂")
        return
    await state.update_data(pending=pending)
    await _finish(call, "Ок, пропускаю 🙂")


async def _finish(call: CallbackQuery, text: str) -> None:
    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text)
    await call.answer()


@router.callback_query(StateFilter(None), F.data.startswith("clarify:"))
async def on_clarify(call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                     profile: LanguageProfile) -> None:
    _, action, seq = call.data.split(":")
    data = await state.get_data()
    pending = data.get("pending", {})
    entry = pending.pop(seq, None)
    await state.update_data(pending=pending)
    await _drop_markup(call)
    if entry is None:
        await call.answer("Уже неактивно 🙂")
        return
    if action == "capture":
        await _capture(call.message, state, conn, llm, profile, entry["text"], call.from_user.id)
    else:
        task = db.open_task(conn, call.from_user.id)
        if task is None or task["id"] != entry["task_id"] or task["status"] != db.TASK_OPEN:
            await call.message.answer(daily.TEXT_CLARIFY_STALE)
        else:
            await daily.answer_task(call.message.bot, conn, llm, profile, call.from_user.id,
                                    entry["text"], date.today(), giveup=False,
                                    task_id=entry["task_id"])
    await call.answer()


@router.message(StateFilter(None), F.text, ~F.text.in_(keyboards.MENU_BUTTONS))
async def on_free_text(message: Message, state: FSMContext, conn: sqlite3.Connection, llm: LLM,
                       profile: LanguageProfile) -> None:
    uid = message.from_user.id
    task = db.open_task(conn, uid)
    verdict = daily.classify_incoming(message.text, forwarded=message.forward_origin is not None,
                                      has_open_task=task is not None)
    if verdict == "ignore":
        return
    if verdict == "capture":
        await _capture(message, state, conn, llm, profile, daily.strip_capture_prefix(message.text),
                       uid)
    elif verdict in ("giveup", "answer"):
        accepted = await daily.answer_task(message.bot, conn, llm, profile, uid, message.text,
                                           date.today(), giveup=(verdict == "giveup"),
                                           task_id=task["id"])
        if not accepted:
            await message.answer(daily.TEXT_CLARIFY_STALE)   # утро успело сменить задание
    else:  # clarify
        data = await state.get_data()
        seq = data.get("seq", 0) + 1
        pending = data.get("pending", {})
        pending[str(seq)] = {"text": message.text, "task_id": task["id"]}
        await state.update_data(pending=pending, seq=seq)
        await message.answer(daily.TEXT_CLARIFY, reply_markup=keyboards.clarify_keyboard(seq))


@router.message(StateFilter(None), ~F.text)
async def reject_non_text(message: Message) -> None:
    await message.answer(daily.TEXT_ONLY_TEXT)


@router.callback_query(F.data.startswith(("more:", "take:", "clarify:")))
async def daily_button_inside_mode(call: CallbackQuery) -> None:
    """Кнопка daily нажата внутри режима добавления/тренировки (StateFilter(None) не
    пропустил) — не оставляем «крутилку», подсказываем выйти в меню."""
    await call.answer("Сначала выйди из режима — нажми любую кнопку меню 🙂", show_alert=False)
```

**Важно про `pending`:** в нём теперь живут три типа записей (превью add, превью capture — оба словари карточки; запись clarify — `{"text", "task_id"}`), но ключи `seq` монотонны и общие, а каждый callback-префикс читает только свои — коллизий нет. `leave_modes` по-прежнему сбрасывает `pending={}` при нажатии меню — превью и уточнения протухают, как и раньше.

**Тесты хендлеров** — добавить в `tests/test_daily_capture.py` (стиль `tests/test_handlers_quota.py`: aiogram-объекты — `MagicMock`/`AsyncMock`, вызов напрямую):

```python
from unittest.mock import AsyncMock, MagicMock

from handlers import add as add_handlers
from handlers import daily as daily_handlers


def _call(data, user_id=U, text_state=None):
    call = MagicMock()
    call.data = data
    call.from_user.id = user_id
    call.message.answer = AsyncMock()
    call.message.edit_text = AsyncMock()
    call.message.edit_reply_markup = AsyncMock()
    call.answer = AsyncMock()
    return call


def _state(data):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data)
    state.update_data = AsyncMock()
    return state


async def test_on_more_stale_date_answers_without_sending(conn, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(daily, "send_daily_task", send)
    monkeypatch.setattr(daily_handlers, "date", SimpleNamespace(today=lambda: date(2026, 10, 6)))
    call = _call("more:2026-10-05")
    await daily_handlers.on_more(call, conn, None, EN)
    send.assert_not_awaited()
    assert call.message.answer.await_args.args[0] == daily.TEXT_MORE_STALE


async def test_on_more_passes_limit_and_reports_results(conn, monkeypatch):
    for result, text in (("limit", daily.TEXT_MORE_LIMIT), ("nothing", daily.TEXT_MORE_EMPTY),
                         ("failed", daily.TEXT_CAPTURE_FAILED)):
        send = AsyncMock(return_value=result)
        monkeypatch.setattr(daily, "send_daily_task", send)
        monkeypatch.setattr(daily_handlers, "date", SimpleNamespace(today=lambda: date(2026, 10, 5)))
        call = _call("more:2026-10-05")
        await daily_handlers.on_more(call, conn, None, EN)
        assert send.await_args.kwargs["limit"] == daily.MAX_TASKS_PER_DAY
        assert call.message.answer.await_args.args[0] == text


async def test_on_clarify_answer_with_stale_task_refuses(conn, monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(daily, "answer_task", answer)
    cid = db.add_card(conn, user_id=U, kind="phrase", word="x", translation="п", transcription="t",
                      example="e", example_translation="э", enriched=True, today=date(2026, 10, 1))
    live = db.create_task(conn, user_id=U, card_id=cid, kind="compose", sentence=None, sentence_ru=None,
                          phrase_form=None, from_example=False, today=date(2026, 10, 5), morning=True)
    call = _call("clarify:answer:4")
    state = _state({"pending": {"4": {"text": "старый ответ", "task_id": live - 1}}})
    await daily_handlers.on_clarify(call, state, conn, None, EN)
    answer.assert_not_awaited()
    assert call.message.answer.await_args.args[0] == daily.TEXT_CLARIFY_STALE


async def test_on_clarify_capture_uses_user_id_not_bot(conn, monkeypatch):
    seen = []

    async def fake_capture_items(conn_, llm, profile, user_id, text):
        seen.append(user_id)
        return [], None
    monkeypatch.setattr(daily, "capture_items", fake_capture_items)
    call = _call("clarify:capture:4", user_id=U)
    call.message.from_user.id = 999999   # бот
    state = _state({"pending": {"4": {"text": "heads-up", "task_id": 1}}})
    await daily_handlers.on_clarify(call, state, conn, None, EN)
    assert seen == [U]


async def test_free_text_plus_only_asks_for_word_without_gemini(conn, monkeypatch):
    items = AsyncMock()
    monkeypatch.setattr(daily, "capture_items", items)
    message = MagicMock()
    message.text = "+"
    message.forward_origin = None
    message.from_user.id = U
    message.answer = AsyncMock()
    await daily_handlers.on_free_text(message, _state({}), conn, None, EN)
    items.assert_not_awaited()
    assert message.answer.await_args.args[0] == daily.TEXT_EMPTY_CAPTURE


async def test_save_yes_resets_missed_only_for_daily_profile(conn, monkeypatch):
    db.bump_missed(conn, U); db.bump_missed(conn, U)
    card = {"kind": "word", "word": "mesa", "translation": "стол", "transcription": "м",
            "example": "e", "example_translation": "э"}
    call = _call("save:yes:1")
    state = _state({"pending": {"1": dict(card)}})
    await add_handlers.save_yes(call, state, conn, PROFILES["es"])
    assert db.get_daily_state(conn, U)["missed_streak"] == 2      # es: не трогаем
    call = _call("save:yes:2")
    state = _state({"pending": {"2": {**card, "word": "silla"}}})
    await add_handlers.save_yes(call, state, conn, PROFILES["en"])
    assert db.get_daily_state(conn, U)["missed_streak"] == 0      # en: сброшен
```

(`SimpleNamespace`, `date`, `PROFILES` уже импортированы в начале файла; `daily_handlers.date` патчится, потому что хендлер берёт `date.today()` из своего модуля.)

- [ ] **Step 6: `bot.py`**

```python
import daily
from handlers import add, menu, training
from handlers import daily as daily_handlers


def prepare_db(conn) -> None:
    db.init_db(conn)
    db.release_stale_grading(conn)   # оценок в полёте на старте нет — зомби снимаем


BASE_ROUTERS = (menu.router, add.router, training.router)
DAILY_ROUTER = daily_handlers.router


def build_dispatcher(*, conn, llm, profile: languages.LanguageProfile,
                     base_routers=BASE_ROUTERS, daily_router=DAILY_ROUTER) -> Dispatcher:
    """Роутеры в фиксированном порядке; daily — ПОСЛЕДНИМ и только для профиля с daily_practice.

    Роутеры инжектируются ради тестов: aiogram не даёт подключить один Router к двум Dispatcher'ам.
    """
    dp = Dispatcher(storage=MemoryStorage())
    dp["conn"] = conn
    dp["llm"] = llm
    dp["profile"] = profile
    for router in base_routers:
        dp.include_router(router)
    if profile.daily_practice:
        dp.include_router(daily_router)   # ловит свободный текст вне режимов
    return dp


def start_daily_loop(bot, conn, llm, profile, cfg) -> asyncio.Task | None:
    """Таймер только при обоих гейтах (профиль + DAILY_AT). Ссылку на task держит вызывающий."""
    if not daily.should_start_loop(profile, cfg):
        return None
    return asyncio.create_task(daily.daily_loop(bot, conn, llm, profile, cfg))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = config.load()
    conn = db.connect(cfg.db_path)
    prepare_db(conn)
    gemini_client = genai.Client(api_key=cfg.gemini_api_key,
                                 http_options=genai_types.HttpOptions(timeout=30_000))
    models = (cfg.gemini_model,)
    if cfg.gemini_fallback_model:
        models += (cfg.gemini_fallback_model,)
    llm = llm_service.LLM(client=gemini_client, models=models)
    profile = languages.PROFILES[cfg.bot_lang]

    bot = Bot(token=cfg.telegram_token)
    dp = build_dispatcher(conn=conn, llm=llm, profile=profile)
    dp.message.filter(F.from_user.id.in_(cfg.allowed_user_ids))
    dp.callback_query.filter(F.from_user.id.in_(cfg.allowed_user_ids))

    daily_task = start_daily_loop(bot, conn, llm, profile, cfg)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        if daily_task is not None:
            daily_task.cancel()
            await asyncio.gather(daily_task, return_exceptions=True)
```

(Фильтры доступа ставятся на `dp` после `build_dispatcher`, как и раньше — порядок роутеров и фильтров не меняется.)

- [ ] **Step 7: `handlers/add.py::save_yes`**

```python
@router.callback_query(F.data.startswith("save:yes:"))
async def save_yes(
    call: CallbackQuery, state: FSMContext, conn: sqlite3.Connection,
    profile: LanguageProfile,
) -> None:
    ...  # без изменений до db.add_card
    db.add_card(...)  # как было
    if profile.daily_practice:
        db.reset_missed(conn, call.from_user.id)   # добавила слово любым путём → тихий режим снят
    await _finish_preview(call, "Сохранено! ✅ Пиши следующее 🙂")
    await call.answer()
```

Проверить `tests/test_handlers_quota.py`: если там `save_yes` вызывается напрямую — добавить `profile=PROFILES["es"]` в вызов.

- [ ] **Step 8: Прогнать тесты и импорт**

Run: `.venv/bin/pytest -q` → зелёные; `.venv/bin/python -c "import bot"` ок.

- [ ] **Step 9: Commit**

```bash
git add handlers/daily.py handlers/add.py bot.py daily.py keyboards.py \
        tests/test_daily_capture.py tests/test_bot_wiring.py tests/test_keyboards.py
git commit -m "daily: capture flow, handlers router (/next, more, take, clarify, free text), bot wiring, reset_missed in add"
```

---

### Task 14: Документы, дельты спеки, финальная проверка ветки

**Files:**
- Modify: `AGENTS.md`, `docs/superpowers/specs/2026-09-30-daily-practice-design.md`, `docs/superpowers/deploy.md`, `.env.example` (проверить), vault `Projects/Spanish Bot/AGENTS.md` (вне репо — отдельный коммит из корня волта, с разрешения Victoria)

- [ ] **Step 1: Спека — пост-имплементационные дельты** (раздел «Провенанс», новый пункт)

```markdown
- 2026-10-XX — реализация по плану `plans/2026-10-02-daily-practice.md`. Дельты
  к спеке: (а) `send_daily_task` возвращает и `"failed"` (ошибка отправки — `/next`
  отвечает «попробуй позже», а не «нечего повторять») и `"limit"` (потолок дня
  проверяется внутри лока по параметру `limit`, не в хендлере; утро не истекает
  задачу, выданную сегодня через `/next`); (б) fallback-пример карточки
  пишется в `daily_tasks.sentence` с флагом `from_example=1` (нужен для оценки
  gap/listen и для повтора по `/next`), из `recent_sentences` исключается;
  (в) `compose_hinted` без озвучки показывает предложение текстом с пометкой 🔇;
  (г) кнопка «Ещё одно» едет `reply_markup`'ом на голосовом сообщении результата;
  (д) `voice.send_card_voice*` получили `reply_markup`; (е) подписи голосовых
  обрезаются до 1024 символов (`fit_caption`), сбор фраз идёт под per-user локом.
```

Статус спеки: `**Статус:** реализовано (ветка daily-practice, 2026-10-XX); деплой — отдельно`.

- [ ] **Step 2: `AGENTS.md` репо**

- Раздел «Структура»: добавить строки `daily.py`, `handlers/daily.py`, `services/sentences.py`, `services/capture.py`; у `db.py` — `+ daily_tasks/daily_state`; у `languages.py` — `+ daily_practice и три промпта EN`; у `voice.py` — `send_text_voice`, `send_card_voice_to`.
- «Ключевые решения»: «Pull-режим, без пуш-напоминаний» → «**es — pull-режим без пуш.** **en — одно утреннее задание в день** (`DAILY_AT` в `.env`, цепочка «Ещё одно» до 3/день, тихий режим после 3 пропусков) + сбор фраз из пересланного текста с контекстом. Спека `docs/superpowers/specs/2026-09-30-daily-practice-design.md`».
- «Рантайм-грабли»: добавить «`daily_tasks.status='grading'` на старте — зомби, снимается `release_stale_grading`; `DAILY_AT` вне 05:00–13:59 — WARNING, не ошибка; `generate_json` отдаёт только dict — массивы заворачивать в объект».
- «Статус»: число тестов (фактическое после прогона), «реализовано, не задеплоено».

- [ ] **Step 3: `deploy.md`** — подраздел «Ежедневная практика (en-бот)»: добавить в `.env` английского юнита `DAILY_AT=09:30`, `DAILY_TZ=Europe/Madrid`; рестарт юнита; проверка в `journalctl`: строка WARNING при вечернем времени; ручной сценарий из спеки (DAILY_AT на 2 минуты вперёд → задание; ответ; «не помню»; `/next`; пересланное сообщение → превью → «Беру» → завтра утром compose_hinted). Маминому юниту ничего не добавлять.

- [ ] **Step 4: Финальный прогон**

Run: `.venv/bin/pytest -q` — записать число; `.venv/bin/python -c "import bot"`; `git status` чистый; `git log --oneline main..daily-practice` — 14 коммитов (±).

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/superpowers/specs/2026-09-30-daily-practice-design.md docs/superpowers/deploy.md
git commit -m "docs: daily practice — spec deltas, AGENTS structure/decisions/pitfalls, deploy notes"
```

- [ ] **Step 6: Передать на финальное ревью ветки** (superpowers:requesting-code-review / финальный ревьюер по выбранному методу исполнения). Деплой на VPS — отдельное решение Victoria, не часть плана.

---

## Self-review (выполнен автором плана)

- **Покрытие спеки:** данные (T1–T3), чистая логика и тексты (T4, T10), расписание и конфиг (T5), профиль (T6), сервисы (T7–T8), озвучка (T9), выдача/цикл (T11), ответ (T12), сбор + роутер + проводка + `add.py` (T13), документы (T14). Непокрытого в спеке не осталось; «Ручная проверка на сервере» — в T14/deploy.md, не в коде.
- **Отклонения от спеки** перечислены в Global Constraints и фиксируются в спеке в T14.
- **Типы и имена:** `Prepared`/`Graded` dataclass'ы; статусы — константы `db.TASK_*`; `send_daily_task` → `"sent"|"resent"|"nothing"|"failed"`; `reply_markup` в сигнатурах `voice.*` с T9 (нужен T12); `formatting.esc` модульная с T1 (нужна T10/T13).
- **Review Focus → тесты:** 1 → `test_card_deleted_during_grading` (T12); 2 → `TEXT_EMPTY_CAPTURE` в `_capture` (T13; покрыть юнитом `classify_incoming("+", …) == "capture"` + `strip_capture_prefix("+") == ""` в T4); 3 → `test_blank_out_case_insensitive_first_occurrence` (T4) + `answers_match` фолдит регистр; 4 → `test_run_morning_isolates_users` (T11); 5 → `test_resend_listen_with_tts_failure_falls_to_text` (T11).

## Провенанс плана

- 2026-10-03 — план написан, самопроверка (esc/field вынесены в T1, reply_markup в T9, тест гонки упрощён).
- 2026-10-03 — **ревью плана, раунд 1 (Codex, read-only): 4 P1 / 11 P2 / 2 P3, все приняты.**
  P1: моки в тестах T12 трогали sqlite из worker-потока `to_thread` (→ подмена
  `asyncio.to_thread` на inline в двух тестах); повтор `listen` при сбое TTS понижался
  только в чате, не в БД (→ `db.set_task_kind`); callback-хендлеры без `StateFilter(None)`
  (→ добавлен + ответ-подсказка при нажатии внутри режима); «Не нашёл» — прошедшее время
  1-го лица (→ «Не вижу»). P2: `recent_sentences` терял ответные предложения fallback-задач;
  закэшированный file_id вне try; `_capture` брал id бота из `call.message` (→ явный
  `user_id`); `default_context` > 60 символов; `check_sentence` не требовал `corrected`
  при fix; «не помню» показывал блок без примера (→ `card_preview`); `on_more` молчал на
  `failed`; лимит дня проверялся вне лока (→ параметр `limit`, результат `"limit"`);
  коллизия имени tmp-файла (→ `NamedTemporaryFile`); `reset_missed` пропускался на
  удалённой карточке; не было тестов хендлеров (→ 5 тестов в T13). P3: строгий `HH:MM`
  регэкспом; `avoid` для `check_sentence` — последние 3.
- 2026-10-03 — **ревью плана, раунд 2 (Codex): 1 P1 / 10 P2 / 1 P3, все приняты.** P1:
  `field` без импорта (→ `from dataclasses import dataclass, field`). P2: утро истекало
  задачу, выданную сегодня через `/next` (→ `sent_on == today` → `nothing`); fallback
  `compose_hinted` без озвучки слова шёл без 🔇 (→ пометка и в этой ветке); подписи без
  лимита 1024 (→ `fit_caption`); лимит дня после повтора (→ лимит первым в локе); гонка
  `seq`/`pending` при двух пересланных (→ лок + резерв токенов до отправки); тест
  «карточка удалена до ответа»; тест «утро ждёт лок во время оценки»; тест хендлера на
  пустой `+`; `bot.py` тестировался тривиально (→ `build_dispatcher`/`start_daily_loop`
  с тестами порядка роутеров и гейтов); дельты спеки без `"limit"`. P3: дедуп внутри
  одного ответа `extract`.
- 2026-10-03 — **ревью плана, раунд 3 (Codex, финальный): 2 P1 / 3 P2, все приняты.** P1:
  `build_dispatcher` дважды в тестах → aiogram «Router is already attached» (→ роутеры
  инжектируются, в тестах свежие `Router()`); хендлер выбирал открытую задачу вне лока, а
  `answer_task` брал «текущую» (→ `task_id` сквозь лок, чужой/истёкший → отказ). P2:
  fallback `compose_hinted` ставил 🔇 даже при удачной озвучке слова; регенерация
  ответного предложения ловила не все исключения (→ `Exception`, вердикт сохраняется);
  `fit_caption` мог дать 1025 символов и порвать тег (→ деградация в plain text с
  гарантией лимита). **Ревью остановлено на трёх раундах** — дальше код и тесты.
  План ждёт утверждения Victoria и выбора способа исполнения.

---

## Приложение A. Технический дизайн (перенесён из спеки 2026-10-04)

> Решение Victoria 2026-10-04: спека пишется с продуктовой точки зрения простыми словами, вся техника живёт в плане. Разделы ниже перенесены из `specs/2026-09-30-daily-practice-design.md` без изменений (состояние спеки после реализации 2026-10-03 и правок 2026-10-04); заголовки понижены на один уровень. Актуальное поведение кода — в самих задачах плана и в коде; при расхождении с кодом прав код.

### Архитектура

Принцип репо: хендлеры тонкие, логика в чистых модулях, чистое покрыто тестами
без Telegram и без живого Gemini.

```
daily.py                 чистая часть: task_kind(), quiet-mode решение,
                         next_fire()/delay (из спеки reminder 06-23) + async
                         daily_loop / send_daily_task (IO)
services/sentences.py    Gemini: make_sentence(), check_sentence(); валидация
services/capture.py      Gemini: extract() — 1–3 фразы из свободного текста
handlers/daily.py        /next, ответ на открытое задание, сбор из свободного
                         текста, кнопки take:yes/no; роутер подключается ПОСЛЕДНИМ
db.py                    +cards.context (миграция), таблицы daily_tasks, daily_state
languages.py             +daily_practice флаг и три промпта/схемы в EN; ES без изменений
voice.py                 send_text_voice(bot, chat_id, mp3_path, caption) — отправка
                         готовой озвучки; синтез делает вызывающий (см. Выдача)
formatting.py            card_preview: строка «📍 контекст: …» если есть (единственное
                         место, где контекст печатается)
config.py                DAILY_AT / DAILY_TZ / DAILY_EXCLUDE_IDS (гейт — DAILY_AT)
bot.py                   include daily.router последним; create_task(daily_loop)
```

#### Данные (`db.py`)

- **`cards.context TEXT`** — короткий русский контекст «откуда» («рабочий созвон,
  релиз»). Добавляется миграцией по образцу `_migrate_column_names`: гард по
  `PRAGMA table_info`, `ALTER TABLE cards ADD COLUMN context TEXT`, идемпотентно.
  У маминой базы колонка тоже появится (пустая) — это не меняет ни текстов, ни
  запросов её бота; инвариант es — про промпты и строки, не про схему.
- **`daily_tasks`** — открытые и прошлые задания. Хранится в БД, а не в FSM:
  MemoryStorage теряется при рестарте, а задание живёт до утра.

  ```sql
  CREATE TABLE IF NOT EXISTS daily_tasks (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id      INTEGER NOT NULL,
      card_id      INTEGER NOT NULL,
      kind         TEXT NOT NULL,      -- compose_hinted|recall|gap|listen|compose
      sentence     TEXT,               -- английское предложение задания (NULL только для compose
                                       -- и для fallback-заданий без предложения, см. sentences)
      sentence_ru  TEXT,
      phrase_form  TEXT,               -- точная форма фразы в sentence (для gap)
      reply_sentence TEXT,             -- ответное предложение бота (compose*), для avoid
      sent_on      TEXT NOT NULL,      -- ISO date выдачи
      morning      INTEGER NOT NULL DEFAULT 0,  -- 1 = выдано таймером; только такие считаются в пропуски
      status       TEXT NOT NULL DEFAULT 'open',  -- open|grading|answered|expired
      answered_ok  INTEGER             -- 1/0 после оценки, NULL пока не оценено
  );
  -- гард «не более одной открытой задачи на пользователя» на уровне БД
  CREATE UNIQUE INDEX IF NOT EXISTS daily_tasks_one_open
      ON daily_tasks(user_id) WHERE status = 'open';
  CREATE TABLE IF NOT EXISTS daily_state (
      user_id       INTEGER PRIMARY KEY,
      missed_streak INTEGER NOT NULL DEFAULT 0,
      last_sent_on  TEXT
  );
  ```
  Функции: `open_task(conn, user_id)`, `create_task(...)`, `recent_sentences(conn,
  card_id, n=6)` — последние `sentence` **и** `reply_sentence` этой карточки
  (оба непустые), чтобы «каждый раз новое» касалось и ответных предложений;
  `get_daily_state / bump_missed / reset_missed / set_last_sent`;
  `count_tasks_on(conn, user_id, today)` — сколько заданий выдано сегодня (для
  лимита цепочки «Ещё одно», константа `MAX_TASKS_PER_DAY = 3`; `/next` в лимит не
  упирается — явная команда, нужна для живой проверки);
  `pick_due_card(conn, user_id, today)` — только `enriched = 1 AND translation
  IS NOT NULL AND translation != ''` (карточка без перевода не годится ни для
  одного kind; в живых базах таких нет, `add_card` всегда пишет `enriched=True`,
  но схема не связывает эти поля, поэтому фильтр на оба) и только карточки,
  **созданные до сегодня** (`created_at < today`: сохранённое утром до таймера или
  днём не должно прийти «через пять минут» — обещано «завтра утром», и таблица
  ступеней считает от дня 1); **сначала новые** (`interval_days = 0`, самая старая
  по `id`), потом самая просроченная (`ORDER BY interval_days = 0 DESC, due_at, id
  LIMIT 1`). Новая — первой, потому что её контекст ещё тёплый, а своё предложение
  в день 1 — самая сильная зацепка (решение брейншторма).

  **Жизненный цикл задачи** (все переходы — условные `UPDATE … WHERE status=?`,
  результат по `rowcount`):
  - `open_task(conn, user_id)` возвращает задачу в статусе `open` **или**
    `grading` («активная»); `grading` живёт только секунды внутри одного ответа
    (под тем же per-user локом, что и выдача — см. ниже), а зомби после падения
    бота снимает **`release_stale_grading(conn)`** на старте (`grading → open`
    для всех: раз бот стартует, никакой оценки в полёте нет);
  - `open → expired`: **`expire_task(conn, task_id) -> bool`** (возвращает
    `morning` истёкшей задачи) — утренняя выдача (только из `open`: `grading` под
    локом утро не увидит); `bump_missed` вызывается **только если истекла
    утренняя** — задание из «Ещё одно» или `/next`, оставленное без ответа, не
    считается пропуском;
  - `open → grading`: **`claim_task(conn, task_id) -> bool`** — первый ответ
    забирает задачу; второй быстрый ответ получает `False` и молча игнорируется;
  - `grading → open`: **`release_task(conn, task_id)`** — оценка не состоялась
    (`SentenceError`/`GradingError`/неожиданное исключение): ответ «Не получилось
    проверить сейчас 😕 Напиши ещё раз через минутку», задача снова открыта, SRS
    не тронут;
  - `grading → answered`: **`finish_task(conn, task_id, ok: bool)`** — после
    оценки, пишет `answered_ok`; вызывается ПОСЛЕ `update_review` (если он нужен).
    **Порядок в хендлере ответа — строго:** `claim` → оценка (`await`) →
    перечитать карточку → `update_review` (если нужно) → `finish_task` →
    **только потом** отправка ответа пользователю. Сбой отправки после
    `finish_task` — лог; SRS уже обновлён, задача закрыта, квота второй раз не
    тратится, результат пользователь увидит по следующему заданию. Бот, упавший
    посреди оценки, оставляет `grading` — снимается на старте (см. выше).
    **Весь хендлер ответа выполняется под тем же per-user `asyncio.Lock`, что и
    выдача**: утренний таймер, сработавший во время `await` оценки, дождётся
    `finish_task` и только потом решит, что истекло, а что нет.
  Не более одной активной задачи на пользователя — частичный уникальный индекс выше
  (страховка; покрывает `open`, а `grading` существует только под локом) плюс лок
  на выдаче и на ответе (см. Выдача). `update_review` коммитит сам (общий
  с es код) — транзакцию через два вызова не строим.
  Гонка «таймер + `/next`» (оба увидели «нет открытой», оба ждут `make_sentence`)
  закрывается **per-user `asyncio.Lock`** (dict в `daily.py`) вокруг всей выдачи:
  проверка открытой → генерация → отправка → `create_task`. Индекс — страховка,
  если лок обойдут.

**Ёмкость.** Каждая фраза до ступени 60 стоит 6 заданий; цель Victoria — **пара
новых фраз в неделю** = ~12 заданий в неделю. Одно утреннее задание даёт 7 —
хвост рос бы на ~5 в неделю. Поэтому цепочка «Ещё одно» до 3 заданий в день
(потолок 21 в неделю): при двух фразах в неделю достаточно жать «ещё» примерно
через день. Уведомление остаётся одно — условие «отвечать проще, чем не отвечать»
не трогаем. Чекпоинт через месяц: если хвост просроченных > 20 при регулярных
ответах — смотреть, где затык (не отвечает на утренние или не жмёт «ещё»).

#### Выбор задания (`daily.py`, чистое)

- **`task_kind(interval_days: int, rng: random.Random) -> str`** — таблица выше;
  для `interval_days >= 30` — `rng.choice(["recall", "gap", "listen", "compose"])`.
  Значения `interval_days`, не совпадающие со ступенями (не должно быть, но
  БД правится руками), округляются вниз до ближайшей ступени.
- **`should_send(state, today) -> bool`** — тихий режим: `missed_streak < 3` →
  `True`; иначе `True` только если `last_sent_on` пуст или `today -
  last_sent_on >= 7 дней`.
- **`next_fire`, delay в абсолютном времени** — 1:1 из спеки
  `2026-06-23-daily-reminder-design.md` (там же грабля DST: delay считать через
  `timestamp()`, target строить свежим aware datetime на календарный день).
- **`blank_out(sentence, phrase_form) -> str`** — регистронезависимая замена первого
  вхождения `phrase_form` на `___`; если вхождения нет — `ValueError` (вызывающий
  берёт fallback, см. ниже).
- **`listen_ok(answer, sentence) -> bool`** — нормализация (lower, дефис → пробел,
  типографский `’` → `'`, убрать пунктуацию **кроме апострофа**, схлопнуть
  пробелы), затем **пословно**:
  одинаковое число слов **и** для каждой пары слов `SequenceMatcher.ratio() >= 0.8`
  (или равенство). Счёт слов ловит выпавшее слово; пословный порог ловит опечатку
  внутри слова как «почти» (ok), но не пропускает смысловую замену: апостроф
  сохранён, поэтому `can` vs `can't` = 0.75 → не ok (посимвольный ratio по всему
  предложению пропустил бы это как 0.98). Без LLM: детерминированно, без квоты.
  Пороги — константы, тестируются на парах: «heads up» vs «heads-up» → ok;
  «Thursady» вместо «Thursday» → ok; выпавший артикль → не ok; `can`/`can't` →
  не ok; другое предложение → не ok. Известный компромисс: `cannot`/`can not` —
  не ok (число слов), ответ всё равно показывает текст.

#### Генерация предложений (`services/sentences.py`)

- **`make_sentence(llm, profile, card, kind, avoid: list[str]) -> dict`** →
  `{sentence, sentence_ru, phrase_form}`. Промпт `profile.sentence_system`: American
  English, уровень B2, фраза употреблена естественно, **тема — `card.context`**
  (у карточек из сбора он всегда непустой, см. capture; у карточек, добавленных
  старым режимом `➕`, его нет — тогда тема из `card.example`, а не «нейтральная»:
  пример хоть как-то привязан к слову), не повторять предложения из `avoid`
  (последние из `daily_tasks`, включая ответные), `phrase_form` — точная подстрока
  предложения. Валидация: `phrase_form` входит в `sentence` (регистронезависимо),
  иначе второй вызов; после второго провала или `QuotaExceededError` —
  **fallback-цепочка**: (1) пример карточки, если `example` и
  `example_translation` непустые **и `card.word` входит в `example`**
  (регистронезависимо) — тогда `phrase_form = card.word`; (2) иначе **задание без
  предложения**: `gap`/`listen` понижаются до `recall`; `compose_hinted`
  показывает блок карточки с озвучкой самого слова (`voice.send_card_voice`, кэш
  file_id) вместо предложения; после ответа на `recall` без предложения — тоже
  озвучка слова. Ошибок пользователю не показываем — у неё всегда есть задание.
  **Оговорённое исключение из «каждый раз новое предложение»:** fallback-пример
  один и тот же; это деградация при недоступном Gemini, а не штатный режим, и
  пример в `daily_tasks.sentence` не пишется (`NULL`), чтобы не засорять `avoid`.
- **`check_sentence(llm, profile, card, answer, avoid) -> dict`** →
  `{verdict: good|fix|off, corrected, note, reply_sentence, reply_sentence_ru,
  reply_phrase_form}`. Валидация ответного предложения как у `make_sentence`
  (`reply_phrase_form` входит в `reply_sentence`); если не прошла — **вердикт
  первого ответа фиксируется**, `check_sentence` второй раз НЕ вызывается
  (второй вызов мог бы вернуть другой verdict), а ответное предложение берётся
  одним вызовом `make_sentence(kind="reply")`; провал и там → пример карточки
  (если содержит фразу) или ответное предложение опускается.
  `good` — естественно и фраза употреблена; `fix` — фраза на месте, но есть
  ошибка (corrected + note что именно); `off` — фраза не использована или смысл
  не тот. `reply_sentence` — ответное предложение бота с той же фразой, **новое**
  (в промпте: не повторять предложение ученика и `avoid` = последние 3 из
  `daily_tasks`). Одним вызовом — экономия квоты. Оценка `ok = verdict in (good, fix)`.
- Оба — по образцу `grading.grade`: `generate_json` + до 2 попыток + проверка
  обязательных ключей; свои `SentenceError`. Ключи нейтральные, `llm_key_map`
  не нужен (en-профиль и так тождество).

#### Сбор (`services/capture.py`)

**`extract(llm, profile, text) -> list[dict]`** — Gemini получает свободный текст
(пересланное сообщение и/или её заметка) и возвращает массив до 3 элементов, каждый
как карточка обогащения (`kind, word, translation, transcription, example,
example_translation`) плюс **`context`** (≤ 60 символов, по-русски, «откуда/о чём»
— из текста сообщения, без выдумок; **всегда непустой**: если источник не
назван, контекст — тема самого сообщения («переписка о переносе релиза»); если
модель всё же вернула пусто — `extract` подставляет «из сообщения: «{первые 50
символов текста}»». Интент требует, чтобы каждая фраза жила в своём контексте, и
это гарантируется на входе, а не «по возможности») и **`usage`**
(короткая строка «обычно: give someone a heads-up», может быть пустой — идёт только в
превью, в БД не хранится). Промпт `profile.capture_system`: если в тексте есть явный
указатель («не поняла X», одиночное слово, кавычки) — ровно эта фраза; иначе — до
3 самых полезных для B2 кусков (фразы/коллокации, а не одиночные частотные слова).
Схема — **объект `{"items": ARRAY}`**, не голый массив: существующий
`llm_service.generate_json` возвращает только `dict` и отбрасывает массив как
`None` (`services/llm.py`), а менять его ради этого не хотим. `extract` берёт
`items[:3]`; пустой список → «Не нашёл, что взять 🙂 Напиши слово или фразу
явно». Дедуп каждого элемента через существующий `db.card_exists`.

#### Хендлеры (`handlers/daily.py`) — подключается ПОСЛЕДНИМ

Роутер только для профиля с `daily_practice=True`; в `bot.py`:
`if profile.daily_practice: dp.include_router(daily.router)`. Все хендлеры с
`StateFilter(None)` и `~F.text.in_(keyboards.MENU_BUTTONS)` — режимы добавления
и тренировок, меню и `/start` остаются как есть и имеют приоритет (их роутеры
раньше). Существующие три тренировки для англо-бота **не убираем**: они работают
на том же пуле просроченных карточек, ручная сессия тоже двигает SRS.

**Ручная тренировка и задание на одну карточку — не двойной SRS.** Карточка
задания остаётся в `get_due_cards` (общий с es код не трогаем). Если до ответа на
задание её уже прошли в ручном режиме, у неё `due_at > today`; тогда ответ на
задание **оценивается, но SRS не трогает** («практика», как повтор внутри сессии
в `session.advance`). Обратный порядок (сначала задание, потом ручная тренировка)
безопасен: после ответа карточка уже не due. Новая карточка, пройденная руками
до утра, пропустит `compose_hinted` и получит задание по своей ступени —
принимаем, ручные режимы для en остаются «клапаном», а не основным путём.

- **`/next`** — `send_daily_task(morning=False)`; всё, включая проверку открытой
  задачи, происходит **внутри её лока** (два `/next` подряд или `/next` +
  таймер не могут выдать две задачи: второй увидит открытую и просто повторит её
  вопрос). Результат: `sent` / `resent` / `nothing` — на `nothing` хендлер
  отвечает «Пока нечего повторять: все фразы ещё не подошли 🙂 Перешли
  что-нибудь новое.» (в отличие от утренней тишины: на явную команду бот всегда
  отвечает). Нужен для живой проверки и для «есть минутка сейчас».
- **Свободный текст, `StateFilter(None)`**, кроме текста, начинающегося с `/`
  (неизвестные команды игнорируются, как сейчас):
  1. `message.forward_origin is not None` **или текст начинается с `+`** → **сбор**
     всегда (пересланное — однозначно материал; `+` — явный способ добавить
     слово при открытом задании, `+` отрезается);
  2. иначе есть открытое задание:
     - `intents.is_giveup(text)` **и ≤ 4 слов** → «не помню»;
     - в тексте есть кириллица (ответы на все kind — английские; заметка
       «сказали heads-up, не поняла» иначе попала бы в giveup через «не поня» и
       сбросила бы SRS) → **уточнение**: «Это ответ на задание или новое слово?»
       с инлайн-кнопками «Ответ» / «Новое слово» (`clarify:answer:<seq>` /
       `clarify:capture:<seq>`; в FSM `pending` под `seq` кладутся текст **и
       `task_id`** открытой задачи; «Ответ» проходит только если задача с этим
       `task_id` всё ещё `open` — иначе «Это задание уже истекло, напиши ответ на
       новое 🙂» и запись выбрасывается: старый текст не должен оцениваться против
       новой карточки; протухшая кнопка → «уже неактивно»);
     - иначе → **ответ на задание**;
  3. иначе → **сбор**.
  Голосовые/фото вне режима — «Пока понимаю только текст: перешли сообщение или
  напиши фразу 🙂» (как `reject_non_text` в add).
- **Ответ на задание**: сначала `claim_task` (см. Данные; `False` → молча выйти),
  потом по `kind` (оценка — `await to_thread`, за это время карточку могла
  сдвинуть ручная тренировка, поэтому **перед SRS-шагом карточка перечитывается**
  `get_card` заново; `None` → «Эта фраза уже удалена», `finish_task(ok=False)`
  без SRS):
  - `compose_hinted`, `compose` → `check_sentence`; ответ: `good` «✅ Отлично,
    звучит естественно.», `fix` «✅ Почти. Лучше так: {corrected} ({note})», `off`
    «❌ Фраза тут не сработала: {note}.» — и во всех случаях 🔊 + текст
    `reply_sentence` («Моё в ответ: …»), если он есть. При `QuotaExceededError` —
    принимаем как ok=True без разбора («умная проверка недоступна — лимит»)
    — своё предложение ценно само по себе, не наказываем за лимит.
  - `recall` → как сейчас `check_translation`: `answers_match` → иначе
    `grading.grade(prompt_ru=card.translation, expected=card.word)`; затем 🔊 +
    текст предложения задания (сгенерировано при выдаче, прятали до ответа);
    без предложения — озвучка слова.
  - `gap` → `answers_match(answer, phrase_form)` — **только точная форма из
    предложения**; иначе `grading.grade(prompt_ru=sentence_ru,
    expected=phrase_form)` (словарная форма `give someone a heads-up` на месте
    `gave him a heads-up` — это `wrong`/`typo` по решению модели, не автозачёт);
    затем 🔊 + полное предложение.
  - `listen` → `listen_ok`; «✅ Всё верно: {sentence}» / «Почти. Было: {sentence}»;
    ok по порогу.
  - «не помню» в любом kind → «Ничего. {card_preview}» + 🔊 предложение (если
    есть, иначе слово) + «Вернусь с ней завтра.», ok=False.
  - После оценки: перечитать карточку; если `fresh["due_at"] <=
    today.isoformat()` (в БД `due_at` — ISO-строка, сравнение строк, как в
    `get_due_cards`) — `srs.next_interval(fresh["interval_days"], ok)` +
    `db.update_review` (без `retried`-гейта — сессии нет), иначе SRS не трогаем;
    затем `finish_task(ok)` (для `compose*` — с `reply_sentence`), `reset_missed`,
    **и только после этого** — отправка ответа (тексты выше). К сообщению с
    результатом добавляется инлайн-кнопка **«Ещё одно»** (`more:<today ISO>`),
    если `count_tasks_on(today) < MAX_TASKS_PER_DAY` **и** `pick_due_card` не
    пуст (проверка дешёвая, без Gemini). Исключение из оценки (`SentenceError`,
    `GradingError`, прочее) → `release_task` + «попробуй ещё раз» (см. Данные).
    Всё — под per-user локом.
- **Кнопка «Ещё одно»** (`more:<date>`): дата в callback ≠ сегодня → «Это было
  вчера 🙂 Утром пришлю новое», кнопка снимается; лимит на сегодня уже выбран →
  «На сегодня хватит, завтра продолжим 🙂»; иначе `send_daily_task(morning=False)`
  под локом (открытая задача уже есть → `resent`, см. `/next`). Результат
  `nothing` (очередь опустела между ответом и нажатием) → «Пока всё повторили 🎉».
  Кнопка снимается с сообщения после любого нажатия (`edit_reply_markup`).
- **Старый режим `➕ Добавить слово`** остаётся для en как есть; единственная
  правка в `handlers/add.py`: после сохранения при `profile.daily_practice` —
  `reset_missed` (добавила слово любым путём → тихий режим снимается). Для es
  ветка не выполняется.
- **Сбор:** `extract` в `asyncio.to_thread` → для каждого элемента превью
  (`card_preview` — он сам печатает «📍 контекст» — плюс строка «💬 {usage}») +
  инлайн «✅ Беру» / «❌ Не надо» с токеном `take:yes:<seq>` (тот же `pending`/`seq`
  механизм из `handlers/add.py`, тот же `leave_modes`-сброс). «Беру» →
  `db.add_card(..., context=…)` (новый параметр **`context: str | None = None`** —
  старый режим `➕` и es-бот вызывают как раньше) + `reset_missed`
  + «Сохранено ✅ — придёт завтра утром». Ошибки/квота — те же тексты, что в add.
  Голосовое-превью слова при сборе **не шлём** (у неё их до трёх, а озвучка ждёт
  задания завтра).

#### Утренняя выдача (`daily.py`, IO)

`send_daily_task(bot, conn, llm, profile, user_id, today, rng, *, morning: bool)
-> Literal["sent", "resent", "nothing"]`, **целиком под per-user `asyncio.Lock`**
(dict `user_id → Lock` в `daily.py`; лок покрывает проверку открытой задачи,
генерацию, отправку и запись):
1. открытая задача (`open` или зависшая `grading`) есть: при `morning=True` →
   `expire_task` + `bump_missed`; при `morning=False` (`/next`) → повторить её
   вопрос из сохранённых полей (без Gemini, без нового TTS для текстовых kind;
   для голосовых — повторный синтез) и вернуть `"resent"`;
2. при `morning=True`: `should_send` ложно → `"nothing"`;
3. `pick_due_card` пусто → `"nothing"` (утром — тишина, как в спеке reminder;
   `/next` сам отвечает текстом);
4. `kind = task_kind(card.interval_days, rng)`; для всех kind, кроме `compose`, —
   `make_sentence` (`to_thread`) с `avoid = recent_sentences` (для `recall`
   предложение прячется до ответа, для `compose` ответное предложение придёт из
   `check_sentence`); fallback без предложения — понижение kind (см. sentences);
5. для голосовых kind (`compose_hinted`, `listen`) — **синтез до отправки**:
   `tts.synthesize` во временный файл; `TTSError` → `listen` понижается до `gap`
   (предложение есть, звука нет), `compose_hinted` уходит текстом с пометкой
   «🔇 (озвучка временно недоступна)». Решение о kind принимается до отправки,
   поэтому отправляющая функция ничего не «глотает»;
6. **отправить одно сообщение**, и только после успешной отправки —
   `create_task(..., morning=morning)` + `set_last_sent` (только при
   `morning=True`) → `"sent"`. Сбой отправки
   (`TelegramForbiddenError`, `TelegramBadRequest`, сеть) → лог, задачи нет,
   счётчик пропусков не растёт (невидимая задача не должна включать тихий режим).

**Одно задание = одно сообщение Telegram** (одно уведомление; интент «бот не
долбит»). Там, где нужен голос, это **голосовое с `caption`** (`sendVoice`
поддерживает подпись до 1024 символов, HTML) — без отдельного текстового
сообщения. Ответы бота на её ответ — тоже одним сообщением (голосовое с подписью
или текст).

Сообщения с `<b>`/`<i>` идут с `parse_mode="HTML"` и экранированием подстановок
(паттерн `formatting.card_preview`); у бота нет дефолтного parse mode.
`voice.send_text_voice(bot, chat_id, mp3_path, caption=None, parse_mode=None)`
— только отправка готового файла через `bot.send_voice` (`BufferedInputFile`),
без кэша (каждое предложение новое); исключения Telegram пробрасывает — решение
о деградации принимает вызывающий (см. шаг 5 выше). Синтез — отдельно,
`tts.synthesize`, как и сейчас в `handlers/add.py`.
Озвучка **слова** с кэшем file_id (fallback без предложения, «не помню»): у
существующего `voice.send_card_voice` появляются необязательные `caption=None,
parse_mode=None`, прокинутые в `answer_voice`, плюс вариант для бот-инициированной
отправки **`voice.send_card_voice_to(bot, chat_id, conn, card, voice, caption=None,
parse_mode=None)`** на общей внутренней реализации (кэш file_id — тот же).
Существующие вызовы без caption не меняются (es-инвариант цел).

`daily_loop(bot, conn, llm, profile, cfg)` — как `reminder_loop` из спеки reminder:
`while True`: `next_fire` → `sleep(delay)` → для каждого `uid ∈ allowed −
exclude`: `send_daily_task(morning=True)` в своём `try/except` (падение одного не
валит остальных) → цикл. Задача держится ссылкой в `bot.py`, отменяется в
`finally` (паттерн из спеки reminder). Стартует **только при
`profile.daily_practice and cfg.daily_at`** — `DAILY_AT` в `.env` маминого бота
не включит ничего (второй гейт, см. Конфиг). Бот лежал в момент срабатывания →
день пропущен, догонять не пытаемся (`/next` всегда есть).

**Тексты выдачи** (гендер-нейтральные; одно сообщение на задание):
- `compose_hinted`: 🔊 sentence с подписью: блок карточки (фраза, перевод, IPA,
  контекст) + «Напиши своё предложение с <b>{word}</b> — про что-нибудь из
  твоей жизни.»
- `recall`: текст «Как сказать по-английски: «{translation}»?» + « (контекст:
  {context})» **только если контекст есть** (у карточек, добавленных старым
  режимом, он `NULL`; backfill вне scope) — без 🔊 до ответа (иначе ответ слышен).
- `gap`: текст «Вставь пропуск:\n<i>{blank_out(sentence)}</i>\n({sentence_ru})»
- `listen`: 🔊 sentence с подписью «Напиши то, что услышишь.»
- `compose`: текст «Напиши своё предложение с <b>{word}</b>.» — только фраза.

#### Профиль (`languages.py`)

`LanguageProfile` получает поля **с дефолтами** (ES не трогаем, байт-инвариант
цел): `daily_practice: bool = False`, `sentence_system: str = ""`,
`sentence_schema: dict | None = None`, `sentence_check_system: str = ""`,
`sentence_check_schema: dict | None = None`, `capture_system: str = ""`,
`capture_schema: dict | None = None`. EN: `daily_practice=True` + три промпта.
Требования к промптам: American English, B2, гендер-нейтральные `note`, темы
из `context`, «не повторяй» для `avoid`, `phrase_form` = точная подстрока.
Тест-инвариант es расширяется проверкой, что у ES `daily_practice is False`.

#### Конфиг (`config.py` / `.env`)

`DAILY_AT=HH:MM` (гейт: нет → цикл не стартует, остальное не читается; есть,
но кривое → `ValueError` при старте), `DAILY_TZ` (IANA, дефолт `Europe/Madrid`),
`DAILY_EXCLUDE_IDS` — правила и валидация 1:1 из спеки reminder (`REMINDER_*`),
включая «дневное время» (счёт «сегодня» — по серверной дате `date.today()`, как
в тренировках). Рекомендованное значение для англо-бота — утро (`09:30`): по плану
Victoria вечер мёртв. Часы вне `05:00–13:59` **не ошибка, а WARNING в лог** при
старте («DAILY_AT вне утреннего окна — намеренно?»): жёсткий отказ мешал бы
живой проверке «на 2 минуты вперёд» вечером, а продуктовое правило «утро»
всё равно видно в журнале. `.env.example` — задокументировать оба.

`daily_practice` в профиле и `DAILY_AT` в конфиге — два гейта, **оба нужны
таймеру**: хендлеры сбора/ответа/`/next` включаются профилем; таймер стартует
только при `profile.daily_practice and cfg.daily_at`. Мамин бот не получает
ничего, даже если кто-то выставит `DAILY_AT` в его `.env` (тест на это);
на en-боте таймер можно выключить, не теряя сбор и `/next`.

#### Квота Gemini (бесплатный тир, 1000/день на flash-lite)

На пользователя в день (штатно): 1 `make_sentence` + ≤1 оценка
(`check_sentence` или `grade`) + по 1 `extract` на пересланное сообщение —
порядка 5 запросов/день. **Худший случай** ×4 на каждый сервисный вызов (до 2
попыток × основная + fallback-модель в `generate_json`) — порядка 20/день.
Запас всё равно на порядок-два. Поведение при 429 — см. хендлеры (compose принимает как ok,
recall/gap — как сейчас в training: «сравни сама», ok=False; выдача при 429 на
`make_sentence` — fallback на пример карточки, без запроса).

### Крайние случаи

- **Ответ на истёкшее задание** (написала днём позже утренней выдачи, когда уже
  висит новое): открытое всегда одно — оценивается новое; старое закрыто expired.
- **Два ответа подряд** (отправила, тут же поправила): второй не проходит
  `claim_task` и молча игнорируется; правки принимаются только до первого ответа.
- **Заметка по-русски при открытом задании** («сказали heads-up, не поняла»):
  уточняющий вопрос с кнопками, не giveup и не оценка (см. хендлеры).
- **Два бота на одном VPS:** таймер только у en-юнита (`DAILY_AT` в его `.env`);
  у маминого `.env` переменной нет → нуль оверхеда.
- **Карточка удалена между выдачей и ответом:** `get_card` → `None` → «Эта фраза
  уже удалена», `finish_task(ok=False)` без SRS.
- **Карточку сдвинула ручная тренировка во время оценки** (`await` на Gemini):
  перечитываем перед SRS-шагом — свежий `due_at > today` → SRS не трогаем.
- **`phrase_form` не входит в предложение** (модель переформулировала): второй
  вызов → fallback на пример карточки, только если в нём есть `card.word`; иначе
  задание без предложения (понижение по kind, см. sentences).
- **Слово сохранено сегодня** (утром до таймера или днём): в выдачу не попадает
  до завтра (`created_at < today`), как и обещано в «Сохранено».
- **Оценка упала** (модель вернула мусор дважды, неожиданное исключение):
  `release_task`, задача снова открыта, «попробуй ещё раз»; SRS не тронут.
- **Фраза с дефисом/без** («heads up»/«heads-up»): `answers_match` строг, дальше
  `grading.grade` считает это `typo` → ok; в `listen_ok` дефисы снимаются при
  нормализации.
- **Автокапитализация телефона:** как сейчас, `answers_match` фолдит регистр.
- **Пересланное сообщение без текста** (фото, стикер): «Пока понимаю только текст».
- **Слишком длинный пересланный текст:** обрезать до 2000 символов перед `extract`.
- **`/next` дважды подряд:** второе — повтор вопроса открытого задания, новое не
  создаётся, квота не тратится. `/next` одновременно с таймером — per-user лок,
  второй увидит открытую задачу.
- **Озвучка недоступна (`TTSError`)**: задание выдаётся текстом с пометкой;
  `listen` понижается до `gap`.

### Тесты (чистое — юнит; IO — вручную)

- `tests/test_daily.py`: `task_kind` для всех ступеней + округление + rng для
  30/60; `should_send` (0/2/3 пропусков, 6 и 7 дней); `next_fire` и абсолютный
  delay через DST-день (перенос из плана reminder); `blank_out` (регистр, отсутствие
  → ValueError); `listen_ok` (дефис, пунктуация, опечатка внутри слова → ok,
  выпавший артикль → не ok, другое предложение → не ok); классификация
  входящего текста при открытом задании (forward / `+` / короткий giveup /
  кириллица → уточнение / английский → ответ).
- `tests/test_db_daily.py`: миграция добавляет `context` (на старой схеме и
  идемпотентно), `open_task` возвращает `open` и `grading`, `release_stale_grading` на старте,
  `recent_sentences` порядок и лимит, `pick_due_card` — новая раньше просроченной,
  затем самая просроченная; `claim_task` — второй вызов `False`; уникальный
  индекс отбивает вторую open-задачу; `release_task`/`finish_task` только из
  `grading`; `expire_task` только из `open`; `pick_due_card` не берёт `translation IS NULL`,
  `created_at = today` и `enriched = 0`; `recent_sentences` включает
  `reply_sentence`; `add_card` без `context` работает как раньше; `daily_state`
  счётчики.
- `tests/test_sentences.py`, `tests/test_capture.py`: моки `generate_json`
  (по образцу `test_grading`): валидный ответ, невалидный → ретрай → ошибка,
  `phrase_form` не в предложении → ретрай → fallback; `extract` — массив, пустой
  массив, обрезка до 3.
- `tests/test_daily_send.py` (async, фейковый `bot` с записью `send_message`/
  `send_voice`, in-memory SQLite, мок llm): нечего выдавать → тишина; открытое
  задание истекает и `missed_streak` растёт; тихий режим после 3 пропусков;
  `TelegramForbiddenError` у одного не останавливает остальных **и не создаёт
  задачу**; gap с fallback без `phrase_form` понижается до recall; карточка без
  примера → задание без предложения; ответ на карточку с `due_at > today` не
  меняет SRS (карточка перечитывается после оценки — тест сдвигает `due_at`
  между claim и SRS-шагом через мок оценки); одно задание = ровно один вызов
  `send_*`; два `/next` под локом → второй `resent`; `TTSError` → `listen`
  становится `gap`; клик «Ответ» по протухшему `task_id` → отказ без оценки;
  сбой оценки → `release_task` и задача снова `open`; утренняя выдача во время
  оценки ждёт лок и не истекает задачу, которая вот-вот закроется; сбой отправки
  ответа после `finish_task` не откатывает SRS и не трогает квоту; `listen_ok`
  с типографским апострофом; `recall`-текст без «контекст: None»; кнопка «Ещё
  одно» появляется только при `< 3` заданий сегодня и непустой очереди; истёкшее
  не-утреннее задание не растит `missed_streak`; `more:` со вчерашней датой —
  отказ без выдачи; `set_last_sent` не трогается цепочкой.
- `tests/test_languages.py`: ES `daily_practice is False`, EN промпты непустые,
  схемы валидные (`required` ⊆ `properties`), схема `extract` — объект с `items`.
- `tests/test_config.py` + старт: `DAILY_AT` при es-профиле не запускает цикл
  (проверяется на уровне функции-фабрики задачи в `bot.py`, вынесенной ради
  теста); вечерний `DAILY_AT` → WARNING, не исключение.
- Ручная проверка на сервере: `DAILY_AT` на 2 минуты вперёд → приходит задание
  нужного kind; ответ → оценка + 🔊; «не помню» → разбор; `/next`; пересланное
  сообщение → превью с контекстом → «Беру» → на следующее утро compose_hinted.

Ожидаемо: **124 → ~170** тестов.


### Технический журнал ревью и дельты реализации (из раздела «Провенанс» спеки)

- 2026-09-30 — брейншторм (интервью → варианты А/Б/В → пример на «a heads-up» →
  своё предложение перенесено на день 1 по предложению Victoria). Спека написана.
- 2026-09-30 — **ревью, раунд 1 (Codex, read-only): 5 P1 / 10 P2, все приняты и
  исправлены inline.** P1: таймер гейтился только `DAILY_AT` (→ и профилем);
  русская заметка при открытом задании уходила в оценку/giveup (→ уточняющие
  кнопки + `+`-префикс); `extract` ждал массив, а `generate_json` отдаёт только
  dict (→ схема `{items: [...]}`); двойной SRS при ручной тренировке + задании
  (→ SRS только если карточка ещё due); «одно сообщение» нарушалось multipart
  (→ голосовое с caption). P2: `sentence NULL` для recall противоречил алгоритму;
  гонки таймер/`/next` и двух ответов (→ лок + уникальный индекс + `claim_task`);
  `check_sentence` без `avoid` и без валидации ответа; автозачёт словарной формы
  в gap (→ только `phrase_form`); `listen_ok` пропускал выпавшее слово (→ счёт
  слов + ratio 0.9); пример карточки может быть NULL (→ fallback-цепочка);
  `/next` молчал на пустом пуле; задача создавалась до отправки (→ после);
  вечерний `DAILY_AT` (→ WARNING, не ошибка — сознательно мягче, чем просил
  ревьюер, ради живой проверки).
- 2026-09-30 — **ревью, раунд 2 (Codex): 3 P1 / 9 P2 / 3 P3, все приняты.** P1:
  `/next` проверял открытую задачу вне лока (→ вся выдача, включая проверку, под
  локом; результат sent/resent/nothing); карточка читалась до `await` оценки, ручная
  тренировка могла сдвинуть её (→ перечитать перед SRS); токен уточнения без
  `task_id` (→ хранить и проверять, что задача ещё open). P2: claim без пути
  отката (→ статусы open/grading/answered/expired, `release_task`); карточка,
  сохранённая до таймера, пришла бы в тот же день (→ `created_at < today`);
  `send_text_voice` глотал `TTSError`, а `listen` надо понижать (→ синтез до
  отправки, решение о kind у вызывающего); ответные предложения не попадали в
  `avoid` (→ колонка `reply_sentence`); пустой `context` рушил инвариант интента
  (→ всегда непустой, подстановка из текста); `enriched=0` карточки (→ фильтр в
  `pick_due_card`); `listen_ok` пропускал `can`/`can't` (→ пословный порог,
  апостроф сохранён); fallback-пример мог не содержать фразу (→ требование);
  сигнатура `add_card` (→ `context=None`). P3: контекст печатался дважды; квота
  в худшем случае ×4; контракт завершения задачи назван по функциям.
- 2026-09-30 — **ревью, раунд 3 (Codex, финальный): 2 P1 / 9 P2 / 1 P3, все
  приняты.** P1: `send_card_voice` без `caption` ломал «одно сообщение» в fallback
  (→ необязательные `caption`/`parse_mode` + `send_card_voice_to`); сравнение
  `due_at` строки с `date` (→ ISO-строки). P2: семантика `open_task` для `grading`
  (→ активная = open|grading, зомби снимаются на старте); ответ вне лока (→ хендлер
  ответа под тем же локом, `expire` только из `open`); порядок «отправить/закрыть»
  (→ finish, потом отправка); `reset_missed` только в новом сборе (→ и в старом
  `➕`, под флагом профиля); повторный `check_sentence` мог сменить verdict (→
  verdict фиксируется, ответное предложение через `make_sentence`); `enriched=1`
  с `NULL`-переводом (→ фильтр на оба); типографский апостроф в `listen_ok`;
  fallback-пример против «всегда новое» (→ оговорённое исключение, в `avoid` не
  пишется); «контекст: None» в recall (→ только если есть). P3: `close_task` в
  тестах (→ переименовано). **Ревью остановлено на трёх раундах** (правило: дальше
  ловит код и тесты, а не чтение); статус спеки — ждёт утверждения Victoria.
- 2026-10-02 — правки Victoria по прочтении: ступени 1 и 3 поменяны местами
  (день 2 — gap, день 5 — recall: после своего предложения мягче сначала увидеть
  фразу в контексте, а голое вспоминание — позже). Ёмкость: цель — пара новых
  фраз в неделю → цепочка «Ещё одно» до 3 заданий в день при одном уведомлении;
  пропуски считаются только по утренним заданиям (`daily_tasks.morning`).
- 2026-10-03 — реализация по плану `plans/2026-10-02-daily-practice.md`. Дельты
  к спеке: (а) `send_daily_task` возвращает и `"failed"` (ошибка отправки — `/next`
  отвечает «попробуй позже», а не «нечего повторять») и `"limit"` (потолок дня
  проверяется внутри лока по параметру `limit`, не в хендлере; утро не истекает
  задачу, выданную сегодня через `/next`); (б) fallback-пример карточки
  пишется в `daily_tasks.sentence` с флагом `from_example=1` (нужен для оценки
  gap/listen и для повтора по `/next`), из `recent_sentences` исключается;
  (в) `compose_hinted` без озвучки показывает предложение текстом с пометкой 🔇;
  (г) кнопка «Ещё одно» едет `reply_markup`'ом на голосовом сообщении результата;
  (д) `voice.send_card_voice*` получили `reply_markup`; (е) подписи голосовых
  обрезаются до 1024 символов (`fit_caption`), сбор фраз идёт под per-user локом;
  (ж) `check_sentence` также отвергает ответное предложение, равное (без учёта
  регистра и пробелов) ответу учащегося или записи из `avoid` → одна регенерация,
  затем пример карточки, затем ничего; (з) `capture.extract` пропускает
  некорректный элемент (нет обязательного ключа), а не валит попытку; если
  пропущены все — «ничего не нашлось»; (и) `fit_caption` берёт самый длинный
  plain-text срез, чья экранированная форма влезает (бинарный поиск): текст с
  большим числом экранируемых символов обрезается, а не теряется; (к) `daily_loop`
  берёт календарную дату в `DAILY_TZ`, а не локальную дату сервера; (л) если
  задание тем временем заменили (утро истекло старое), ответ показывает «Это
  задание уже истекло, напиши ответ на новое 🙂» (`TEXT_CLARIFY_STALE`) — и для
  кнопки «Ответ» в clarify, и для набранного ответа, гонящегося с утренней
  выдачей; второй быстрый ответ, пришедший, пока оценивается первый, молча
  игнорируется (`answer_task` → `"done" | "stale" | "busy"`, сообщение только на
  `"stale"`); (м) частичный уникальный индекс остаётся
  `WHERE status='open'`, как в спеке: ревьюер предлагал расширить на `grading`,
  отклонено — `grading` живёт только под per-user локом (а утро под тем же локом
  истекает и зависший вчерашний `grading`); (н) `gap`: вписанное целиком
  предложение (сверка как в `listen` — без учёта регистра и пунктуации, опечатка
  в слове допустима) засчитывается как точное совпадение, без вызова Gemini;
  (о) результат `compose_hinted` показывает текстом предложение-подсказку,
  которое учащийся слышал в задании (блок 🔊, как у recall/gap);
  (п) решение Victoria 2026-10-03: у en-бота нет reply-меню — пять кнопок убраны
  (`ReplyKeyboardRemove` в `/start` и в концах тренировок), разделы — командами
  Telegram через «/» (`/next /add /vocab /cards /check /listen`, роутер команд
  подключается первым и работает из любого режима); подсказка «выйди из режима» у
  кнопок daily внутри режима — про команду «/». Es-бот не меняется.
  (р) решение Victoria 2026-10-03 (случай: вставленный промо-текст засчитан как ответ
  на задание про «span» → ❌ и lapse): **сбор не путается с ответом.** Правило 1 —
  `classify_incoming(..., target=, stale=)`: после giveup и кириллицы, если открытая
  задача просрочена → `clarify`; если английский текст из ≥ 6 слов
  (`CLARIFY_MIN_WORDS`) не содержит ни `phrase_form` задачи, ни слово карточки (любое из
  двух засчитывается; без учёта регистра, `sentences.contains`) → `clarify`; короткие ответы (< 6 слов) без
  слова идут в оценку как раньше. Правило 2 — задания от `/next` и «Ещё одно»
  (`morning = 0`) через 15 минут (`NEXT_TASK_TTL`, по новой колонке
  `daily_tasks.issued_at`, ISO UTC; у старых строк NULL = не просрочено) переходят в
  режим «переспросить»: любой свободный текст → кнопки «Ответ / Новое слово»
  (`TEXT_CLARIFY`), ответ через «Ответ» оценивается как обычно (задание всё ещё
  `open`); `/next` / «Ещё одно» при просроченном задании тихо истекает его (без
  `bump_missed`) и выдаёт новое (лимит дня проверяется первым), при свежем — повтор,
  как раньше. Битый `issued_at` — не просрочено (WARNING в лог), naive-время — UTC. Утренние задания не затронуты — ждут весь день. Es-бот не меняется
  (миграция — простое добавление nullable-колонки).
  (с) решение Victoria 2026-10-04 (три чужих предложения подряд — много): дельта (о) ОТМЕНЕНА — результат `compose_hinted` не показывает текст подсказки (озвучка не менялась); en-промпт `sentence_check_*` требует, чтобы `reply_sentence` был ответной репликой собеседника на предложение ученика (та же тема, вопрос/уточнение/продолжение, с целевой фразой), запасная регенерация (`make_sentence(kind="reply")`) получает предложение ученика как контекст. Подпись «Моё в ответ:» без изменений. Es-бот не меняется.
  (т) решение Victoria 2026-10-04 (ответ, скопированный с подсказки, получал «good»): для `compose`/`compose_hinted` при непустом `task.sentence` и `listen_ok(ответ, sentence)` (регистр/знаки не в счёт) `answer_task` после `claim_task` и до оценки возвращает задачу в `open` (`release_task`; SRS, `missed_streak` и Gemini не трогаются), шлёт `TEXT_COPIED_HINT` («Это предложение из подсказки 🙂 Напиши своё — про что-нибудь из твоей жизни.») и возвращает новое значение `"retry"` (tri-state `done|stale|busy` + `retry`; хендлеры на `retry` ничего не шлют). `giveup` и `gap` не затронуты. Вторая линия: `check_sentence(..., hint=)` — en-шаблон получает строку «Подсказка (предложение из задания): {hint}» («—» без подсказки), en-system велит ставить `off` для копии/лёгкой переделки подсказки. Es-бот не меняется.
  (у) решение Victoria 2026-10-08 (5 утр подряд без повторов): расписание повторов и новых слов — см. план `docs/superpowers/plans/2026-10-08-daily-scheduling.md` и его «Провенанс плана»; реализация в Tasks 1–3 той ветки (`daily-scheduling`). Спека переписана по разделам (утреннее задание, цепочка повторов вместо «Ещё одно», `/next` только новые слова, `requested`-переспрос, `clock.py`); `MAX_TASKS_PER_DAY`, `more_button_allowed`, `more_keyboard`, `on_more`, `pick_due_card` удалены; п. дельты (р) «`/next` тихо истекает просроченное задание» отменён — `/next` повторяет открытое и обновляет `issued_at`, а переспрос по времени теперь только у задач `requested = 1` (выданных по `/next`).
