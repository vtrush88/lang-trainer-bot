"""Языковые профили: всё языкозависимое в одном месте.

Профиль выбирается конфигом (BOT_LANG) и внедряется через dp["profile"].
es-литералы обязаны быть байт-в-байт равны тем, что жили в сервисах и
хендлерах до обобщения (инвариант маминого бота) — их фиксирует
tests/test_languages.py. Не редактировать без сознательного решения
поменять поведение живого испанского бота.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    tts_voice: str
    enrichment_system: str
    enrichment_schema: dict
    grading_system: str
    grading_schema: dict
    # .format(prompt_ru=…, expected=…, answer=…)
    grading_user_template: str
    # ключ ответа модели -> нейтральный ключ приложения; прочие ключи as-is
    llm_key_map: dict
    greeting: str
    add_intro: str
    translate_question: str  # шаблон с {}
    # True — без reply-клавиатуры, разделы через команды Telegram «/» (только en; у es дефолт)
    command_menu: bool = False
    # --- ежедневная практика (только en; у es дефолты) ---
    daily_practice: bool = False
    sentence_system: str = ""
    sentence_schema: dict | None = None
    # .format(word=…, translation=…, context=…, kind=…, avoid=…)
    sentence_user_template: str = ""
    sentence_check_system: str = ""
    sentence_check_schema: dict | None = None
    # .format(word=…, translation=…, context=…, answer=…, avoid=…)
    sentence_check_user_template: str = ""
    capture_system: str = ""
    capture_schema: dict | None = None


ES = LanguageProfile(
    code="es",
    tts_voice="es-ES-XimenaNeural",
    enrichment_system=(
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
    ),
    enrichment_schema={
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
    },
    grading_system=(
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
    ),
    grading_schema={
        "type": "OBJECT",
        "properties": {
            "verdict": {"type": "STRING", "enum": ["correct", "typo", "wrong"]},
            "correct_spanish": {"type": "STRING"},
            "note": {"type": "STRING"},
        },
        "required": ["verdict", "correct_spanish", "note"],
    },
    grading_user_template=(
        "Русский запрос: {prompt_ru}\n"
        "Ожидаемый испанский: {expected}\n"
        "Ответ ученика: {answer}"
    ),
    llm_key_map={
        "spanish": "word",
        "russian": "translation",
        "example_es": "example",
        "example_ru": "example_translation",
        "correct_spanish": "correct",
    },
    greeting=(
        "¡Hola! 🌞 Я помогу учить испанский.\n\n"
        "• «➕ Добавить слово» — пришли слово или фразу, я переведу, озвучу и "
        "запомню.\n"
        "• «🎴 Карточки», «✍️ Проверить себя», «🎧 Аудирование» — тренировки.\n"
        "• «📖 Мой словарь» — все добавленные слова."
    ),
    add_intro=(
        "Пиши слова или фразы — по одному, на испанском или русском 🙂 "
        "Я сохраню каждое. Когда закончишь, выбери что-нибудь в меню внизу."
    ),
    translate_question="Как по-испански: «{}»?",
)

_EN_SENTENCE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sentence": {"type": "STRING"},
        "sentence_ru": {"type": "STRING"},
        "phrase_form": {"type": "STRING"},
    },
    "required": ["sentence", "sentence_ru", "phrase_form"],
}

_EN_SENTENCE_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["good", "fix", "off"]},
        "corrected": {"type": "STRING"},
        "note": {"type": "STRING"},
        "reply_sentence": {"type": "STRING"},
        "reply_sentence_ru": {"type": "STRING"},
        "reply_phrase_form": {"type": "STRING"},
    },
    "required": ["verdict", "corrected", "note", "reply_sentence",
                 "reply_sentence_ru", "reply_phrase_form"],
}

_EN_CAPTURE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING", "enum": ["word", "phrase"]},
                    "word": {"type": "STRING"},
                    "translation": {"type": "STRING"},
                    "transcription": {"type": "STRING"},
                    "example": {"type": "STRING"},
                    "example_translation": {"type": "STRING"},
                    "context": {"type": "STRING"},
                    "usage": {"type": "STRING"},
                },
                "required": ["kind", "word", "translation", "transcription",
                             "example", "example_translation", "context"],
            },
        },
    },
    "required": ["items"],
}


EN = LanguageProfile(
    code="en",
    tts_voice="en-US-EmmaNeural",
    enrichment_system=(
        "Ты помогаешь русскоязычному ученику среднего уровня (B1-B2) учить "
        "американский английский. На вход даётся слово или фраза на английском "
        "ИЛИ на русском. Определи язык. Верни английский вариант (word) в "
        "американском варианте — американская лексика и спеллинг (apartment, "
        "elevator, color, fall, cookie), НЕ британские (flat, lift, colour, "
        "autumn, biscuit). Дай русский перевод (translation). "
        "transcription — транскрипция IPA в слэшах, вариант General American, "
        "со знаком ударения ˈ для многосложных слов: thought → /θɔːt/, "
        "apartment → /əˈpɑːrtmənt/, comfortable → /ˈkʌmftərbəl/. "
        "НЕ русскими буквами и НЕ британское произношение. "
        "Добавь пример-предложение на английском (example) уровнем чуть выше "
        "среднего (B2+): живые разговорные конструкции, фразовые глаголы, "
        "естественные коллокации — и его русский перевод (example_translation). "
        "Перевод и пояснения — простые, по-русски. "
        "Поле kind = 'word' для одного слова, 'phrase' для фразы/предложения. "
        "Всё кратко."
    ),
    enrichment_schema={
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "enum": ["word", "phrase"]},
            "word": {"type": "STRING"},
            "translation": {"type": "STRING"},
            "transcription": {"type": "STRING"},
            "example": {"type": "STRING"},
            "example_translation": {"type": "STRING"},
        },
        "required": ["kind", "word", "translation", "transcription",
                     "example", "example_translation"],
    },
    grading_system=(
        "Ты мягко проверяешь, как русскоязычный ученик перевёл слово/фразу на "
        "английский. Тебе дают: русский запрос, ожидаемый английский перевод и "
        "ответ ученика. Оцени verdict: 'correct' (всё верно), 'typo' (правильно "
        "по сути, но мелкая опечатка — пропущенный апостроф, удвоенная или "
        "пропущенная буква, неверное окончание), 'wrong' (неверно). В correct "
        "дай правильное написание. В note — короткая ДОБАВЛЯЮЩАЯ подсказка "
        "по-русски: для 'typo'/'wrong' — что именно не так (например «пропущен "
        "апостроф», «лишняя буква», «это слово значит …»); для 'correct' — "
        "короткое ободрение или крошечный факт. НЕ дублируй вердикт: слова "
        "«верно», «правильно», «почти» ученик уже видит отдельно, в note их не "
        "повторяй. Пол ученика неизвестен — без гендерных форм в его адрес "
        "(не «написала», «умница»)."
    ),
    grading_schema={
        "type": "OBJECT",
        "properties": {
            "verdict": {"type": "STRING", "enum": ["correct", "typo", "wrong"]},
            "correct": {"type": "STRING"},
            "note": {"type": "STRING"},
        },
        "required": ["verdict", "correct", "note"],
    },
    grading_user_template=(
        "Русский запрос: {prompt_ru}\n"
        "Ожидаемый английский: {expected}\n"
        "Ответ ученика: {answer}"
    ),
    llm_key_map={},
    greeting=(
        "Hi! 🌞 Я помогу учить английский. Все разделы — в меню «/»:\n\n"
        "• /next — задание сейчас.\n"
        "• /add — пришли слово или фразу, я переведу, озвучу и запомню.\n"
        "• /cards, /check, /listen — тренировки: карточки, проверить себя, аудирование.\n"
        "• /vocab — мой словарь.\n\n"
        "Английский текст можно просто переслать сюда или написать с «+» в начале — "
        "предложу сохранить фразы из него."
    ),
    add_intro=(
        "Пиши слова или фразы — по одному, на английском или русском 🙂 "
        "Я сохраню каждое. Когда закончишь, выбери другую команду в меню «/» "
        "(например, /next или /vocab)."
    ),
    translate_question="Как по-английски: «{}»?",
    command_menu=True,
    daily_practice=True,
    sentence_system=(
        "Ты пишешь ОДНО предложение на американском английском уровня B2 для "
        "русскоязычного ученика, который учит фразу. Тебе дают фразу, её перевод, "
        "контекст, откуда она пришла (тема), вид задания и список предложений, "
        "которые НЕЛЬЗЯ повторять. Правила: предложение живое и естественное, "
        "8–16 слов, фраза употреблена в нём точно и уместно; тема предложения — "
        "из указанного контекста (если контекст — это пример-предложение, держись "
        "его темы), не абстрактная и не «про кота»; для вида задания gap фраза "
        "должна стоять в предложении дословно, чтобы её можно было вырезать; "
        "для вида reply предложение — ответная реплика собеседника на фразу "
        "ученика, которая дана в контексте. "
        "Верни: sentence — предложение; sentence_ru — его естественный русский "
        "перевод; phrase_form — ТОЧНАЯ подстрока sentence, в которой употреблена "
        "фраза (с теми же буквами и формой слов, как в sentence). Не повторяй "
        "предложения из списка «не повторять» и не делай их перефразом. "
        "Пол ученика неизвестен — без гендерных форм в его адрес."
    ),
    sentence_schema=_EN_SENTENCE_SCHEMA,
    sentence_user_template=(
        "Фраза: {word}\n"
        "Перевод: {translation}\n"
        "Контекст (тема): {context}\n"
        "Вид задания: {kind}\n"
        "Не повторять:\n{avoid}"
    ),
    sentence_check_system=(
        "Ты мягко проверяешь предложение, которое русскоязычный ученик уровня B2 "
        "сам составил на американском английском с заданной фразой. Тебе дают "
        "фразу, её перевод, контекст и предложение ученика. Оцени verdict: 'good' "
        "— предложение естественное и фраза употреблена верно; 'fix' — фраза на "
        "месте, но есть грамматическая/лексическая ошибка или неестественность; "
        "'off' — фраза не использована, использована в другом смысле или "
        "предложение не на английском. В corrected — исправленный вариант "
        "предложения ученика (для 'good' повтори его как есть). В note — короткая "
        "ДОБАВЛЯЮЩАЯ подсказка по-русски: для 'fix'/'off' — что именно не так "
        "(«нужен артикль», «после … идёт герундий»); для 'good' — крошечный факт "
        "или ободрение. НЕ дублируй вердикт словами «верно», «почти». Затем "
        "напиши ответную реплику: reply_sentence — ответная реплика собеседника "
        "на предложение ученика, по смыслу и теме то же, что сказал ученик, как "
        "реплика в живом диалоге (вопрос, уточнение, продолжение мысли), "
        "обязательно с целевой фразой, уровень B2, американский английский, не "
        "повторяющая ни предложение ученика, ни список «не повторять». Пример (только "
        "образец, не копируй его): ученик «I made this bot to pursue my goal to speak English better.» → "
        "реплика «Nice — what else are you planning to pursue once your English "
        "feels solid?». reply_sentence_ru — русский перевод реплики; "
        "reply_phrase_form — ТОЧНАЯ подстрока reply_sentence с фразой. "
        "Пол ученика неизвестен — без гендерных форм в его адрес "
        "(не «написала», «умница»)."
    ),
    sentence_check_schema=_EN_SENTENCE_CHECK_SCHEMA,
    sentence_check_user_template=(
        "Фраза: {word}\n"
        "Перевод: {translation}\n"
        "Контекст (тема): {context}\n"
        "Предложение ученика: {answer}\n"
        "Не повторять:\n{avoid}"
    ),
    capture_system=(
        "Ты помогаешь русскоязычному ученику уровня B2 собирать английские фразы "
        "из его реальной жизни. На вход — свободный текст: пересланное сообщение, "
        "кусок переписки, заметка после созвона, возможно с пояснением по-русски. "
        "Задача — вернуть items: от 0 до 3 фраз, которые стоит выучить. Если в "
        "тексте есть явный указатель (одиночное слово/фраза, кавычки, «не поняла "
        "X», «что значит X») — верни РОВНО эту фразу, одну. Иначе выбери до 3 "
        "самых полезных для B2 кусков: коллокации, фразовые глаголы, устойчивые "
        "обороты — НЕ одиночные частотные слова. Для каждого: kind ('word' или "
        "'phrase'); word — фраза в словарной форме на американском английском "
        "(a heads-up, give someone a heads-up); translation — русский перевод; "
        "transcription — IPA в слэшах, General American; example — пример-"
        "предложение B2+ с этой фразой; example_translation — его перевод; "
        "context — ДО 60 символов по-русски, откуда/о чём была фраза, строго из "
        "текста, без выдумок (например «рабочий созвон, перенос релиза»); если "
        "источник не назван — тема самого сообщения; context не может быть "
        "пустым. usage — короткая строка «обычно: …» с типичным употреблением "
        "(можно пустую). Если учить нечего — items пустой. "
        "Пол ученика неизвестен — без гендерных форм в его адрес."
    ),
    capture_schema=_EN_CAPTURE_SCHEMA,
)

PROFILES: dict[str, LanguageProfile] = {"es": ES, "en": EN}
