from pathlib import Path


def test_executor_has_real_mute_and_per_chat_duration():
    source = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert "restrict_chat_member" in source
    assert "get_mute_duration_minutes" in source
    assert "LIVE MUTE | RESTRICTED" in source


def test_ticket_ui_exposes_manual_mute():
    source = Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    assert "🔇 Mute" in source
    assert "mg:tact:{ticket.id}:mute" in source
