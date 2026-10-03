import keyboards


def test_main_menu_has_all_buttons():
    kb = keyboards.main_menu()
    labels = {btn.text for row in kb.keyboard for btn in row}
    assert keyboards.BTN_ADD in labels
    assert keyboards.BTN_FLASHCARDS in labels
    assert keyboards.BTN_TRANSLATE in labels
    assert keyboards.BTN_LISTEN in labels
    assert keyboards.BTN_VOCAB in labels


def test_reveal_keyboard_has_callback_data():
    kb = keyboards.reveal_keyboard()
    datas = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "show_answer" in datas


def test_grade_keyboard_has_remember_and_forgot():
    kb = keyboards.grade_keyboard()
    datas = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "grade:remember" in datas
    assert "grade:forgot" in datas


def test_vocab_keyboard_number_buttons_open_cards():
    cards = [{"id": 7}, {"id": 9}]
    kb = keyboards.vocab_keyboard(cards, page=0, total=2)
    first_row = kb.inline_keyboard[0]
    assert [b.text for b in first_row] == ["1", "2"]
    assert [b.callback_data for b in first_row] == ["card:7:0", "card:9:0"]


def test_vocab_keyboard_second_page_numbering_and_nav():
    cards = [{"id": 11}]
    kb = keyboards.vocab_keyboard(cards, page=1, total=6)
    assert kb.inline_keyboard[0][0].text == "6"
    nav = {b.callback_data for b in kb.inline_keyboard[1]}
    assert nav == {"vocab:0"}  # back only; no page 3


def test_vocab_keyboard_no_nav_single_page():
    kb = keyboards.vocab_keyboard([{"id": 1}], page=0, total=1)
    assert len(kb.inline_keyboard) == 1


def test_save_card_keyboard_carries_seq():
    kb = keyboards.save_card_keyboard(7)
    datas = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert datas == {"save:yes:7", "save:no:7"}


def test_card_detail_keyboard_carries_id_and_page():
    kb = keyboards.card_detail_keyboard(5, 2)
    datas = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert datas == {"del:5:2", "vocab:2"}


from datetime import date


def test_more_keyboard_carries_date():
    kb = keyboards.more_keyboard(date(2026, 10, 5))
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "more:2026-10-05" and "Ещё" in btn.text


def test_take_and_clarify_keyboards_carry_seq():
    take = keyboards.take_keyboard(7).inline_keyboard[0]
    assert [b.callback_data for b in take] == ["take:yes:7", "take:no:7"]
    clar = keyboards.clarify_keyboard(3).inline_keyboard[0]
    assert [b.callback_data for b in clar] == ["clarify:answer:3", "clarify:capture:3"]
