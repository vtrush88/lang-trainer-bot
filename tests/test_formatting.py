import sqlite3

import db  # noqa: F401
import formatting
from formatting import card_preview, esc, field


def test_card_preview_includes_all_fields():
    text = formatting.card_preview({
        "word": "comida", "translation": "еда", "transcription": "комИда",
        "example": "La comida está lista.", "example_translation": "Еда готова.",
    })
    assert "comida" in text
    assert "еда" in text
    assert "комИда" in text
    assert "La comida está lista." in text


def test_card_preview_bolds_spanish_word():
    text = formatting.card_preview({
        "word": "comida", "translation": "еда", "transcription": "комИда",
        "example": "La comida está lista.", "example_translation": "Еда готова.",
    })
    assert "<b>comida</b>" in text


def test_card_preview_escapes_html_special_chars():
    # Raw <, >, & in the data must be escaped, or Telegram's HTML parser breaks.
    text = formatting.card_preview({
        "word": "tú & yo", "translation": "ты <и> я", "transcription": "ту и йо",
        "example": "a < b & c", "example_translation": "пример",
    })
    assert "<b>tú &amp; yo</b>" in text   # word escaped, then bolded
    assert "ты &lt;и&gt; я" in text
    assert "a &lt; b &amp; c" in text


def test_answer_reveal():
    text = formatting.answer_reveal({"translation": "еда", "transcription": "комИда"})
    assert "еда" in text and "комИда" in text


def test_word_list_line_numbered():
    line = formatting.word_list_line(3, {"word": "agua", "translation": "вода"})
    assert line.startswith("3.")
    assert "agua" in line and "вода" in line


def test_vocab_title_single_page():
    assert formatting.vocab_title(0, 1, 4) == "📖 Твой словарь (4 слова)"


def test_vocab_title_multi_page():
    assert formatting.vocab_title(1, 3, 12) == "📖 Твой словарь (стр. 2/3, 12 слов)"


def test_vocab_title_plural_forms():
    assert formatting.vocab_title(0, 1, 1).endswith("(1 слово)")
    assert formatting.vocab_title(0, 1, 21).endswith("(21 слово)")
    assert formatting.vocab_title(0, 1, 3).endswith("(3 слова)")
    assert formatting.vocab_title(0, 3, 11).endswith("11 слов)")


CARD = {"word": "a heads-up", "translation": "предупреждение заранее",
        "transcription": "/ˈhedz ʌp/", "example": "Just a heads-up.",
        "example_translation": "Просто предупреждаю."}


def test_card_preview_without_context_is_unchanged():
    text = card_preview(CARD)
    assert "📍" not in text
    assert text == (
        "🔤 <b>a heads-up</b>\n"
        "🇷🇺 предупреждение заранее\n"
        "🗣 произношение: /ˈhedz ʌp/\n"
        "📝 пример: Just a heads-up. — Просто предупреждаю."
    )


def test_card_preview_with_context_adds_line():
    text = card_preview({**CARD, "context": "созвон <QA>"})
    assert text.endswith("\n📍 контекст: созвон &lt;QA&gt;")


def test_card_preview_empty_context_is_skipped():
    assert "📍" not in card_preview({**CARD, "context": ""})


def test_esc_escapes_html_but_not_quotes():
    assert esc("<a&b>") == "&lt;a&amp;b&gt;"
    assert esc('say "hi"') == 'say "hi"'


def test_field_reads_dict_and_row_safely():
    assert field(CARD, "context") is None
    assert field({**CARD, "context": "x"}, "context") == "x"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 'w' AS word").fetchone()
    assert field(row, "word") == "w"
    assert field(row, "context") is None
