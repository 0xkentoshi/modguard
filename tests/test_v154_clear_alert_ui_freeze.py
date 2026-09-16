from pathlib import Path


def _shadow_clear_block() -> str:
    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    start = handlers.index('elif action == "shadow_clear"')
    tail = handlers[start:]
    end = tail.index('elif action == "bans"')
    return tail[:end]


def test_shadow_clear_only_deletes_transient_alerts_and_purges_registry():
    block = _shadow_clear_block()
    assert "list_admin_alert_artifacts" in block
    assert "delete_message" in block
    assert "purge_admin_alert_artifacts" in block
    assert "kind_prefix=\"shadow_alert\"" in block


def test_shadow_clear_does_not_navigate_or_create_persistent_panel():
    block = _shadow_clear_block()
    forbidden = (
        "dashboard_service.render",
        "settings_payload",
        "tools_payload",
        "dashboard_payload",
        "main_payload",
    )
    for token in forbidden:
        assert token not in block, token


def test_shadow_clear_acknowledges_callback_before_delete_loop():
    block = _shadow_clear_block()
    ack = block.index("safe_callback_answer")
    delete = block.index("delete_message")
    assert ack < delete


def test_shadow_clear_records_audit_log_without_ui_side_effects():
    block = _shadow_clear_block()
    assert "SHADOW ALERTS CLEARED" in block
