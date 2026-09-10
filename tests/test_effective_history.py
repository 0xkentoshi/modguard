import pytest

from app.database.db import Database
from app.database.repository import AuditRepository


@pytest.mark.asyncio
async def test_dry_run_events_do_not_become_user_reputation(
    tmp_path,
):
    database_path = tmp_path / "effective_history.db"

    database = Database(
        "sqlite+aiosqlite:///"
        f"{database_path.as_posix()}"
    )

    await database.init()

    audit = AuditRepository(
        database.session_factory
    )

    await audit.create_event(
        chat_id=-100123,
        telegram_message_id=1,
        target_user_id=777,
        action="ban",
        category="spam",
        severity="high",
        confidence=0.99,
        reason="Dry run test",
        autonomous=True,
        reversible=True,
        metadata={
            "dry_run": True,
        },
    )

    events = await audit.get_effective_user_events(
        chat_id=-100123,
        target_user_id=777,
    )

    assert events == []

    await database.dispose()


@pytest.mark.asyncio
async def test_real_event_can_become_user_reputation(
    tmp_path,
):
    database_path = tmp_path / "real_history.db"

    database = Database(
        "sqlite+aiosqlite:///"
        f"{database_path.as_posix()}"
    )

    await database.init()

    audit = AuditRepository(
        database.session_factory
    )

    await audit.create_event(
        chat_id=-100123,
        telegram_message_id=1,
        target_user_id=777,
        action="mute",
        category="spam",
        severity="high",
        confidence=0.95,
        reason="Confirmed live action",
        autonomous=True,
        reversible=True,
        metadata={
            "dry_run": False,
        },
    )

    events = await audit.get_effective_user_events(
        chat_id=-100123,
        target_user_id=777,
    )

    assert len(events) == 1
    assert events[0].action == "mute"

    await database.dispose()
