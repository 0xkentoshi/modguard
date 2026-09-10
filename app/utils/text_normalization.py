import re
import unicodedata

from confusable_homoglyphs import confusables
from pydantic import BaseModel, Field


# Символы, которые часто используются для визуального разрыва слова.
# Мы НЕ удаляем вообще все Unicode Format Characters,
# потому что некоторые языки действительно их используют.
INVISIBLE_CHARS = {
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u180e",  # MONGOLIAN VOWEL SEPARATOR
}


class TextSignals(BaseModel):
    raw_text: str
    normalized_text: str

    contains_invisible_chars: bool = False
    invisible_char_count: int = 0

    mixed_script_tokens: list[str] = Field(default_factory=list)
    letter_digit_tokens: list[str] = Field(default_factory=list)

    scripts: list[str] = Field(default_factory=list)


def remove_suspicious_invisible_chars(text: str) -> tuple[str, int]:
    result: list[str] = []
    removed = 0

    for char in text:
        if char in INVISIBLE_CHARS:
            removed += 1
            continue

        result.append(char)

    return "".join(result), removed


def normalize_text(text: str) -> str:
    """
    Делает безопасное представление текста для дополнительного анализа.

    Важно:
    - исходный текст НЕ теряется;
    - мы не пытаемся сами решить, является сообщение scam/spam;
    - результат используется только как дополнительный input для агента.
    """

    # Например:
    # ＦＲＥＥ -> FREE
    normalized = unicodedata.normalize("NFKC", text)

    normalized, _ = remove_suspicious_invisible_chars(normalized)

    # Убираем лишние пробелы, не ломая слова.
    normalized = re.sub(r"[ \t]+", " ", normalized)

    return normalized.strip()


def extract_tokens(text: str) -> list[str]:
    """
    Получаем буквенно-цифровые токены Unicode.

    Работает не только с English/Russian.
    """

    return re.findall(r"\w+", text, flags=re.UNICODE)


def find_mixed_script_tokens(tokens: list[str]) -> list[str]:
    """
    Ищем слова, внутри которых смешаны разные Unicode scripts.

    Например:
        scam   -> нет
        скам   -> нет
        sсam   -> да
    """

    result: list[str] = []

    for token in tokens:
        if len(token) < 2:
            continue

        try:
            if confusables.is_mixed_script(token):
                result.append(token)
        except Exception:
            # Никогда не ломаем moderation pipeline из-за
            # странного Unicode-ввода.
            continue

    return result


def contains_adjacent_letter_and_digit(token: str) -> bool:
    """
    Общий сигнал leetspeak / obfuscation.

    Например:
        scam   -> False
        sc4m   -> True
        3kam   -> True

    Это НЕ означает, что слово вредоносное.
    """

    if not any(char.isalpha() for char in token):
        return False

    if not any(char.isdigit() for char in token):
        return False

    for left, right in zip(token, token[1:]):
        if left.isalpha() and right.isdigit():
            return True

        if left.isdigit() and right.isalpha():
            return True

    return False


def find_letter_digit_tokens(tokens: list[str]) -> list[str]:
    return [
        token
        for token in tokens
        if contains_adjacent_letter_and_digit(token)
    ]


def detect_scripts(text: str) -> list[str]:
    """
    Небольшая metadata-функция.

    Она НЕ определяет язык.
    Она только показывает Unicode scripts,
    встречающиеся в сообщении.
    """

    scripts: set[str] = set()

    script_markers = {
        "LATIN": "LATIN",
        "CYRILLIC": "CYRILLIC",
        "GREEK": "GREEK",
        "ARABIC": "ARABIC",
        "HEBREW": "HEBREW",
        "HIRAGANA": "HIRAGANA",
        "KATAKANA": "KATAKANA",
        "HANGUL": "HANGUL",
        "DEVANAGARI": "DEVANAGARI",
        "THAI": "THAI",
        "ARMENIAN": "ARMENIAN",
        "GEORGIAN": "GEORGIAN",
    }

    for char in text:
        if not char.isalpha():
            continue

        name = unicodedata.name(char, "")

        if (
            "CJK UNIFIED IDEOGRAPH" in name
            or "CJK COMPATIBILITY IDEOGRAPH" in name
        ):
            scripts.add("HAN")
            continue

        for marker, script_name in script_markers.items():
            if marker in name:
                scripts.add(script_name)
                break

    return sorted(scripts)


def analyze_text(text: str | None) -> TextSignals:
    raw_text = text or ""

    _, invisible_count = remove_suspicious_invisible_chars(raw_text)

    normalized = normalize_text(raw_text)

    tokens = extract_tokens(normalized)

    return TextSignals(
        raw_text=raw_text,
        normalized_text=normalized,
        contains_invisible_chars=invisible_count > 0,
        invisible_char_count=invisible_count,
        mixed_script_tokens=find_mixed_script_tokens(tokens),
        letter_digit_tokens=find_letter_digit_tokens(tokens),
        scripts=detect_scripts(raw_text),
    )