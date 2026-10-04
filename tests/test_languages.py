"""Профили языков. Главный тест — регрессия мамы: es-профиль байт-в-байт
равен литералам, которые жили в services/enrichment.py, services/grading.py,
handlers/menu.py, handlers/add.py, handlers/training.py, services/tts.py
до обобщения. Эталоны скопированы сюда дословно — НЕ переформатировать."""
from __future__ import annotations

import languages
from languages import PROFILES

ES_ENRICHMENT_SYSTEM = (
    "Ты помогаешь русскоязычному новичку учить испанский язык Испании "
    "(европейский, кастильский — НЕ латиноамериканский вариант). "
    "На вход даётся слово или фраза на испанском ИЛИ на русском. "
    "Определи язык. Верни испанский вариант (spanish) в варианте Испании — "
    "используй пиренейскую лексику (coche, ordenador, móvil, zumo, vale, "
    "vosotros и т.п.), НЕ латиноамериканскую (carro, computadora, celular, jugo). "
    "Дай русский перевод (russian). "
    "transcription — произношение ТОЛЬКО русскими буквами, с ударением "
    "(ударную гласную пиши заглавной). Передавай звуки ЕДИНООБРАЗНО: "
    "ll и y → «й» (calle→кАйе, llave→йАвэ, pollo→пОйо, paella→паЭйя, lluvia→йУвиа); "
    "ñ → «нь» (España→эспАнья, año→Аньо); "
    "j, и g перед e/i → «х» (jamón→хамОн, gente→хЭнте); "
    "h не читается (hola→Ола); "
    "c и z перед e/i → «с» без межзубного (cerveza→сервЭса, gracias→грАсиас). "
    "Добавь короткий "
    "пример-предложение на испанском Испании (example_es) с переводом (example_ru). "
    "Поле kind = 'word' для одного слова, 'phrase' для фразы/предложения. "
    "Всё кратко и для начинающего."
)

ES_ENRICHMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["word", "phrase"]},
        "spanish": {"type": "STRING"},
        "russian": {"type": "STRING"},
        "transcription": {"type": "STRING"},
        "example_es": {"type": "STRING"},
        "example_ru": {"type": "STRING"},
    },
    "required": ["kind", "spanish", "russian", "transcription",
                 "example_es", "example_ru"],
}

ES_GRADING_SYSTEM = (
    "Ты мягко проверяешь, как русскоязычный новичок перевёл слово/фразу на "
    "испанский. Тебе дают: русский запрос, ожидаемый испанский перевод и ответ "
    "ученика. Оцени verdict: 'correct' (всё верно), 'typo' (правильно по сути, "
    "но мелкая опечатка или пропущенный акцент), 'wrong' (неверно). В "
    "correct_spanish дай правильное написание. В note — короткая ДОБАВЛЯЮЩАЯ "
    "подсказка по-русски: для 'typo'/'wrong' — что именно не так (например "
    "«пропущен акцент», «лишняя буква», «это слово значит …»); для 'correct' — "
    "короткое ободрение или крошечный факт. НЕ дублируй вердикт: слова «верно», "
    "«правильно», «почти» ученик уже видит отдельно, в note их не повторяй. "
    "Пол ученика неизвестен — без гендерных форм в его адрес "
    "(не «написала», «умница»)."
)

ES_GRADING_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["correct", "typo", "wrong"]},
        "correct_spanish": {"type": "STRING"},
        "note": {"type": "STRING"},
    },
    "required": ["verdict", "correct_spanish", "note"],
}

ES_GRADING_USER_TEMPLATE = (
    "Русский запрос: {prompt_ru}\n"
    "Ожидаемый испанский: {expected}\n"
    "Ответ ученика: {answer}"
)

ES_GREETING = (
    "¡Hola! 🌞 Я помогу учить испанский.\n\n"
    "• «➕ Добавить слово» — пришли слово или фразу, я переведу, озвучу и "
    "запомню.\n"
    "• «🎴 Карточки», «✍️ Проверить себя», «🎧 Аудирование» — тренировки.\n"
    "• «📖 Мой словарь» — все добавленные слова."
)

ES_ADD_INTRO = (
    "Пиши слова или фразы — по одному, на испанском или русском 🙂 "
    "Я сохраню каждое. Когда закончишь, выбери что-нибудь в меню внизу."
)


def test_es_profile_is_byte_identical_to_legacy_literals():
    es = PROFILES["es"]
    assert es.code == "es"
    assert es.tts_voice == "es-ES-XimenaNeural"
    assert es.enrichment_system == ES_ENRICHMENT_SYSTEM
    assert es.enrichment_schema == ES_ENRICHMENT_SCHEMA
    assert es.grading_system == ES_GRADING_SYSTEM
    assert es.grading_schema == ES_GRADING_SCHEMA
    assert es.grading_user_template == ES_GRADING_USER_TEMPLATE
    assert es.greeting == ES_GREETING
    assert es.add_intro == ES_ADD_INTRO
    assert es.translate_question == "Как по-испански: «{}»?"


def test_es_key_map_translates_legacy_llm_keys():
    m = PROFILES["es"].llm_key_map
    assert m == {
        "spanish": "word",
        "russian": "translation",
        "example_es": "example",
        "example_ru": "example_translation",
        "correct_spanish": "correct",
    }


def test_en_profile_basics():
    en = PROFILES["en"]
    assert en.code == "en"
    assert en.tts_voice == "en-US-EmmaNeural"
    assert en.llm_key_map == {}  # ключи en-схем уже нейтральные
    assert "IPA" in en.enrichment_system
    assert "американ" in en.enrichment_system.lower()
    assert "русскими буквами" in en.enrichment_system  # запрет упомянут явно
    assert set(en.enrichment_schema["properties"]) == {
        "kind", "word", "translation", "transcription",
        "example", "example_translation",
    }
    assert set(en.grading_schema["properties"]) == {"verdict", "correct", "note"}
    assert en.translate_question == "Как по-английски: «{}»?"
    assert "Ожидаемый английский" in en.grading_user_template


def test_profiles_are_complete_and_wellformed():
    assert set(PROFILES) == {"es", "en"}
    for profile in PROFILES.values():
        for field in ("code", "tts_voice", "enrichment_system", "grading_system",
                      "grading_user_template", "greeting", "add_intro",
                      "translate_question"):
            assert getattr(profile, field), f"{profile.code}.{field} пуст"
        assert "{}" in profile.translate_question
        for placeholder in ("{prompt_ru}", "{expected}", "{answer}"):
            assert placeholder in profile.grading_user_template
        assert profile.enrichment_schema["required"] == list(
            profile.enrichment_schema["properties"])
        assert profile.grading_schema["required"] == list(
            profile.grading_schema["properties"])
        # note: гендер-нейтральность требуется в обоих grading-промптах
        assert "Пол ученика неизвестен" in profile.grading_system


def _schema_ok(schema):
    assert schema["type"] == "OBJECT"
    assert set(schema["required"]) <= set(schema["properties"])


def test_es_profile_has_no_daily_practice():
    es = PROFILES["es"]
    assert es.daily_practice is False
    assert es.sentence_system == "" and es.sentence_schema is None
    assert es.capture_system == "" and es.capture_schema is None


def test_en_daily_prompts_and_schemas():
    en = PROFILES["en"]
    assert en.daily_practice is True
    for text in (en.sentence_system, en.sentence_check_system, en.capture_system,
                 en.sentence_user_template, en.sentence_check_user_template):
        assert text.strip()
    _schema_ok(en.sentence_schema)
    assert set(en.sentence_schema["required"]) == {"sentence", "sentence_ru", "phrase_form"}
    _schema_ok(en.sentence_check_schema)
    assert en.sentence_check_schema["properties"]["verdict"]["enum"] == ["good", "fix", "off"]
    assert {"reply_sentence", "reply_phrase_form"} <= set(en.sentence_check_schema["required"])


def test_en_capture_schema_is_object_with_items_array():
    en = PROFILES["en"]
    _schema_ok(en.capture_schema)
    assert en.capture_schema["required"] == ["items"]
    items = en.capture_schema["properties"]["items"]
    assert items["type"] == "ARRAY"
    item = items["items"]
    assert item["type"] == "OBJECT"
    assert {"word", "translation", "transcription", "example",
            "example_translation", "context"} <= set(item["required"])
    assert "usage" in item["properties"] and "usage" not in item["required"]


def test_en_templates_format_with_expected_keys():
    en = PROFILES["en"]
    s = en.sentence_user_template.format(word="a heads-up", translation="п", context="к",
                                         kind="gap", avoid="—")
    assert "a heads-up" in s and "gap" in s
    c = en.sentence_check_user_template.format(word="a heads-up", translation="п", context="к",
                                               answer="I gave a heads-up.", avoid="—", hint="—")
    assert "I gave a heads-up." in c


def test_daily_prompts_are_gender_neutral():
    en = PROFILES["en"]
    for text in (en.sentence_system, en.sentence_check_system, en.capture_system):
        assert "Пол ученика неизвестен" in text


def test_command_menu_flag_default_off_es_off_en_on():
    import dataclasses
    from languages import LanguageProfile
    field = {f.name: f for f in dataclasses.fields(LanguageProfile)}["command_menu"]
    assert field.default is False
    assert PROFILES["es"].command_menu is False
    assert PROFILES["en"].command_menu is True


def test_en_texts_point_to_commands_not_buttons():
    en = PROFILES["en"]
    for cmd in ("/next", "/add", "/vocab", "/cards", "/check", "/listen"):
        assert cmd in en.greeting, cmd
    assert "«+»" in en.greeting   # английский текст можно прислать с «+» — бот предложит сохранить
    assert "➕ Добавить слово" not in en.greeting and "📖 Мой словарь" not in en.greeting
    assert "меню внизу" not in en.add_intro
    assert "/" in en.add_intro


def test_en_sentence_check_prompt_asks_reply_to_learner_sentence():
    en = PROFILES["en"]
    assert "ответная реплика собеседника" in en.sentence_check_system
    assert "what else are you planning" in en.sentence_check_system


def test_en_check_prompt_knows_hint_and_copy_rule():
    en = PROFILES["en"]
    assert "{hint}" in en.sentence_check_user_template
    assert "Подсказка (предложение из задания): {hint}" in en.sentence_check_user_template
    assert "копией или лёгкой переделкой подсказки" in en.sentence_check_system
    assert "это предложение из подсказки, нужно своё" in en.sentence_check_system
