import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_shadow_mode_can_toggle(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'control.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(database.session_factory)

    settings = await repository.ensure_chat_settings(
        chat_id=-100123,
        chat_title="Test chat",
    )
    assert settings.shadow_mode is False

    assert await repository.toggle_shadow(-100123) is True
    assert await repository.toggle_shadow(-100123) is False

    await database.dispose()


@pytest.mark.asyncio
async def test_ambiguous_cases_merge_into_one_open_ticket(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'tickets.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(database.session_factory)

    first = await repository.create_or_update_ticket(
        chat_id=-100123,
        telegram_message_id=1,
        target_user_id=777,
        username="@tester",
        category="threat",
        severity="high",
        confidence=0.65,
        reason="Ambiguous threat.",
        message_text="message one",
        context={},
    )

    second = await repository.create_or_update_ticket(
        chat_id=-100123,
        telegram_message_id=2,
        target_user_id=777,
        username="@tester",
        category="threat",
        severity="high",
        confidence=0.70,
        reason="Another ambiguous threat.",
        message_text="message two",
        context={},
    )

    assert first.id == second.id
    assert second.occurrence_count == 2
    assert await repository.count_open_tickets(-100123) == 1

    await database.dispose()
