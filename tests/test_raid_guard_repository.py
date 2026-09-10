import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_raid_guard_defaults_off_and_is_chat_scoped(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'raid.db').as_posix()}"
    )
    await database.init()
    repository = ControlRepository(database.session_factory)

    assert await repository.get_raid_guard_enabled(-1001) is False
    assert await repository.get_raid_guard_enabled(-1002) is False

    assert await repository.toggle_raid_guard(-1001) is True
    assert await repository.get_raid_guard_enabled(-1001) is True
    assert await repository.get_raid_guard_enabled(-1002) is False

    await database.dispose()
