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


def _norm(s: str) -> str:
    return (s or "").strip().lower()


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
    used = {_norm(answer), *(_norm(a) for a in avoid)}
    if reply and contains(reply, reply_form) and _norm(reply) not in used:
        result["reply_sentence"] = reply
        result["reply_sentence_ru"] = data.get("reply_sentence_ru") or ""
        return result
    # Вердикт зафиксирован; ответное предложение добираем отдельно, check не повторяем.
    try:
        topic = _topic(card).strip()
        reply_ctx = f"ответ собеседнику на реплику: «{answer}»"
        reply_card = {**dict(card),
                      "context": f"{topic}; {reply_ctx}" if topic else reply_ctx}
        fresh = make_sentence(llm, profile, reply_card, "reply", [*avoid, answer])
        result["reply_sentence"], result["reply_sentence_ru"] = fresh["sentence"], fresh["sentence_ru"]
    except Exception as exc:   # noqa: BLE001 — вердикт уже есть, ответное предложение — best-effort
        log.warning("reply sentence regeneration failed: %s", exc)
        fb = fallback_sentence(card)
        if fb is not None:
            result["reply_sentence"], result["reply_sentence_ru"] = fb["sentence"], fb["sentence_ru"]
    return result
