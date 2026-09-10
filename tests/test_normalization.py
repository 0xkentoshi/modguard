from app.utils.text_normalization import analyze_text, normalize_text


def test_normal_text_is_preserved():
    result = analyze_text("Привет, как дела?")

    assert result.raw_text == "Привет, как дела?"
    assert result.normalized_text == "Привет, как дела?"
    assert result.contains_invisible_chars is False
    assert result.mixed_script_tokens == []


def test_english_text_is_preserved():
    result = analyze_text("Hello everyone")

    assert result.normalized_text == "Hello everyone"
    assert result.mixed_script_tokens == []


def test_nfkc_normalizes_fullwidth_characters():
    result = normalize_text("ＦＲＥＥ ＵＳＤＴ")

    assert result == "FREE USDT"


def test_zero_width_characters_are_removed():
    text = "s\u200bc\u200ba\u200bm"

    result = analyze_text(text)

    assert result.raw_text == text
    assert result.normalized_text == "scam"
    assert result.contains_invisible_chars is True
    assert result.invisible_char_count == 3


def test_mixed_cyrillic_latin_token_is_detected():
    # Вторая буква визуально похожа на c,
    # но фактически это кириллическая "с".
    text = "sсam"

    result = analyze_text(text)

    assert "sсam" in result.mixed_script_tokens
    assert "LATIN" in result.scripts
    assert "CYRILLIC" in result.scripts


def test_normal_multilingual_message_is_not_mixed_token():
    result = analyze_text("Привет OpenAI")

    # Сообщение мультиязычное — это нормально.
    assert "CYRILLIC" in result.scripts
    assert "LATIN" in result.scripts

    # Но внутри отдельных слов алфавиты не смешаны.
    assert result.mixed_script_tokens == []


def test_letter_digit_obfuscation_is_detected():
    result = analyze_text("FR33 crypto SC4M 3kam")

    assert "FR33" in result.letter_digit_tokens
    assert "SC4M" in result.letter_digit_tokens
    assert "3kam" in result.letter_digit_tokens


def test_chinese_text_does_not_break_analyzer():
    result = analyze_text("免费领取 USDT")

    assert result.raw_text == "免费领取 USDT"
    assert result.normalized_text == "免费领取 USDT"
    assert "HAN" in result.scripts
    assert "LATIN" in result.scripts


def test_arabic_text_does_not_break_analyzer():
    result = analyze_text("احصل على USDT مجانا")

    assert result.raw_text == "احصل على USDT مجانا"
    assert "ARABIC" in result.scripts
    assert "LATIN" in result.scripts


def test_empty_text_is_supported():
    result = analyze_text(None)

    assert result.raw_text == ""
    assert result.normalized_text == ""
    assert result.mixed_script_tokens == []
    assert result.letter_digit_tokens == []