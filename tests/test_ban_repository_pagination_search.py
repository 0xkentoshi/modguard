import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_bans_are_newest_first_paginated_and_searchable(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "bans.db").as_posix())
    await db.init()
    repo = ControlRepository(db.session_factory)
    for i in range(12):
        await repo.record_moderation_ban(
            chat_id=-1001,
            user_id=1000 + i,
            username=f"user{i}",
            source="autonomous",
            category="scam",
            reason="test",
        )
    assert await repo.count_active_moderation_bans(chat_id=-1001) == 12
    first = await repo.list_active_moderation_bans(chat_id=-1001, limit=10, offset=0)
    second = await repo.list_active_moderation_bans(chat_id=-1001, limit=10, offset=10)
    assert [x.user_id for x in first][:2] == [1011, 1010]
    assert len(first) == 10
    assert len(second) == 2
    found = await repo.search_active_moderation_bans(chat_id=-1001, query="@user11")
    assert len(found) == 1 and found[0].user_id == 1011
    found_id = await repo.search_active_moderation_bans(chat_id=-1001, query="1003")
    assert any(x.user_id == 1003 for x in found_id)
    await db.dispose()
