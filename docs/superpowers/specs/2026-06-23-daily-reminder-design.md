# Daily Review Reminder — Design

**Date:** 2026-06-23
**Status:** approved (brainstorm) — pending implementation plan
**2026-09-30:** для англо-бота (`BOT_LANG=en`) заменена спекой
`2026-09-30-daily-practice-design.md` (её таймер/DST-часть переиспользована там
1:1 под именами `DAILY_*`); для маминого es-бота остаётся одобренной и не реализованной.
**Components:** new `reminders.py`, `config.py`, `bot.py`, `tests/`

## Goal

Once a day, at a configured time, the bot proactively messages each user how many
words are due for review today — an unobtrusive nudge to open a training session.
If a user has nothing due, the bot stays silent that day.

## Motivation

The end user (mom) reviews in pull-mode: she only practises when she remembers to
open the bot. A gentle once-a-day "you have N words to review" lowers the barrier
to keep the SRS streak going, without turning into noise.

**This reverses a documented key decision.** The MVP was deliberately *pull-mode,
no push reminders* (`AGENTS.md`, `specs/2026-06-02-spanish-bot-design.md`). This
feature intentionally introduces the bot's first scheduled, bot-initiated message.
The "no push" principle in those docs should be updated to "one opt-out-able daily
due-count reminder" when this ships. Push *notifications beyond the daily count*
(streaks, stats, multi-time) remain out of scope.

## Decisions (from brainstorm)

- **Silent on zero.** A reminder is sent only when a user has ≥1 card due. "Once a
  day" means "at most once a day" — no "0 words / all done" message (would train
  the user to ignore the bot; with Leitner intervals 1→3→7→14→30→60, zero-due days
  are common).
- **Recipients: all allowed users, each their own count.** Iterate
  `ALLOWED_USER_IDS`, compute each user's own due count, message those with ≥1.
- **Fixed time + timezone in `.env`** (not per-user, no in-bot settings — YAGNI for
  ~3 users). The presence of `REMINDER_AT` is also the on/off switch.
- **Opt-out via `.env` exclude list** (`REMINDER_EXCLUDE_IDS`), e.g. the test
  account. No in-bot toggle, no per-user DB flag.
- **Approach A — in-process asyncio loop.** No new dependency (`zoneinfo` is stdlib
  in 3.12), no second process, no new deploy artifact. Reuses the running `Bot`
  and DB connection. The bot is `Restart=always` under systemd, so it is almost
  always up at fire time. (APScheduler and a systemd timer were considered and
  rejected as over-engineered for an at-most-once daily job — see brainstorm.)

## Architecture

A new module `reminders.py`, thin in the project style — pure logic separated from
IO, pure parts unit-tested, the timing loop verified manually.

### Pure functions (unit-tested)

- **`next_fire(now: datetime, hour: int, minute: int) -> datetime`** — given an
  aware `now` (in `REMINDER_TZ`), return the next `hour:minute` instant: today if
  still in the future, else tomorrow, as an **aware** datetime built fresh for the
  target calendar day at `hour:minute` (not `now + timedelta`). Construction via
  `zoneinfo` (`ZoneInfo(REMINDER_TZ)`), so the Spain summer/winter (CET/CEST) switch
  is handled automatically — no manual offset.
- **The sleep delay MUST be computed in absolute time:** `delay =
  next_fire.timestamp() - now.timestamp()` (equivalently, subtract after
  `.astimezone(UTC)`). **Do NOT** `await asyncio.sleep((next_fire - now).total_seconds())`
  on two same-zone aware datetimes: CPython subtracts same-`tzinfo` datetimes by
  wall clock (it skips the UTC conversion), so on a DST-transition day that delta is
  off by ±1h (verified: spring-forward gives 86400 s instead of the real 82800 s).
- **`plural_words(n: int) -> str`** — Russian pluralization of «слово»: `1, 21, 31…
  → слово`; `2–4, 22–24… → слова`; `0, 5–20, 25–30… → слов`.
- **`reminder_text(count: int) -> str`** — the message body (see Wording).

### IO / async

- **`async send_daily_reminders(bot, conn, user_ids, exclude_ids, today) -> None`**
  — for each `uid in user_ids - exclude_ids`: `count = len(db.get_due_cards(conn,
  uid, today))`; if `count > 0`, `await bot.send_message(uid, reminder_text(count),
  parse_mode="HTML")`. Each send is wrapped in its own `try/except` (see Edge cases)
  so one failure does not abort the batch.
- **`async reminder_loop(bot, conn, cfg) -> None`** — `while True`: compute
  `next_fire` in `REMINDER_TZ`, `await asyncio.sleep(delay)` (absolute-time delay,
  above) until then, call `send_daily_reminders` with `today = date.today()`, then
  loop (recompute → next day). The whole per-fire body is wrapped in `try/except` +
  log so an unexpected error reschedules instead of killing the loop.

### Wiring (`bot.py`)

After the `Bot` is created, hold a reference to the task (a bare
`create_task` may be garbage-collected — the asyncio docs require keeping a
reference) and cancel it cleanly on shutdown:

```python
reminder_task = None
if cfg.reminder_at is not None:
    reminder_task = asyncio.create_task(reminders.reminder_loop(bot, conn, cfg))
try:
    await dp.start_polling(bot)
finally:
    if reminder_task is not None:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
```

The task runs on the same event loop and thread as the handlers, so it shares the
single `conn` safely (its only DB use is the read-only `get_due_cards`). The
`finally` cancel + `gather` avoids a pending-task warning on shutdown and makes the
lifecycle explicit.

## Configuration (`config.py` / `.env`)

Add to the frozen `Config` dataclass and `load()`. **`REMINDER_AT` gates the rest:
the other two vars are parsed and validated only when `REMINDER_AT` is set.** A
disabled feature must never crash the bot, so a typo in `REMINDER_TZ` or
`REMINDER_EXCLUDE_IDS` is irrelevant while `REMINDER_AT` is absent.

- **`REMINDER_AT`** = `HH:MM` (24h). **Absent/empty → feature off** (`reminder_at =
  None`, loop never starts, the two vars below are not read). **Present but malformed
  → raise `ValueError` at startup** (loud failure in the journal, consistent with
  `_require`; better than silently firing at the wrong time). Parsed to a
  `datetime.time`. **Daytime value expected** (see Date basis).
- **`REMINDER_TZ`** = IANA name, default `Europe/Madrid` (mom is in Spain;
  peninsular Spanish bot). Parsed/validated **only when `REMINDER_AT` is set**, by
  constructing `ZoneInfo` (bad name → `ZoneInfoNotFoundError` at startup).
- **`REMINDER_EXCLUDE_IDS`** = comma-separated ids, default empty set (parsed like
  `ALLOWED_USER_IDS`), **only when `REMINDER_AT` is set**.

## Date basis (why server date, not local date)

The **timezone governs only *when* the reminder fires**; the **count uses
`date.today()`** — the same server date the training handlers already use. This
guarantees the number in the reminder matches what the user sees on opening a
training screen, and avoids touching the training handlers.

Safe **for a daytime `REMINDER_AT`** (the intended use). Mom is in Spain
(`Europe/Madrid`, UTC+1/+2): a daytime fire, e.g. 10:00 local = 08:00–09:00 UTC, is
the same calendar day in both zones; she also trains during her daytime, all the
same UTC date. So the count matches what she sees on opening training.

**Known limitation:** this holds only while local and UTC dates agree at fire time.
A very-early-morning `REMINDER_AT` (e.g. 00:30 Madrid = 22:30/23:30 UTC the previous
day) would compute the count for the prior server day — off by one. We therefore
**require a daytime `REMINDER_AT`** rather than make "today" timezone-aware in the
training handlers (that would be the only fully-general fix, but it is out of scope
and pointless for current users). The realistic config is a morning hour.

## Wording

Gender-neutral (one male user among the users) and time-neutral (the time is
configurable; avoid "Доброе утро"):

> 🔔 Сегодня на повторение: &lt;b&gt;N слов&lt;/b&gt;. Загляни в «🎴 Карточки», когда будет минутка 🙂

The count uses `plural_words(N)`. **The bot has no default parse mode**, so the bold
must be sent with `parse_mode="HTML"` and `<b>…</b>` (the pattern `card_preview`
already uses) — otherwise raw `**`/`<b>` would show literally. `reminder_text`
returns the HTML string; `send_daily_reminders` passes `parse_mode="HTML"`. Plain
text (no bold) is the fallback if we'd rather not set a parse mode. Exact copy is
easy to adjust at spec review.

## Edge cases

- **Bot down at fire time.** That day's reminder is missed (at-most-once). Accepted;
  no catch-up. (This is the one reliability cost of Approach A vs a systemd timer.)
- **User never pressed /start, or blocked the bot.** `bot.send_message` raises
  `TelegramForbiddenError` / `TelegramBadRequest` ("chat not found"). Caught per
  user: log and skip; the rest of the batch still goes out.
- **Unexpected error mid-fire.** The per-fire `try/except` logs and lets the loop
  reschedule the next day instead of dying.
- **Malformed `REMINDER_AT`.** Fail fast at startup (visible in `journalctl`).
- **`REMINDER_AT` absent.** Loop not started — feature fully off, zero overhead.
- **Re-fire / tight loop.** After sending, `next_fire` is recomputed from the
  current time (just past today's target) → always returns tomorrow; no double-send,
  no busy loop.
- **DB concurrency.** Reminder only reads (`get_due_cards`) on the same loop thread
  and `conn` as the handlers → no locking concern.

## Testing

Per project convention (`AGENTS.md`): pure logic unit-tested, IO/timing verified
manually.

- **Pure, unit:** `next_fire` (now before target → today; after → tomorrow) **and
  the absolute-time delay** — across the Madrid spring-forward day the
  `timestamp()`-based delay is the real elapsed seconds (e.g. 23 h = 82800 s, not
  86400 s), which is the regression guard for finding 1; `plural_words` (1, 2, 4, 5,
  11, 21); `reminder_text` (returns the `<b>…</b>` HTML with the right plural form).
- **`send_daily_reminders`:** async test with a fake `bot` (records
  `send_message` calls + the `parse_mode` arg) and an in-memory SQLite seeded so some
  users have due cards and some don't — assert only `due>0` non-excluded users are
  messaged, `parse_mode="HTML"` is passed, and that a send raising
  `TelegramForbiddenError` for one user does not stop the others.
- **`reminder_loop`:** not unit-tested (timers); covered by the manual pass.
- **Manual on server:** set `REMINDER_AT` ~2 min ahead → message arrives with the
  correct count; a user with 0 due → no message; an id in `REMINDER_EXCLUDE_IDS` →
  no message; a send-log line appears in `journalctl`.

Expected suite: **81 → ~87** (several new unit tests).

## Out of scope

- Per-user reminder time / timezone, and any in-bot settings command.
- An in-bot `/reminders on|off` toggle and per-user DB preference.
- Catch-up of reminders missed while the bot was down (systemd-timer `Persistent`
  behavior).
- Streaks, progress stats, or any push beyond the single daily due-count.
- Time-of-day–varying greeting ("Доброе утро" vs "Добрый вечер").
