"""Подсказка для заданий «вспомни»/«пропуск»: короткое английское объяснение слова
без самого слова. По образцу services/sentences.py: generate_json + до двух попыток +
валидация. Любой сбой (в т.ч. квота) → None: вызывающий покажет только маску."""
from __future__ import annotations

import logging
import re

from languages import LanguageProfile
from services import llm as llm_service
from services.sentences import contains

log = logging.getLogger(__name__)


_STOP = frozenset({"a", "an", "the", "to", "of", "sb", "sth", "someone", "something",
                   "one's", "oneself"})
_MIN_TOKEN = 4


def _leaks(definition: str, word: str, phrase_form: str | None = None) -> bool:
    """Выдаёт ли объяснение ответ: значимое слово карточки/формы (без стоп-слов и коротких,
    < 4 букв) как начало слова в definition, либо вся фраза без дефисов / слитно."""
    text = definition or ""
    lowered = text.lower()
    for phrase in (word, phrase_form):
        if not phrase:
            continue
        p = phrase.lower()
        if contains(text, p):
            return True
        for variant in (p.replace("-", " "), re.sub(r"[\s-]+", "", p)):
            if variant and variant in lowered:
                return True
        for token in re.split(r"[\s-]+", p):
            token = token.strip(".,!?;:\"()«»")
            letters = sum(ch.isalpha() for ch in token)
            if token in _STOP or letters < _MIN_TOKEN:
                continue
            if re.search(rf"\b{re.escape(token)}", lowered):
                return True
    return False


def make_hint(llm: llm_service.LLM, profile: LanguageProfile, card,
              phrase_form: str | None = None) -> str | None:
    word = card["word"]
    user = profile.hint_user_template.format(word=word, translation=card["translation"])
    try:
        for _ in range(2):
            data = llm_service.generate_json(
                llm, system=profile.hint_system, schema=profile.hint_schema,
                text=user, max_output_tokens=256)
            definition = ((data or {}).get("definition") or "").strip()
            if definition and not _leaks(definition, word, phrase_form):
                return definition
        log.warning("hint for %r: no valid definition after 2 attempts", word)
    except Exception as exc:   # noqa: BLE001 — подсказка best-effort: квота/сеть → только маска
        log.warning("hint for %r failed: %s", word, exc)
    return None
