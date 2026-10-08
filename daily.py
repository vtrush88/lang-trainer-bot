"""Ежедневная практика (en-бот): чистая логика (виды заданий, норма новых слов, классификация
свободного текста, рендеры) + IO (выдача заданий и цепочка повторов под user_lock, утренний
цикл, оценка ответов, сбор фраз из пересланного текста).

Спека: docs/superpowers/specs/2026-09-30-daily-practice-design.md.
"""
from __future__ import annotations

import asyncio
import difflib
import html
import logging
import os
import random
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from aiogram.exceptions import TelegramAPIError

import clock
import db
import formatting
import intents
import voice
from formatting import esc, field
from services import capture, grading, sentences, srs, tts
from services.llm import QuotaExceededError

VOICE_KINDS = frozenset({"compose_hinted", "listen"})
MAX_REPEATS_PER_DAY = 5
NEW_PER_WEEK = 2
NEW_MIN_GAP_DAYS = 3
MISSED_QUIET_AFTER = 3
QUIET_PERIOD_DAYS = 7
WORD_RATIO = 0.8
GIVEUP_MAX_WORDS = 4
CLARIFY_MIN_WORDS = 6                    # длинный английский без слова задания → переспросить
NEXT_TASK_TTL = timedelta(minutes=15)    # только задание по /next (requested) — потом «переспросить»

rng = random.Random()                    # дефолтный генератор выдачи


def _module_rng() -> random.Random:
    """Текущий модульный `rng` (читается при вызове — monkeypatch daily.rng в тестах работает);
    нужен там, где параметр `rng` затеняет модульное имя."""
    return rng

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


def week_start(today: date) -> date:
    """Понедельник ISO-недели, в которую попадает today."""
    return today - timedelta(days=today.weekday())


def new_word_allowed(*, count_this_week: int, last_new: date | None, today: date) -> bool:
    if count_this_week >= NEW_PER_WEEK:
        return False
    return last_new is None or (today - last_new).days >= NEW_MIN_GAP_DAYS


def days_until_new_word(*, count_this_week: int, last_new: date | None, today: date) -> int:
    gap_wait = 0 if last_new is None else max(0, NEW_MIN_GAP_DAYS - (today - last_new).days)
    week_wait = 0
    if count_this_week >= NEW_PER_WEEK:
        week_wait = (week_start(today) + timedelta(days=7) - today).days
    return max(gap_wait, week_wait)


def when_text(days: int) -> str:
    if days == 1:
        return "завтра"
    if days % 100 not in (11, 12, 13, 14):
        if days % 10 == 1:
            return f"через {days} день"
        if days % 10 in (2, 3, 4):
            return f"через {days} дня"
    return f"через {days} дней"


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


def _utcnow() -> datetime:
    """Текущее aware-время UTC; модульная функция — тесты подменяют её monkeypatch'ем."""
    return datetime.now(timezone.utc)


_bad_issued_at_warned: set = set()   # id задач, про битый issued_at уже предупредили


def is_stale(task, now: datetime) -> bool:
    """Задание, явно запрошенное через /next (requested), старше NEXT_TASK_TTL. Утренние и
    задания цепочки — никогда (ждут весь день); issued_at NULL (строки до дельты (р)) — не stale; naive-время считаем UTC;
    нечитаемая строка — не stale (одно предупреждение в лог на задачу)."""
    if task["morning"] or not task["requested"] or not task["issued_at"]:
        return False
    try:
        issued = datetime.fromisoformat(task["issued_at"])
    except ValueError:
        key = task["id"]
        if key not in _bad_issued_at_warned:
            _bad_issued_at_warned.add(key)
            log.warning("task %s: malformed issued_at %r — treated as not stale",
                        key, task["issued_at"])
        return False
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return now - issued >= NEXT_TASK_TTL


def _mentions_any(text: str, target: str | Iterable[str | None] | None) -> bool | None:
    """Есть ли в тексте хоть одна цель (без учёта регистра). None — целей нет вовсе."""
    targets = [target] if isinstance(target, str) else list(target or ())
    targets = [t for t in targets if t]
    if not targets:
        return None
    return any(sentences.contains(text, t) for t in targets)


def classify_incoming(text: str, *, forwarded: bool, has_open_task: bool,
                      target: str | Iterable[str | None] | None = None,
                      stale: bool = False) -> str:
    """Что делать со свободным текстом вне режимов (см. спеку, «Хендлеры», дельта (р)).

    target — цель задания: строка или несколько (phrase_form задачи и слово карточки);
    текст «содержит цель», если в нём есть любая из них. stale — задача просрочена (is_stale).
    """
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
    if stale:
        return "clarify"
    if _mentions_any(t, target) is False and len(t.split()) >= CLARIFY_MIN_WORDS:
        return "clarify"
    return "answer"


def strip_capture_prefix(text: str) -> str:
    t = text.strip()
    return t[1:].strip() if t.startswith("+") else t


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


# ---- Рендеры: тексты заданий и результатов (чистые, HTML для parse_mode="HTML") ----
VOICE_UNAVAILABLE = "🔇 (озвучка временно недоступна)"
TEXT_NEXT_LATER = "Новое слово будет {when} 🙂"
TEXT_NEXT_TOMORROW = "Сохранённое сегодня придёт завтра 🙂"
TEXT_NEXT_SAVED_WHEN = "Сохранённое сегодня придёт {when} 🙂"   # норма/интервал пустят позже
TEXT_NO_NEW_WORDS = "Новых слов нет — перешли что-нибудь 🙂"
TEXT_MORE_GONE = "Эта кнопка больше не нужна — задания теперь приходят сами 🙂"
TEXT_NEXT_FAILED = "Не получилось выдать задание сейчас 😕 Попробуй /next через минутку."
TEXT_CARD_DELETED = "Эта фраза уже удалена 🙂"
TEXT_GRADE_FAILED = "Не получилось проверить сейчас 😕 Напиши ещё раз через минутку."
TEXT_SAVED = "Сохранено ✅ — придёт с ближайшим новым словом."
TEXT_ONLY_TEXT = "Пока понимаю только текст: перешли сообщение или напиши фразу 🙂"
TEXT_NOTHING_FOUND = "Не вижу, что тут взять 🙂 Напиши слово или фразу явно."
TEXT_COPIED_HINT = "Это предложение из подсказки 🙂 Напиши своё — про что-нибудь из твоей жизни."
TEXT_CLARIFY = "Это ответ на задание или новое слово?"
TEXT_CLARIFY_STALE = "Это задание уже истекло, напиши ответ на новое 🙂"
TEXT_EMPTY_CAPTURE = "После «+» напиши слово или фразу 🙂"
TEXT_QUOTA_COMPOSE = ("✅ Принято! (умная проверка пока недоступна — лимит бесплатных "
                      "запросов; своё предложение всё равно засчитано)")


def card_block(card) -> str:
    """Фраза жирным, перевод, IPA, контекст — без примера."""
    text = (f"🔤 <b>{esc(card['word'])}</b>\n"
            f"🇷🇺 {esc(card['translation'])}\n"
            f"🗣 {esc(card['transcription'])}")
    context = field(card, "context")
    if context:
        text += f"\n📍 {esc(context)}"
    return text


def render_task(kind: str, card, sentence: str | None, sentence_ru: str | None,
                phrase_form: str | None, *, voice_ok: bool) -> str:
    word = esc(card["word"])
    if kind == "compose_hinted":
        text = card_block(card)
        if not voice_ok:
            text += f"\n{VOICE_UNAVAILABLE}"
            if sentence:
                text += f"\n<i>{esc(sentence)}</i>"
        return text + (f"\n\nНапиши своё предложение с <b>{word}</b> — "
                       "про что-нибудь из твоей жизни.")
    if kind == "gap":
        return (f"Вставь пропуск:\n<i>{esc(blank_out(sentence, phrase_form))}</i>\n"
                f"({esc(sentence_ru)})")
    if kind == "recall":
        text = f"Как сказать по-английски: «{esc(card['translation'])}»?"
        context = field(card, "context")
        return text + (f" (контекст: {esc(context)})" if context else "")
    if kind == "listen":
        return "Напиши то, что услышишь."
    if kind == "compose":
        return f"Напиши своё предложение с <b>{word}</b>."
    raise ValueError(f"unknown kind {kind!r}")


def with_sentence(text: str, sentence: str | None) -> str:
    return f"{text}\n\n🔊 <i>{esc(sentence)}</i>" if sentence else text


CAPTION_LIMIT = 1024
_TAG_RE = re.compile(r"<[^>]+>")


def fit_caption(text: str) -> str:
    """Подпись к голосовому <= 1024 символов — гарантированно.

    Длинный текст деградирует в plain: теги снимаем, сущности раскрываем, режем,
    экранируем заново (экранирование удлиняет — поэтому бинарный поиск наибольшего
    среза, чей экранированный вид влезает) и ставим «…».
    """
    if len(text) <= CAPTION_LIMIT:
        return text
    plain = html.unescape(_TAG_RE.sub("", text))
    target = CAPTION_LIMIT - 1
    lo, hi = 0, min(len(plain), target)       # наибольший срез, чей экранированный вид влезает
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(esc(plain[:mid])) <= target:
            lo = mid
        else:
            hi = mid - 1
    out = esc(plain[:lo])
    return out + "…"


def render_compose_result(check: dict | None) -> str:
    if check is None:
        return TEXT_QUOTA_COMPOSE
    v = check["verdict"]
    if v == "good":
        text = "✅ Отлично, звучит естественно."
    elif v == "fix":
        text = f"✅ Почти. Лучше так: {esc(check['corrected'])} ({esc(check['note'])})"
    else:
        text = f"❌ Фраза тут не сработала: {esc(check['note'])}."
    if check.get("reply_sentence"):
        text += f"\n\nМоё в ответ: <i>{esc(check['reply_sentence'])}</i>"
    return text


def render_grade_result(verdict: dict | None, *, exact: bool, expected: str, quota: bool) -> str:
    if exact:
        return "✅ Верно!"
    if quota or verdict is None:
        return (f"❌ Правильно: {esc(expected)}\n(умная проверка пока недоступна — "
                "лимит бесплатных запросов; сравни свой ответ с правильным)")
    if verdict["verdict"] == "correct":
        return "✅ Верно!"
    if verdict["verdict"] == "typo":
        return f"✅ Почти! Правильно: {esc(verdict['correct'])} ({esc(verdict['note'])})"
    return f"❌ Не совсем. Правильно: {esc(verdict['correct'])} ({esc(verdict['note'])})"


def render_listen_result(ok: bool, sentence: str) -> str:
    return (f"✅ Всё верно: <i>{esc(sentence)}</i>" if ok
            else f"Почти. Было: <i>{esc(sentence)}</i>")


def render_giveup(card, sentence: str | None) -> str:
    text = f"Ничего 🙂\n\n{formatting.card_preview(card)}"   # полная карточка, с примером
    if sentence:
        text += f"\n\n<i>{esc(sentence)}</i>"
    return text + "\n\nВернусь с ней завтра."


# ---- IO: выдача задания, утренний прогон, цикл ----
log = logging.getLogger(__name__)
# Вся иерархия Bot API: Forbidden/BadRequest/Network, но и RetryAfter/ServerError —
# они соседи, а не подклассы перечисленных.
TELEGRAM_SEND_ERRORS = (TelegramAPIError,)

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
                          rng: random.Random | None = None, *, morning: bool = False,
                          want_new: bool = False) -> str:
    """Выдать задание под локом пользователя (см. _issue_locked).

    "sent" | "resent" | "nothing" | "later" | "tomorrow" | "failed"."""
    if rng is None:
        rng = _module_rng()
    async with user_lock(user_id):
        return await _issue_locked(bot, conn, llm, profile, user_id, today, rng,
                                   morning=morning, want_new=want_new, chain=False)


def _nothing(user_id: int, reason: str) -> str:
    """Каждый "nothing" выдачи — с причиной в логе (иначе «почему сегодня тишина» не понять)."""
    log.info("daily %s: nothing — %s", user_id, reason)
    return "nothing"


async def _issue_locked(bot, conn, llm, profile, user_id: int, today: date, rng: random.Random,
                        *, morning: bool, want_new: bool, chain: bool) -> str:
    """Ядро выдачи; вызывается УЖЕ под user_lock (лок не реентерабельный — цепочка из
    answer_task зовёт эту функцию, а не send_daily_task).

    Порядок: открытая задача → тихий режим (утро) → повтор (если не want_new; потолок
    MAX_REPEATS_PER_DAY, в день долга нового слова нет) → новое слово по недельной норме."""
    active = db.open_task(conn, user_id)
    expired_prev = False
    if active is not None:
        if chain:   # страховка: задачу только что закрыли под этим же локом
            return _nothing(user_id, "chain guard: a task is still open")
        if morning:
            if active["sent_on"] == today.isoformat():
                # сегодняшнее уже выдано (/next перед DAILY_AT) — не истекаем
                return _nothing(user_id, "open task from today")
            # include_grading: под локом grading — зомби (сбой без рестарта)
            was_morning = db.expire_task(conn, active["id"], include_grading=True)
            expired_prev = True
            if was_morning:
                db.bump_missed(conn, user_id)
        else:
            card = db.get_card(conn, active["card_id"])
            if card is not None:   # /next при открытой задаче — только повторить её
                try:
                    final_kind = await deliver(bot, conn, profile, user_id,
                                               _prepared_from_task(active), card)
                except TELEGRAM_SEND_ERRORS as exc:
                    log.warning("resend to %s failed: %s", user_id, exc)
                    return "failed"
                if final_kind != active["kind"]:
                    db.set_task_kind(conn, active["id"], final_kind)
                # свежий issued_at: ответ сразу после /next не попадёт в «переспрос»
                db.touch_task_issued_at(conn, active["id"], _utcnow())
                return "resent"
            db.expire_task(conn, active["id"])   # карточка удалена — задача мертва
    if morning:
        st = db.get_daily_state(conn, user_id)
        if not should_send(st["missed_streak"], st["last_sent_on"], today):
            return _nothing(user_id, "quiet mode" + (" (expired yesterday's open task first)"
                                                     if expired_prev else ""))
    card = None
    if not want_new:
        repeat = db.pick_due_repeat(conn, user_id, today)
        if repeat is not None:
            if db.count_repeats_on(conn, user_id, today) >= MAX_REPEATS_PER_DAY:
                # долг повторов важнее нового слова — остаток завтра
                return _nothing(user_id, "repeat cap reached, repeats remain for tomorrow")
            card = repeat
    is_new = card is None
    if is_new:
        allowed = (db.count_new_on(conn, user_id, today) == 0
                   and new_word_allowed(
                       count_this_week=db.count_new_since(conn, user_id, week_start(today), today),
                       last_new=db.last_new_on(conn, user_id), today=today))
        card = db.pick_new_card(conn, user_id, today)
        if card is None:   # наличие раньше нормы: «будет через N дней» без слов — неправда
            if want_new and db.has_new_cards_created_on(conn, user_id, today):
                return "tomorrow"
            return _nothing(user_id, "no due repeat and no new card")
        if not allowed:
            if want_new:
                return "later"
            return _nothing(user_id, "new-word quota or 3-day gap")
    prepared = await prepare_task(conn, llm, profile, card, rng)
    try:
        final_kind = await deliver(bot, conn, profile, user_id, prepared, card)
    except TELEGRAM_SEND_ERRORS as exc:
        log.warning("send to %s failed: %s", user_id, exc)
        return "failed"
    db.create_task(conn, user_id=user_id, card_id=card["id"], kind=final_kind,
                   sentence=prepared.sentence, sentence_ru=prepared.sentence_ru,
                   phrase_form=prepared.phrase_form, from_example=prepared.from_example,
                   today=today, morning=morning, issued_at=_utcnow(), is_new=is_new,
                   requested=want_new)
    log.info("daily task for %s: card %s, kind %s%s%s", user_id, card["id"], final_kind,
             " (new)" if is_new else "", " (morning)" if morning else "")
    if morning:
        db.set_last_sent(conn, user_id, today)
    return "sent"


async def run_morning(bot, conn, llm, profile, user_ids: Iterable[int], today: date,
                      rng: random.Random) -> None:
    for uid in sorted(set(user_ids)):
        try:
            result = await send_daily_task(bot, conn, llm, profile, uid, today, rng, morning=True)
            log.info("daily %s → %s", uid, result)
        except Exception:
            log.exception("daily task for %s failed", uid)


async def daily_loop(bot, conn, llm, profile, cfg) -> None:
    """Зону настраивает bot.main (clock.configure) — единственный владелец; здесь только чтение."""
    while True:
        now = clock.now()
        target = next_fire(now, cfg.daily_at.hour, cfg.daily_at.minute)
        delay = fire_delay(now, target)
        log.info("daily loop: next fire %s (in %.0fs)", target, delay)
        await asyncio.sleep(delay)
        try:
            await run_morning(bot, conn, llm, profile,
                              cfg.allowed_user_ids - cfg.daily_exclude_ids, clock.today(), rng)
        except Exception:
            log.exception("daily run failed")


# ---- IO: ответ на задание ----
@dataclass
class Graded:
    ok: bool
    text: str
    speak: str | None          # что озвучить вместе с ответом (предложение/слово) или None
    reply_sentence: str | None


async def grade_answer(llm, profile, task, card, answer: str, *, giveup: bool,
                       avoid: list[str]) -> Graded:
    """Оценка по СОХРАНЁННОМУ kind задачи. БД не трогает (вызывается с to_thread)."""
    kind = task["kind"]
    sentence = task["sentence"]
    if giveup:
        return Graded(False, render_giveup(card, sentence), sentence or card["word"], None)
    if kind in ("compose_hinted", "compose"):
        try:
            check = await asyncio.to_thread(sentences.check_sentence, llm, profile, card,
                                            answer, avoid, sentence)
        except QuotaExceededError:
            check = None
        if check is None:
            return Graded(True, render_compose_result(None), None, None)
        reply = check.get("reply_sentence")
        return Graded(check["verdict"] in ("good", "fix"),
                      render_compose_result(check), reply, reply)
    if kind in ("recall", "gap"):
        expected = card["word"] if kind == "recall" else (task["phrase_form"] or card["word"])
        prompt_ru = (card["translation"] if kind == "recall"
                     else (task["sentence_ru"] or card["translation"]))
        if (grading.answers_match(answer, expected)
                or (kind == "gap" and sentence and listen_ok(answer, sentence))):
            ok = True   # gap: целиком вписанное предложение тоже верно
            text = render_grade_result(None, exact=True, expected=expected, quota=False)
        else:
            try:
                verdict = await asyncio.to_thread(grading.grade, llm, profile,
                                                  prompt_ru=prompt_ru, expected=expected,
                                                  answer=answer.strip())
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


async def _send_result(bot, conn, profile, chat_id: int, card, graded: Graded) -> bool:
    """Результат — ОДНО сообщение: голос с подписью, иначе текст. True — доставлен."""
    caption = fit_caption(graded.text)
    try:
        if graded.speak and graded.speak != card["word"]:
            mp3 = await _synthesize_tmp(graded.speak, profile.tts_voice)
            if mp3 is not None:
                try:
                    await voice.send_text_voice(bot, chat_id, mp3, caption=caption,
                                                parse_mode="HTML")
                    return True
                finally:
                    if os.path.exists(mp3):
                        os.remove(mp3)
        elif graded.speak:
            sent = await voice.send_card_voice_to(bot, chat_id, conn, card, profile.tts_voice,
                                                  caption=caption, parse_mode="HTML")
            if sent is not None:
                return True
        await bot.send_message(chat_id, graded.text, parse_mode="HTML")
        return True
    except TELEGRAM_SEND_ERRORS as exc:
        log.warning("result to %s not delivered: %s", chat_id, exc)
        return False


async def _safe_send(bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except TELEGRAM_SEND_ERRORS as exc:
        log.warning("message to %s not delivered: %s", chat_id, exc)
        return False


async def _chain_next(bot, conn, llm, profile, user_id: int, rng: random.Random) -> None:
    """Следующее задание после доставленного результата — под ТЕМ ЖЕ локом (через
    _issue_locked, не send_daily_task: asyncio.Lock не реентерабелен). Дата берётся заново:
    ответ, начатый до полуночи, не выдаёт задание вчерашним днём. Сбой — только лог."""
    try:
        result = await _issue_locked(bot, conn, llm, profile, user_id, clock.today(), rng,
                                     morning=False, want_new=False, chain=True)
    except Exception:
        log.warning("chain for %s crashed", user_id, exc_info=True)
        return
    if result == "failed":
        log.warning("chain for %s: next task not delivered", user_id)
    else:
        log.info("chain for %s → %s", user_id, result)


async def answer_task(bot, conn, llm, profile, user_id: int, text: str, today: date,
                      *, giveup: bool, task_id: int | None = None,
                      rng: random.Random | None = None) -> str:
    """claim -> оценка -> перечитать карточку -> SRS -> finish -> отправка, всё под локом.

    "done"  — ответ принят и обработан (в т.ч. сбой оценки / удалённая карточка);
    "stale" — задачи нет / чужая / утро успело её истечь: ответ НЕ применяется к новой;
    "retry" — ответ скопирован с подсказки (compose/compose_hinted): задача снова open,
              SRS и missed_streak не тронуты, TEXT_COPIED_HINT уже отправлен внутри;
    "busy"  — задача ещё жива, но уже не open (оценивается или отвечена): второй
              быстрый ответ — хендлер молча игнорирует.
    task_id — задача, которую хендлер видел до лока.

    После доставленного результата ("done", кроме сбоя оценки) — цепочка: следующий
    повтор/новое слово на сегодня (_chain_next), её исход на результат не влияет.
    """
    if rng is None:
        rng = _module_rng()
    async with user_lock(user_id):
        task = db.get_task(conn, task_id) if task_id is not None else db.open_task(conn, user_id)
        if task is None or task["user_id"] != user_id or task["status"] == db.TASK_EXPIRED:
            return "stale"
        if task["status"] != db.TASK_OPEN or not db.claim_task(conn, task["id"]):
            return "busy"
        card = db.get_card(conn, task["card_id"])
        if card is None:
            if await _close_deleted(bot, conn, user_id, task["id"]):
                await _chain_next(bot, conn, llm, profile, user_id, rng)
            return "done"
        if (not giveup and task["kind"] in ("compose", "compose_hinted") and task["sentence"]
                and listen_ok(text, task["sentence"])):
            db.release_task(conn, task["id"])   # (т): копия подсказки — не ответ
            await _safe_send(bot, user_id, TEXT_COPIED_HINT)
            return "retry"
        try:
            graded = await grade_answer(llm, profile, task, card, text, giveup=giveup,
                                        avoid=db.recent_sentences(conn, card["id"], n=3))
        except Exception:
            log.exception("grading failed for task %s", task["id"])
            db.release_task(conn, task["id"])
            await _safe_send(bot, user_id, TEXT_GRADE_FAILED)
            return "done"
        fresh = db.get_card(conn, card["id"])   # могли удалить/повторить, пока думал Gemini
        if fresh is None:
            if await _close_deleted(bot, conn, user_id, task["id"]):
                await _chain_next(bot, conn, llm, profile, user_id, rng)
            return "done"
        if fresh["due_at"] <= today.isoformat():   # иначе ручная тренировка уже засчитала
            interval = srs.next_interval(fresh["interval_days"], graded.ok)
            db.update_review(conn, fresh["id"], interval_days=interval,
                             due_at=srs.due_on(today, interval), remembered=graded.ok)
        db.finish_task(conn, task["id"], ok=graded.ok, reply_sentence=graded.reply_sentence)
        db.reset_missed(conn, user_id)
        if await _send_result(bot, conn, profile, user_id, fresh, graded):
            await _chain_next(bot, conn, llm, profile, user_id, rng)
        return "done"


async def _close_deleted(bot, conn, user_id: int, task_id: int) -> bool:
    """Закрыть задачу удалённой карточки; True — уведомление доставлено."""
    db.finish_task(conn, task_id, ok=False)
    db.reset_missed(conn, user_id)   # ответ был — тихий режим снимается
    return await _safe_send(bot, user_id, TEXT_CARD_DELETED)


# ---- IO: сбор фраз из свободного текста ----
# Те же тексты, что в handlers/add.py (квота / ошибка обогащения).
TEXT_QUOTA = "Лимит бесплатных ИИ-запросов пока исчерпан 😕 Попробуй позже или завтра."
TEXT_CAPTURE_FAILED = ("Не получилось обработать сейчас 😕 Попробуй ещё раз через минутку "
                       "или пришли другое слово.")


async def capture_items(conn, llm, profile, user_id: int, text: str) -> tuple[list[dict], str | None]:
    """Фразы из текста без дублей (внутри ответа и в словаре) + текст ошибки для пользователя."""
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
