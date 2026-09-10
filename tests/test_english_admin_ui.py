from pathlib import Path
import re


def test_user_facing_admin_ui_is_english_only():
    files = [
        "app/admin/dashboard.py",
        "app/admin/test_mode.py",
        "app/bot/admin_handlers.py",
        "app/bot/handlers.py",
        "app/moderation/executor.py",
        "app/community_policy/core.py",
    ]

    for filename in files:
        text = Path(filename).read_text(encoding="utf-8")
        assert re.search(r"[А-Яа-яЁё]", text) is None, filename
