import pytest

from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_immunity_by_id_and_username_and_removal(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'immunity.db'}")
    await db.init()
    repo = ControlRepository(db.session_factory)

    await repo.ensure_chat_settings(chat_id=-1001, chat_title="Demo")

    id_record = await repo.add_moderation_immunity(
        chat_id=-1001,
        user_id=12345,
        username="service_bot",
        created_by_admin_id=1,
    )
    username_record = await repo.add_moderation_immunity(
        chat_id=-1001,
        username="OtherBot",
        created_by_admin_id=1,
    )

    assert await repo.is_moderation_immune(
        chat_id=-1001, user_id=12345, username="renamed_bot"
    )
    assert await repo.is_moderation_immune(
        chat_id=-1001, user_id=999, username="otherbot"
    )
    assert not await repo.is_moderation_immune(
        chat_id=-1001, user_id=777, username="normal_user"
    )

    records = await repo.list_moderation_immunity(chat_id=-1001)
    assert {r.id for r in records} == {id_record.id, username_record.id}

    assert await repo.remove_moderation_immunity(
        chat_id=-1001, record_id=username_record.id
    )
    assert not await repo.is_moderation_immune(
        chat_id=-1001, user_id=999, username="OtherBot"
    )

    await db.dispose()


@pytest.mark.asyncio
async def test_immunity_survives_chat_migration_without_duplicates(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    await db.init()
    repo = ControlRepository(db.session_factory)

    await repo.ensure_chat_settings(chat_id=-123, chat_title="Old")
    await repo.ensure_chat_settings(chat_id=-100999, chat_title="New")
    await repo.add_moderation_immunity(chat_id=-123, user_id=42)
    await repo.add_moderation_immunity(chat_id=-100999, user_id=42)

    canonical = await repo.register_chat_migration(
        old_chat_id=-123,
        new_chat_id=-100999,
        chat_title="Migrated",
    )
    assert canonical == -100999
    records = await repo.list_moderation_immunity(chat_id=-100999)
    assert len(records) == 1
    assert records[0].user_id == 42

    await db.dispose()
