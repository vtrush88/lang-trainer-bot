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
