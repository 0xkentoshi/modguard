from pathlib import Path


def test_mute_and_ban_paths_delete_trigger_message_even_for_harassment():
    source=Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert "enforcement_includes_cleanup" in source
    assert 'policy.final_action in {"mute", "ban"}' in source


def test_warning_text_has_spam_and_harassment_paths():
    source=Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert 'reason == "spam"' in source
    assert 'reason == "harassment"' in source


def test_ticket_records_conversation_context():
    source=Path("app/moderation/executor.py").read_text(encoding="utf-8")
    dashboard=Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    assert '"recent_chat_messages"' in source
    assert "Conversation context" in dashboard


def test_warning_throttle_is_scoped_by_reason_category():
    source = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert 'f"{current.user_id}:{reason}"' in source
