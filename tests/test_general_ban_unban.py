import pytest
from app.admin import control_models  # noqa
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_general_ban_registry_and_unban_state(tmp_path):
    database=Database("sqlite+aiosqlite:///" + (tmp_path/"ban.db").as_posix())
    await database.init()
    repo=ControlRepository(database.session_factory)
    record=await repo.record_moderation_ban(
        chat_id=-1001, user_id=7, username="@u", source="autonomous",
        category="scam", reason="Confirmed scam",
    )
    active=await repo.list_active_moderation_bans(chat_id=-1001)
    assert [x.id for x in active] == [record.id]
    await repo.mark_moderation_unbanned(ban_id=record.id)
    assert await repo.list_active_moderation_bans(chat_id=-1001) == []
    await database.dispose()


def test_settings_exposes_banned_users_page_and_unban_callback():
    from pathlib import Path
    dashboard=Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    handlers=Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    assert "🚫 Banned users" in dashboard
    assert "mg:unban:" in dashboard
    assert 'action == "unban"' in handlers
    assert "unban_chat_member" in handlers


def test_ticket_ban_also_deletes_trigger_message():
    from pathlib import Path

    source = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    ban_branch = source.find('ticket_action\n                == "ban"')
    assert ban_branch >= 0

    next_branch = source.find('ticket_action == "mute"', ban_branch)
    assert next_branch > ban_branch

    fragment = source[ban_branch:next_branch]
    assert "ban_chat_member" in fragment
    assert "delete_message" in fragment
    assert "record_moderation_ban" in fragment


def test_real_ticket_resolution_is_bridged_into_moderation_history():
    from pathlib import Path

    executor = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")

    assert "record_manual_ticket_resolution" in executor
    assert '"human_confirmed": True' in executor
    assert "moderation_executor.record_manual_ticket_resolution" in handlers


def test_manual_ticket_delete_failure_does_not_silently_resolve():
    from pathlib import Path

    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    delete_branch = handlers.find('ticket_action\n                == "delete"')
    assert delete_branch >= 0
    next_branch = handlers.find('ticket_action\n                == "ban"', delete_branch)
    assert next_branch > delete_branch
    fragment = handlers[delete_branch:next_branch]
    assert "Telegram delete failed:" in fragment
    assert "return" in fragment
