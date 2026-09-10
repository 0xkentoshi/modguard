import pytest
from app.admin import control_models  # noqa
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_settings_only_chat_appears_in_change_chat(tmp_path):
    database=Database("sqlite+aiosqlite:///" + (tmp_path/"chat.db").as_posix())
    await database.init()
    repo=ControlRepository(database.session_factory)
    await repo.ensure_chat_settings(chat_id=-100555, chat_title="Fresh Group")
    chats=await repo.list_managed_chats()
    assert any(c.chat_id == -100555 and c.title == "Fresh Group" for c in chats)
    await database.dispose()


def test_group_handler_registers_my_chat_member_updates():
    from pathlib import Path
    source=Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert "@router.my_chat_member()" in source
    assert "MANAGED CHAT REGISTERED" in source
    assert "ensure_chat_settings" in source
