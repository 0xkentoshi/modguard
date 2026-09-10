import pytest

from app.database.db import Database
from app.database.repository import AuditRepository


@pytest.mark.asyncio
async def test_audit_event_can_be_created(
    tmp_path,
):
    database_path = (
        tmp_path / "audit.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = AuditRepository(
        database.session_factory
    )

    event = await repository.create_event(
        chat_id=-100123,
        telegram_message_id=50,
        target_user_id=777,
        action="ban",
        category="scam",
        severity="critical",
        confidence=0.98,
        reason="Test scam",
        autonomous=True,
        reversible=True,
        metadata={
            "test": True,
        },
    )

    assert event.event_key.startswith(
        "MG-"
    )

    events = await repository.get_recent_events(
        chat_id=-100123
    )

    assert len(events) == 1
    assert events[0].action == "ban"
    assert events[0].category == "scam"
    assert events[0].autonomous is True

    await database.dispose()


@pytest.mark.asyncio
async def test_audit_event_can_be_reversed(
    tmp_path,
):
    database_path = (
        tmp_path / "audit_reverse.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = AuditRepository(
        database.session_factory
    )

    event = await repository.create_event(
        chat_id=-100123,
        telegram_message_id=51,
        target_user_id=888,
        action="mute",
        category="spam",
        severity="high",
        confidence=0.95,
        reason="Repeated spam",
        autonomous=True,
        reversible=True,
    )

    result = await repository.mark_reversed(
        event.event_key
    )

    assert result is True

    events = await repository.get_recent_events(
        chat_id=-100123
    )

    assert events[0].reversed is True
    assert events[0].reversed_at is not None

    await database.dispose()