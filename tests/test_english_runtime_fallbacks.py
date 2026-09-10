from pathlib import Path
import re


def test_runtime_fallback_reasons_are_english():
    text = Path("app/agent/moderator.py").read_text(encoding="utf-8")

    forbidden = [
        "Не удалось получить",
        "Внутренняя ошибка",
        "Быстрый AI-фильтр",
        "Сообщение является жалобой",
        "После жалобы",
        "Жалоба участника",
    ]

    for phrase in forbidden:
        assert phrase not in text

    assert "Could not obtain a reliable moderation decision" in text
    assert "Internal AI moderator error" in text
    assert (
        "Fast triage and independent safe challenge both found no violation."
        in text
    )


def test_synthetic_test_ticket_copy_is_english():
    text = Path("app/admin/control_repository.py").read_text(encoding="utf-8")

    assert "There is a closed topic with good profit" in text
    assert "Synthetic ambiguous case" in text
    assert re.search(r"[А-Яа-яЁё]", text) is None
