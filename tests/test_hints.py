"""services/hints.py: объяснение слова для подсказки (Gemini) с валидацией."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors

from languages import PROFILES
from services import hints, llm

EN = PROFILES["en"]
CARD = {"id": 1, "word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/x/", "example": "e", "example_translation": "э", "context": "созвон"}


def _llm(client):
    return llm.LLM(client=client, models=("flash",))


def _resp(payload):
    return SimpleNamespace(text=json.dumps(payload))


def test_make_hint_returns_definition_and_uses_profile():
    client = MagicMock()
    client.models.generate_content.return_value = _resp(
        {"definition": "  A warning given in advance.  "})
    assert hints.make_hint(_llm(client), EN, CARD) == "A warning given in advance."
    kw = client.models.generate_content.call_args.kwargs
    assert kw["config"].system_instruction == EN.hint_system
    assert kw["config"].response_schema == EN.hint_schema
    assert kw["contents"] == "Слово: a heads-up\nПеревод: предупреждение заранее"


def test_make_hint_retries_when_definition_contains_word_then_none():
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _resp({"definition": "A Heads-Up is a warning."}), _resp({"definition": ""})]
    assert hints.make_hint(_llm(client), EN, CARD) is None
    assert client.models.generate_content.call_count == 2


def test_make_hint_second_attempt_can_succeed():
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _resp({"definition": "a heads-up"}), _resp({"definition": "Early warning."})]
    assert hints.make_hint(_llm(client), EN, CARD) == "Early warning."


def test_make_hint_quota_and_crash_give_none():
    client = MagicMock()
    client.models.generate_content.side_effect = [errors.ClientError(429, {"message": "quota"})]
    assert hints.make_hint(_llm(client), EN, CARD) is None
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("boom")
    assert hints.make_hint(_llm(client), EN, CARD) is None


@pytest.mark.parametrize("definition, word, phrase_form", [
    ("Heads-up: an early warning.", "a heads-up", None),
    ("A heads up before something happens.", "a heads-up", None),
    ("An early headsup.", "a heads-up", None),
    ("To give notice in advance.", "give someone a heads-up", None),
    ("He gave notice early.", "give someone a heads-up", "gave him a heads-up"),
])
def test_leaks_rejects_word_tokens_and_variants(definition, word, phrase_form):
    assert hints._leaks(definition, word, phrase_form) is True


@pytest.mark.parametrize("definition, word", [
    ("to warn someone in advance", "give someone a heads-up"),
    ("to give something to a person before it happens", "a heads-up"),   # только стоп-слова
])
def test_leaks_accepts_clean_definitions(definition, word):
    assert hints._leaks(definition, word) is False


def test_make_hint_rejects_partial_leak_and_uses_phrase_form():
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _resp({"definition": "Heads-up: an early warning."}),
        _resp({"definition": "He gave notice early."})]
    assert hints.make_hint(_llm(client), EN, CARD, phrase_form="gave him a heads-up") is None
