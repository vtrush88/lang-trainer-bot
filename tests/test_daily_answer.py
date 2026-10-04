"""Ответ на задание: claim → оценка → перечитать карточку → SRS → finish → отправка."""
import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest

import daily
import db
from languages import PROFILES
from services import grading, sentences, tts
from services.llm import QuotaExceededError
from tests.test_daily_send import FakeBot, _card, fake_tts  # noqa: F401  (fixture reuse)

EN = PROFILES["en"]
U = 111
TODAY = date(2026, 10, 5)
S = "Can you give me a heads-up before you merge?"


def _open(conn, cid, kind, *, sentence=S, phrase_form="a heads-up", morning=True, today=TODAY):
    return db.create_task(conn, user_id=U, card_id=cid, kind=kind, sentence=sentence,
                          sentence_ru="Предупредишь перед мержем?", phrase_form=phrase_form,
                          from_example=False, today=today, morning=morning)


CHECK = {"verdict": "good", "corrected": "", "note": "ок",
         "reply_sentence": "Thanks for the heads-up!", "reply_sentence_ru": "Спасибо!"}


async def test_compose_good_updates_srs_and_sends_reply_voice(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: dict(CHECK))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "I gave the team a heads-up.", TODAY, giveup=False) == "done"
    task = db.get_task(conn, tid)
    assert task["status"] == "answered" and task["answered_ok"] == 1
    assert task["reply_sentence"] == "Thanks for the heads-up!"
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 1 and card["due_at"] == "2026-10-06" and card["reps"] == 1
    assert len(bot.sent) == 1 and bot.sent[0][0] == "voice"
    assert "✅ Отлично" in bot.sent[0][2] and "Thanks for the heads-up!" in bot.sent[0][2]
    assert f"<i>{S}</i>" not in bot.sent[0][2]   # (с): текст подсказки не показываем (отмена (о))
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_compose_off_resets_interval(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence",
                        lambda *a, **k: {**CHECK, "verdict": "off", "note": "фразы нет", "reply_sentence": None})
    cid = _card(conn, interval=14, due=TODAY)
    _open(conn, cid, "compose", sentence=None, phrase_form=None)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "Nothing relevant.", TODAY, giveup=False)
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 1 and card["lapses"] == 1
    assert bot.sent[0][0] == "message" and bot.sent[0][2].startswith("❌ Фраза тут не сработала")


async def test_compose_quota_is_accepted_as_ok(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(sentences, "check_sentence", boom)
    cid = _card(conn)
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "I gave a heads-up.", TODAY, giveup=False)
    assert db.get_card(conn, cid)["interval_days"] == 1
    assert "лимит" in bot.sent[0][2]


async def test_recall_exact_match_skips_gemini_and_speaks_sentence(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=3, due=TODAY)
    _open(conn, cid, "recall")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "A heads-up", TODAY, giveup=False)
    assert bot.sent[0][0] == "voice" and bot.sent[0][2].startswith("✅ Верно!") and S in bot.sent[0][2]
    assert db.get_card(conn, cid)["interval_days"] == 7


async def test_recall_without_sentence_speaks_word(conn, fake_tts, monkeypatch):
    cid = _card(conn, interval=3, due=TODAY)
    _open(conn, cid, "recall", sentence=None, phrase_form=None)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "a heads-up", TODAY, giveup=False)
    assert bot.sent[0][0] == "voice"                       # озвучка слова через send_card_voice_to
    assert db.get_card(conn, cid)["audio_file_id"] == "FID"  # и закэширована


async def test_gap_accepts_only_phrase_form_exactly_else_grades(conn, fake_tts, monkeypatch):
    calls = []
    monkeypatch.setattr(grading, "grade",
                        lambda llm, p, *, prompt_ru, expected, answer: calls.append((prompt_ru, expected, answer))
                        or {"verdict": "wrong", "correct": expected, "note": "форма"})
    cid = _card(conn, interval=1, due=TODAY)
    _open(conn, cid, "gap", phrase_form="gave him a heads-up")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "give someone a heads-up", TODAY, giveup=False)
    assert calls == [("Предупредишь перед мержем?", "gave him a heads-up", "give someone a heads-up")]
    assert bot.sent[0][2].startswith("❌ Не совсем")
    assert db.get_card(conn, cid)["interval_days"] == 1 and db.get_card(conn, cid)["lapses"] == 1


async def test_listen_uses_listen_ok_no_gemini(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=7, due=TODAY)
    _open(conn, cid, "listen")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "can you give me a heads up before you merge", TODAY, giveup=False)
    assert bot.sent[0][0] == "message" and bot.sent[0][2].startswith("✅ Всё верно")
    assert db.get_card(conn, cid)["interval_days"] == 14


async def test_giveup_resets_and_shows_card(conn, fake_tts):
    cid = _card(conn, interval=7, due=TODAY)
    _open(conn, cid, "listen")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "не помню", TODAY, giveup=True)
    assert db.get_card(conn, cid)["interval_days"] == 1
    assert bot.sent[0][0] == "voice" and bot.sent[0][2].startswith("Ничего 🙂")


async def test_answer_for_stale_task_id_is_refused(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("оценки не должно быть"))
    cid = _card(conn)
    old = _open(conn, cid, "compose_hinted", today=date(2026, 10, 4))
    db.expire_task(conn, old)
    _open(conn, cid, "compose", sentence=None, phrase_form=None)      # новая открытая
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False, task_id=old) == "stale"
    assert db.open_task(conn, U)["status"] == "open" and bot.sent == []


async def test_second_answer_is_ignored(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: dict(CHECK))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "first", TODAY, giveup=False, task_id=tid) == "done"
    assert await daily.answer_task(bot, conn, None, EN, U, "second", TODAY, giveup=False, task_id=tid) == "busy"
    assert len(bot.sent) == 1 and db.get_card(conn, cid)["reps"] == 1


async def _inline_to_thread(fn, *args, **kwargs):
    """sqlite3-коннект тестов однопоточный (check_same_thread) — мок, трогающий БД,
    должен исполниться в потоке теста, а не в worker-е to_thread."""
    return fn(*args, **kwargs)


async def test_card_reviewed_manually_meanwhile_keeps_srs(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    cid = _card(conn, interval=3, due=TODAY)
    tid = _open(conn, cid, "recall")

    def grade_and_shift(*a, **k):
        # пока «думает Gemini», ручная тренировка сдвинула карточку
        db.update_review(conn, cid, interval_days=7, due_at=date(2026, 10, 12), remembered=True)
        return {"verdict": "correct", "correct": "a heads-up", "note": "ок"}
    monkeypatch.setattr(grading, "grade", grade_and_shift)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "heads up", TODAY, giveup=False)
    card = db.get_card(conn, cid)
    assert card["interval_days"] == 7 and card["due_at"] == "2026-10-12" and card["reps"] == 2  # 1 из _card + 1 ручная; answer_task не добавил
    assert db.get_task(conn, tid)["status"] == "answered"


async def test_card_deleted_during_grading(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")

    def check_and_delete(*a, **k):
        db.delete_card(conn, cid, user_id=U)
        return dict(CHECK)
    monkeypatch.setattr(sentences, "check_sentence", check_and_delete)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "answered" and db.get_task(conn, tid)["answered_ok"] == 0
    assert bot.sent[-1][2] == daily.TEXT_CARD_DELETED
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_card_deleted_before_answer(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("оценки не должно быть"))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    db.delete_card(conn, cid, user_id=U)
    db.bump_missed(conn, U)
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False) == "done"
    t = db.get_task(conn, tid)
    assert t["status"] == "answered" and t["answered_ok"] == 0
    assert bot.sent[-1][2] == daily.TEXT_CARD_DELETED
    assert db.get_daily_state(conn, U)["missed_streak"] == 0


async def test_morning_during_grading_waits_for_lock(conn, fake_tts, monkeypatch):
    """Утренняя выдача, пришедшая во время оценки, ждёт лок: задачу не истекает, пропуск не растёт."""
    async def slow_to_thread(fn, *args, **kwargs):
        await asyncio.sleep(0.02)
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", slow_to_thread)
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    monkeypatch.setattr(sentences, "make_sentence", lambda *a, **k: {"sentence": "S2", "sentence_ru": "r",
                                                                      "phrase_form": "second"})
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted", today=date(2026, 10, 4))
    _card(conn, word="second", example="A second one.")
    bot = FakeBot()
    answer = asyncio.create_task(daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False))
    await asyncio.sleep(0)   # answer_task успел взять лок и уйти в оценку
    morning = asyncio.create_task(daily.send_daily_task(bot, conn, None, EN, U, TODAY,
                                                        random.Random(1), morning=True))
    await asyncio.gather(answer, morning)
    assert db.get_task(conn, tid)["status"] == "answered"       # не expired
    assert db.get_daily_state(conn, U)["missed_streak"] == 0
    assert db.open_task(conn, U)["card_id"] != cid               # утро выдало следующую карточку


async def test_grading_failure_releases_task(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise sentences.SentenceError("junk")
    monkeypatch.setattr(sentences, "check_sentence", boom)
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "open"
    assert db.get_card(conn, cid)["reps"] == 0
    assert bot.sent[-1][2] == daily.TEXT_GRADE_FAILED


async def test_send_failure_after_finish_keeps_srs(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    bot = FakeBot(fail_for={U})
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert db.get_task(conn, tid)["status"] == "answered"
    assert db.get_card(conn, cid)["reps"] == 1


async def test_more_button_only_when_queue_and_limit_allow(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    _card(conn, word="another one", example="Another one here.")
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert bot.sent[-1][3]["reply_markup"].inline_keyboard[0][0].callback_data == "more:2026-10-05"
    # очередь пуста → кнопки нет
    cid3 = _card(conn, word="third", example="Third one here.")
    for w in ("another one",):
        row = conn.execute("SELECT id FROM cards WHERE word = ?", (w,)).fetchone()
        db.update_review(conn, row["id"], interval_days=30, due_at=date(2026, 11, 5), remembered=True)
    db.update_review(conn, cid3, interval_days=30, due_at=date(2026, 11, 5), remembered=True)
    _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)
    await daily.answer_task(bot, conn, None, EN, U, "y", TODAY, giveup=False)
    assert bot.sent[-1][3].get("reply_markup") is None


async def test_more_button_hidden_at_daily_limit(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    cid = _card(conn)
    _card(conn, word="another one", example="Another one here.")
    for _ in range(2):   # две уже выданы и закрыты сегодня
        t = _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)
        db.claim_task(conn, t); db.finish_task(conn, t, ok=True)
    _open(conn, cid, "compose", sentence=None, phrase_form=None, morning=False)   # третья
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert bot.sent[-1][3].get("reply_markup") is None


async def test_recall_quota_shows_expected_and_counts_wrong(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(grading, "grade", boom)
    cid = _card(conn, interval=3, due=TODAY)
    _open(conn, cid, "recall")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "heads", TODAY, giveup=False)
    assert "лимит" in bot.sent[0][2] and "a heads-up" in bot.sent[0][2]
    assert db.get_card(conn, cid)["interval_days"] == 1


async def test_compose_avoid_comes_from_recent_sentences(conn, fake_tts, monkeypatch):
    seen = {}

    def spy(llm, profile, card, answer, avoid, hint=None):
        seen["avoid"] = avoid
        return {**CHECK, "reply_sentence": None}
    monkeypatch.setattr(sentences, "check_sentence", spy)
    cid = _card(conn)
    _open(conn, cid, "compose_hinted")
    await daily.answer_task(FakeBot(), conn, None, EN, U, "x", TODAY, giveup=False)
    assert seen["avoid"] == [S]


def test_more_button_allowed_false_without_due_card(conn):
    assert daily.more_button_allowed(conn, U, TODAY) is False


async def test_answer_for_task_in_grading_is_busy_not_stale(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("оценки не должно быть"))
    cid = _card(conn)
    tid = _open(conn, cid, "compose_hinted")
    db.claim_task(conn, tid)                       # первая оценка ещё идёт
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False, task_id=tid) == "busy"
    assert db.get_task(conn, tid)["status"] == "grading" and bot.sent == []


async def test_answer_for_foreign_or_missing_task_is_stale(conn, fake_tts):
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False, task_id=999) == "stale"
    assert await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False) == "stale"


async def test_gap_full_sentence_typed_is_correct_without_gemini(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=1, due=TODAY)
    _open(conn, cid, "gap")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "can you give me a heads up before you merge",
                            TODAY, giveup=False)
    assert bot.sent[0][2].startswith("✅ Верно!")
    assert db.get_card(conn, cid)["interval_days"] == 3 and db.get_card(conn, cid)["lapses"] == 0


async def test_compose_hinted_quota_result_has_no_hint_sentence(conn, fake_tts, monkeypatch):
    def boom(*a, **k):
        raise QuotaExceededError("q")
    monkeypatch.setattr(sentences, "check_sentence", boom)
    _open(conn, _card(conn), "compose_hinted")
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "I gave a heads-up.", TODAY, giveup=False)
    assert "лимит" in bot.sent[0][2] and f"<i>{S}</i>" not in bot.sent[0][2]


async def test_compose_without_sentence_result_has_no_hint_block(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: {**CHECK, "reply_sentence": None})
    _open(conn, _card(conn), "compose_hinted", sentence=None, phrase_form=None)
    bot = FakeBot()
    await daily.answer_task(bot, conn, None, EN, U, "x", TODAY, giveup=False)
    assert bot.sent[0][2] == "✅ Отлично, звучит естественно."


# ---- (т) ответ, скопированный с подсказки, не засчитывается ----

async def _copy_case(conn, fake_tts, monkeypatch, kind, answer):
    monkeypatch.setattr(sentences, "check_sentence", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=3, due=TODAY)
    tid = _open(conn, cid, kind)
    db.bump_missed(conn, U)
    before = dict(db.get_card(conn, cid))
    bot = FakeBot()
    result = await daily.answer_task(bot, conn, None, EN, U, answer, TODAY, giveup=False)
    return cid, tid, before, bot, result


@pytest.mark.parametrize("answer", [S, "can you give me a HEADS-UP before you merge", "  " + S.upper() + "! "])
async def test_compose_hinted_copy_of_hint_is_retry(conn, fake_tts, monkeypatch, answer):
    cid, tid, before, bot, result = await _copy_case(conn, fake_tts, monkeypatch, "compose_hinted", answer)
    assert result == "retry"
    assert db.get_task(conn, tid)["status"] == "open"
    assert dict(db.get_card(conn, cid)) == before
    assert [m[2] for m in bot.sent] == [daily.TEXT_COPIED_HINT]
    assert db.get_daily_state(conn, U)["missed_streak"] == 1


async def test_compose_copy_of_sentence_is_retry_too(conn, fake_tts, monkeypatch):
    _, tid, _, bot, result = await _copy_case(conn, fake_tts, monkeypatch, "compose", S)
    assert result == "retry" and db.get_task(conn, tid)["status"] == "open"


async def test_compose_hinted_answer_differing_by_words_is_graded_with_hint(conn, fake_tts, monkeypatch):
    seen = {}

    def fake(llm, profile, card, answer, avoid, hint=None):
        seen["hint"] = hint
        return dict(CHECK)
    monkeypatch.setattr(sentences, "check_sentence", fake)
    cid = _card(conn)
    _open(conn, cid, "compose_hinted")
    bot = FakeBot()
    assert await daily.answer_task(bot, conn, None, EN, U, "Please give me a heads-up before the release.",
                                   TODAY, giveup=False) == "done"
    assert seen["hint"] == S


async def test_gap_full_sentence_still_correct_after_copy_rule(conn, fake_tts, monkeypatch):
    monkeypatch.setattr(grading, "grade", lambda *a, **k: pytest.fail("Gemini не нужен"))
    cid = _card(conn, interval=3, due=TODAY)
    tid = _open(conn, cid, "gap")
    assert await daily.answer_task(FakeBot(), conn, None, EN, U, S, TODAY, giveup=False) == "done"
    assert db.get_task(conn, tid)["answered_ok"] == 1
