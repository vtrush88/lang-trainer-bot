"""Чистая логика ежедневной практики (daily.py)."""
import random
import re
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



# ---- дельта (р): уточнение для длинного английского без слова + stale /next ----

LONG_PROMO = "Our new release ships faster builds and better caching for everyone"


@pytest.mark.parametrize("text, target, stale, expected", [
    ("I gave them a heads-up before the release, as usual", "a heads-up", False, "answer"),
    ("I gave them A HEADS-UP before the release, as usual", "a heads-up", False, "answer"),
    (LONG_PROMO, "a heads-up", False, "clarify"),                 # 6+ слов без цели
    ("one two three four five six", "span", False, "clarify"),    # ровно 6 — порог включён
    ("one two three four five", "span", False, "answer"),         # 5 слов — оценка как раньше
    ("I forgot it", "span", False, "answer"),                     # короткий неверный ответ
    ("They span the river with a bridge every year", "span", True, "clarify"),   # stale — всегда
    ("span", "span", True, "clarify"),
    (LONG_PROMO, None, False, "answer"),                          # target неизвестен — как раньше
    ("не помню", "span", True, "giveup"),                         # giveup раньше stale
    ("anything at all goes here for sure", "span", True, "capture"),  # без задания — сбор
])
def test_classify_incoming_target_and_stale(text, target, stale, expected):
    has_task = expected != "capture"
    assert daily.classify_incoming(text, forwarded=False, has_open_task=has_task,
                                   target=target, stale=stale) == expected


def test_classify_incoming_forwarded_and_plus_beat_stale():
    assert daily.classify_incoming("x", forwarded=True, has_open_task=True,
                                   target="span", stale=True) == "capture"
    assert daily.classify_incoming("+ span", forwarded=False, has_open_task=True,
                                   target="span", stale=True) == "capture"
    assert daily.classify_incoming("/next", forwarded=False, has_open_task=True,
                                   target="span", stale=True) == "ignore"



@pytest.mark.parametrize("text, expected", [
    ("Please give a heads-up to the whole team today", "answer"),     # только слово карточки
    ("Yesterday I GAVE A HEADS-UP to the whole team", "answer"),      # только форма
    ("Our new release ships faster builds and better caching", "clarify"),  # ни того, ни другого
])
def test_classify_incoming_any_of_several_targets(text, expected):
    targets = ("gave a heads-up", "give a heads-up")
    assert daily.classify_incoming(text, forwarded=False, has_open_task=True,
                                   target=targets) == expected


def test_classify_incoming_empty_targets_behave_like_none():
    long = "Our new release ships faster builds and better caching"
    assert daily.classify_incoming(long, forwarded=False, has_open_task=True, target=()) == "answer"
    assert daily.classify_incoming(long, forwarded=False, has_open_task=True,
                                   target=(None, "")) == "answer"

def test_clarify_min_words_and_ttl_constants():
    from datetime import timedelta
    assert daily.CLARIFY_MIN_WORDS == 6
    assert daily.NEXT_TASK_TTL == timedelta(minutes=15)


def _t(*, morning, issued_at):
    return {"morning": int(morning), "issued_at": issued_at}


def test_is_stale():
    from datetime import timedelta, timezone
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    iso = lambda m: (now - timedelta(minutes=m)).isoformat()
    assert daily.is_stale(_t(morning=True, issued_at=iso(600)), now) is False   # утро — никогда
    assert daily.is_stale(_t(morning=False, issued_at=None), now) is False      # старые строки
    assert daily.is_stale(_t(morning=False, issued_at=iso(14)), now) is False
    assert daily.is_stale(_t(morning=False, issued_at=iso(15)), now) is True
    assert daily.is_stale(_t(morning=False, issued_at=iso(60)), now) is True



def test_is_stale_naive_issued_at_is_treated_as_utc():
    from datetime import timedelta, timezone
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    naive = lambda m: (now - timedelta(minutes=m)).replace(tzinfo=None).isoformat()
    assert daily.is_stale(_t(morning=False, issued_at=naive(15)), now) is True
    assert daily.is_stale(_t(morning=False, issued_at=naive(14)), now) is False


def test_is_stale_malformed_issued_at_is_not_stale_and_warns(caplog):
    from datetime import timezone
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    with caplog.at_level("WARNING", logger="daily"):
        assert daily.is_stale({"id": 9001, "morning": 0, "issued_at": "yesterday-ish"}, now) is False
        assert daily.is_stale({"id": 9001, "morning": 0, "issued_at": "yesterday-ish"}, now) is False
    warns = [r for r in caplog.records if "issued_at" in r.getMessage()]
    assert len(warns) == 1                                   # одно предупреждение на задачу

def test_utcnow_is_aware_utc():
    from datetime import timezone
    assert daily._utcnow().tzinfo is not None
    assert daily._utcnow().utcoffset() == timezone.utc.utcoffset(None)


def test_strip_capture_prefix():
    assert daily.strip_capture_prefix("+ heads-up") == "heads-up"
    assert daily.strip_capture_prefix("+heads-up") == "heads-up"
    assert daily.strip_capture_prefix("heads-up") == "heads-up"
    assert daily.strip_capture_prefix("+") == ""


from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")


def test_next_fire_today_if_still_ahead():
    now = datetime(2026, 10, 5, 8, 0, tzinfo=MADRID)
    assert daily.next_fire(now, 9, 30) == datetime(2026, 10, 5, 9, 30, tzinfo=MADRID)


def test_next_fire_tomorrow_if_passed():
    now = datetime(2026, 10, 5, 9, 30, tzinfo=MADRID)
    assert daily.next_fire(now, 9, 30) == datetime(2026, 10, 6, 9, 30, tzinfo=MADRID)


def test_fire_delay_is_absolute_across_dst():
    # 2026-03-29 02:00 → 03:00 в Мадриде: до 09:00 следующего дня реально 23 часа
    now = datetime(2026, 3, 28, 9, 0, tzinfo=MADRID)
    target = daily.next_fire(now, 9, 0)
    assert target == datetime(2026, 3, 29, 9, 0, tzinfo=MADRID)
    assert daily.fire_delay(now, target) == 23 * 3600
    assert (target - now).total_seconds() == 24 * 3600  # вот почему не так


def test_fire_delay_never_negative():
    now = datetime(2026, 10, 5, 9, 0, tzinfo=MADRID)
    assert daily.fire_delay(now, now) == 0.0


def test_should_start_loop_needs_both_gates():
    en = SimpleNamespace(daily_practice=True)
    es = SimpleNamespace(daily_practice=False)
    on = SimpleNamespace(daily_at=time(9, 30))
    off = SimpleNamespace(daily_at=None)
    assert daily.should_start_loop(en, on) is True
    assert daily.should_start_loop(en, off) is False
    assert daily.should_start_loop(es, on) is False   # DAILY_AT в маминой .env — ничего
    assert daily.should_start_loop(es, off) is False


# ---- Task 10: рендеры ----
CARD = {"id": 1, "word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up.",
        "example_translation": "Просто предупреждаю.", "context": "созвон <QA>"}
S = "Can you give me a heads-up before you merge?"
S_RU = "Предупредишь меня перед мержем?"


def test_card_block_has_no_example_and_escapes():
    text = daily.card_block(CARD)
    assert "<b>a heads-up</b>" in text and "предупреждение заранее" in text
    assert "/ˈhedz ʌp/" in text and "📍 созвон &lt;QA&gt;" in text
    assert "Just a heads-up." not in text
    assert "📍" not in daily.card_block({**CARD, "context": None})


def test_render_task_compose_hinted_voice_and_text_fallback():
    t = daily.render_task("compose_hinted", CARD, S, S_RU, "a heads-up", voice_ok=True)
    assert "Напиши своё предложение с <b>a heads-up</b>" in t and S not in t
    t2 = daily.render_task("compose_hinted", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert daily.VOICE_UNAVAILABLE in t2 and f"<i>{S}</i>" in t2
    t3 = daily.render_task("compose_hinted", CARD, None, None, None, voice_ok=True)
    assert daily.VOICE_UNAVAILABLE not in t3 and "Напиши своё предложение" in t3
    t4 = daily.render_task("compose_hinted", CARD, None, None, None, voice_ok=False)
    assert daily.VOICE_UNAVAILABLE in t4 and "<i>" not in t4


def test_render_task_gap_recall_listen_compose():
    gap = daily.render_task("gap", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert "<i>Can you give me ___ before you merge?</i>" in gap and S_RU in gap
    recall = daily.render_task("recall", CARD, S, S_RU, "a heads-up", voice_ok=False)
    assert "«предупреждение заранее»" in recall and "контекст: созвон &lt;QA&gt;" in recall
    assert S not in recall
    recall2 = daily.render_task("recall", {**CARD, "context": None}, None, None, None, voice_ok=False)
    assert "контекст" not in recall2 and "None" not in recall2
    assert daily.render_task("listen", CARD, S, S_RU, "a heads-up", voice_ok=True) == "Напиши то, что услышишь."
    assert daily.render_task("compose", CARD, None, None, None, voice_ok=False) == \
        "Напиши своё предложение с <b>a heads-up</b>."


def test_render_compose_result():
    good = daily.render_compose_result({"verdict": "good", "corrected": "", "note": "ок",
                                        "reply_sentence": "Thanks for the heads-up!",
                                        "reply_sentence_ru": "Спасибо!"})
    assert good.startswith("✅ Отлично, звучит естественно.")
    assert "Моё в ответ: <i>Thanks for the heads-up!</i>" in good
    fix = daily.render_compose_result({"verdict": "fix", "corrected": "I gave the team a heads-up.",
                                       "note": "нужен артикль", "reply_sentence": None,
                                       "reply_sentence_ru": None})
    assert fix == "✅ Почти. Лучше так: I gave the team a heads-up. (нужен артикль)"
    off = daily.render_compose_result({"verdict": "off", "corrected": "", "note": "фраза не использована",
                                       "reply_sentence": None, "reply_sentence_ru": None})
    assert off == "❌ Фраза тут не сработала: фраза не использована."
    assert "лимит" in daily.render_compose_result(None)


def test_render_grade_result():
    assert daily.render_grade_result(None, exact=True, expected="a heads-up", quota=False) == "✅ Верно!"
    typo = daily.render_grade_result({"verdict": "typo", "correct": "a heads-up", "note": "дефис"},
                                     exact=False, expected="a heads-up", quota=False)
    assert typo == "✅ Почти! Правильно: a heads-up (дефис)"
    wrong = daily.render_grade_result({"verdict": "wrong", "correct": "a heads-up", "note": "это другое"},
                                      exact=False, expected="a heads-up", quota=False)
    assert wrong == "❌ Не совсем. Правильно: a heads-up (это другое)"
    quota = daily.render_grade_result(None, exact=False, expected="a heads-up", quota=True)
    assert quota.startswith("❌ Правильно: a heads-up") and "лимит" in quota


def test_render_listen_and_giveup_and_with_sentence():
    assert daily.render_listen_result(True, S) == f"✅ Всё верно: <i>{S}</i>"
    assert daily.render_listen_result(False, S) == f"Почти. Было: <i>{S}</i>"
    g = daily.render_giveup(CARD, S)
    assert g.startswith("Ничего 🙂") and "<b>a heads-up</b>" in g and f"<i>{S}</i>" in g
    assert "📝 пример: Just a heads-up." in g
    assert g.endswith("Вернусь с ней завтра.")
    assert "<i>" not in daily.render_giveup(CARD, None)
    assert daily.with_sentence("x", S) == f"x\n\n🔊 <i>{S}</i>"
    assert daily.with_sentence("x", None) == "x"


def test_texts_are_gender_neutral_and_nothing_found():
    assert daily.TEXT_NOTHING_FOUND.startswith("Не вижу, что тут взять")
    for name in dir(daily):
        if name.startswith("TEXT_"):
            val = getattr(daily, name)
            assert "написала" not in val and "умница" not in val and "Не нашёл" not in val


def test_render_escapes_user_and_model_text():
    assert "&lt;b&gt;" in daily.render_listen_result(False, "<b>x</b>")
    fix = daily.render_compose_result({"verdict": "fix", "corrected": "a<b", "note": "&",
                                       "reply_sentence": "<i>", "reply_sentence_ru": None})
    assert "a&lt;b" in fix and "(&amp;)" in fix and "<i>&lt;i&gt;</i>" in fix


def test_fit_caption_guarantees_limit_without_broken_html():
    short = "<b>ok</b>"
    assert daily.fit_caption(short) == short
    long = "<b>" + "a" * 600 + "</b> &amp; <i>" + "b" * 600 + "</i>"
    out = daily.fit_caption(long)
    assert len(out) <= daily.CAPTION_LIMIT and out.endswith("…")
    assert "<" not in out and "&amp;" in out
    assert len(daily.fit_caption("&" * 1030)) <= daily.CAPTION_LIMIT


def test_fit_caption_two_unclosed_tags_and_entity_at_cut():
    out = daily.fit_caption("<b><i>" + "a" * 1100)           # два незакрытых тега
    assert "<" not in out and len(out) <= daily.CAPTION_LIMIT
    # сущность у границы реза: в plain это «&», заново экранируется целиком
    for pad in range(1010, 1030):
        text = "x" * pad + "&amp;" * 10
        res = daily.fit_caption(text)
        assert len(res) <= daily.CAPTION_LIMIT
        body = res[:-1]
        assert not re.search(r"&(?!amp;|lt;|gt;)", body)     # нет разорванных сущностей
        assert not re.search(r"&[a-z]*$", body.replace("&amp;", ""))


def test_fit_caption_long_text_length_bound():
    assert len(daily.fit_caption("<b>" + "я<&>" * 2000 + "</b>")) <= daily.CAPTION_LIMIT


def test_fit_caption_escape_heavy_keeps_content():
    for text in ("&amp;" * 300 + "x" * 800, "<b>" + "&lt;" * 700 + "</b>"):
        out = daily.fit_caption(text)
        assert 500 < len(out) <= daily.CAPTION_LIMIT and out.endswith("…")
        assert not re.search(r"&(?!amp;|lt;|gt;)", out[:-1])
