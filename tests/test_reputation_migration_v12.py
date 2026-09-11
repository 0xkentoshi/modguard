import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_light_memory_setting_follows_basic_group_to_supergroup_migration(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "migration.db").as_posix())
    await db.init()
    repo = ControlRepository(db.session_factory)

    old_chat = -12345
    new_chat = -100987654
    await repo.ensure_chat_settings(chat_id=old_chat, chat_title="Old Group")
    await repo.set_light_offense_decay_hours(old_chat, 12)

    canonical = await repo.register_chat_migration(
        old_chat_id=old_chat,
        new_chat_id=new_chat,
        chat_title="New Supergroup",
    )

    assert canonical == new_chat
    assert await repo.get_light_offense_decay_hours(new_chat) == 12
    assert await repo.resolve_chat_id(old_chat) == new_chat

    await db.dispose()
