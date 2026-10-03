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
