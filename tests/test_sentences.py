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


def test_check_sentence_regenerates_reply_that_repeats_answer_or_avoid():
    # reply == ответ ученика
    dup = {**CHECK, "reply_sentence": "I gave a heads-up.", "reply_phrase_form": "heads-up"}
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp(dup), _resp(GOOD)]
    out = sentences.check_sentence(_llm(client), EN, CARD, "i gave a heads-up. ", [])
    assert out["reply_sentence"] == GOOD["sentence"]
    # reply из avoid
    dup2 = {**CHECK, "reply_sentence": "Old heads-up.", "reply_phrase_form": "heads-up"}
    client.models.generate_content.side_effect = [_resp(dup2), _resp(GOOD)]
    out = sentences.check_sentence(_llm(client), EN, CARD, "x", ["Old heads-up."])
    assert out["reply_sentence"] == GOOD["sentence"]


def test_check_sentence_requires_note():
    client = MagicMock()
    client.models.generate_content.side_effect = [_resp({**CHECK, "note": ""}), _resp(CHECK)]
    out = sentences.check_sentence(_llm(client), EN, CARD, "x", [])
    assert out["note"] == "нужен артикль"
    assert client.models.generate_content.call_count == 2
