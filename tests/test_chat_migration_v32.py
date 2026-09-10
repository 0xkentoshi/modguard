import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_basic_group_migration_collapses_duplicate_chat_and_preserves_settings(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "migration.db").as_posix())
    await db.init()
    repo = ControlRepository(db.session_factory)

    old_id = -123456789
    new_id = -1001234567890

    await repo.ensure_chat_settings(chat_id=old_id, chat_title="Sandbox 4")
    await repo.toggle_shadow(old_id)
    await repo.toggle_live_ban(old_id)
    await repo.set_mute_duration_minutes(old_id, 180)
    await repo.toggle_raid_guard(old_id)

    # Telegram may register the new supergroup with defaults before we learn
    # that it is the same community. Those defaults must not erase old config.
    await repo.ensure_chat_settings(chat_id=new_id, chat_title="Sandbox 4")
    await repo.ensure_enforcement_settings(chat_id=new_id)
    await repo.ensure_mute_settings(chat_id=new_id)
    await repo.ensure_raid_settings(chat_id=new_id)

    await repo.save_dashboard_state(admin_id=42, selected_chat_id=old_id, view="settings")
    ban = await repo.record_moderation_ban(
        chat_id=old_id,
        user_id=777,
        username="tester",
        source="autonomous",
        category="scam",
        reason="test",
    )

    resolved = await repo.register_chat_migration(
        old_chat_id=old_id,
        new_chat_id=new_id,
        chat_title="Sandbox 4",
    )
    assert resolved == new_id
    assert await repo.resolve_chat_id(old_id) == new_id

    chats = await repo.list_managed_chats()
    ids = [c.chat_id for c in chats]
    assert old_id not in ids
    assert ids.count(new_id) == 1

    assert (await repo.get_chat_settings(new_id)).shadow_mode is True
    assert await repo.get_live_ban_enabled(new_id) is True
    assert await repo.get_mute_duration_minutes(new_id) == 180
    assert await repo.get_raid_guard_enabled(new_id) is True

    state = await repo.get_dashboard_state(42)
    assert state.selected_chat_id == new_id

    moved_ban = await repo.get_moderation_ban(ban.id)
    assert moved_ban.chat_id == new_id
    assert moved_ban.active is True

    # Idempotent: duplicate Telegram migration events must not duplicate chats.
    await repo.register_chat_migration(
        old_chat_id=old_id,
        new_chat_id=new_id,
        chat_title="Sandbox 4",
    )
    chats = await repo.list_managed_chats()
    assert [c.chat_id for c in chats].count(new_id) == 1

    await db.dispose()


def test_change_chat_self_heals_stale_telegram_ids_and_diagnostics_exist():
    from pathlib import Path

    dashboard = Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    admin = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")

    assert "TelegramMigrateToChat" in dashboard
    assert "CHAT MIGRATION REPAIRED" in dashboard
    assert "register_chat_migration" in handlers
    assert "migrate_to_chat_id" in handlers
    assert "migrate_from_chat_id" in handlers
    assert "🩺 Diagnostics" in dashboard
    assert 'action == "diag"' in admin
