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
