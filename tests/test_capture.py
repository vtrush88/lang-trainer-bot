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
