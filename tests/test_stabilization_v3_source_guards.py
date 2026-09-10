from pathlib import Path


def test_fast_safe_has_independent_challenge():
    source = Path("app/agent/moderator.py").read_text(encoding="utf-8")
    assert "SAFE CHALLENGE" in source
    assert "challenge.route == \"safe\"" in source
    assert "challenge.confidence >= 0.97" in source


def test_report_false_point95_is_not_trusted_anymore():
    source = Path("app/agent/moderator.py").read_text(encoding="utf-8")
    assert "result.confidence < 0.99" in source


def test_cache_key_contains_history_behavior_and_user_scope():
    source = Path("app/agent/moderator.py").read_text(encoding="utf-8")
    assert "history_digest" in source
    assert "messages_last_60s" in source
    assert "repeated_recent_messages" in source
    assert "user:{current.user_id}" in source
    assert "deep:{int(force_deep)}" in source


def test_harassment_default_has_no_autonomous_mute_ban_ladder():
    source = Path("app/moderation/policy.py").read_text(encoding="utf-8")
    light_block = source.split("LIGHT_LADDER_CATEGORIES", 1)[1].split("}", 1)[0]
    assert '"harassment"' not in light_block
    assert "Repeated harassment/fight" in source
    assert 'final_action="warn"' in source


def test_owner_admin_restriction_guard_exists():
    source = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert "_target_can_be_restricted" in source
    assert "LIVE MUTE SKIPPED" in source
    assert "LIVE BAN SKIPPED" in source


def test_ban_ui_has_pagination_and_search():
    dashboard = Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    repo = Path("app/admin/control_repository.py").read_text(encoding="utf-8")
    assert "page_size = 10" in dashboard
    assert "max_pages = 10" in dashboard
    assert "newest first" in dashboard
    assert "Search ban" in dashboard
    assert "search_active_moderation_bans" in repo


def test_shadow_alert_cleanup_is_persistent():
    model = Path("app/admin/control_models.py").read_text(encoding="utf-8")
    executor = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    assert "admin_alert_artifacts" in model
    assert "register_admin_alert_artifact" in executor
    assert 'action == "shadow_clear"' in handlers
