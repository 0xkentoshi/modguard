from datetime import datetime, timedelta, timezone

import pytest

from app.database.db import Database
from app.database.repository import MessageRepository


@pytest.mark.asyncio
async def test_message_upsert_updates_existing_message(
    tmp_path,
):
    database_path = (
        tmp_path / "messages.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = MessageRepository(
        database.session_factory
    )

    now = datetime.now(timezone.utc)

    await repository.upsert_message(
        telegram_message_id=10,
        chat_id=-100123,
        user_id=100,
        username="@alex",
        full_name="Alex",
        chat_title="Sandbox",
        content_type="text",
        raw_text="Hello",
        normalized_text="Hello",
        is_edited=False,
        is_forwarded=False,
        reply_to_message_id=None,
        telegram_date=now,
    )

    await repository.upsert_message(
        telegram_message_id=10,
        chat_id=-100123,
        user_id=100,
        username="@alex",
        full_name="Alex",
        chat_title="Sandbox",
        content_type="text",
        raw_text="Hello edited",
        normalized_text="Hello edited",
        is_edited=True,
        is_forwarded=False,
        reply_to_message_id=None,
        telegram_date=(
            now + timedelta(seconds=5)
        ),
    )

    count = await repository.count_messages()

    assert count == 1

    record = await repository.get_message(
        -100123,
        10,
    )

    assert record is not None
    assert record.raw_text == "Hello edited"
    assert record.is_edited is True

    await database.dispose()


@pytest.mark.asyncio
async def test_recent_user_history_is_chronological(
    tmp_path,
):
    database_path = (
        tmp_path / "history.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = MessageRepository(
        database.session_factory
    )

    now = datetime.now(timezone.utc)

    for index in range(3):
        await repository.upsert_message(
            telegram_message_id=index + 1,
            chat_id=-100123,
            user_id=500,
            username="@tester",
            full_name="Tester",
            chat_title="Sandbox",
            content_type="text",
            raw_text=f"message {index + 1}",
            normalized_text=f"message {index + 1}",
            is_edited=False,
            is_forwarded=False,
            reply_to_message_id=None,
            telegram_date=(
                now
                + timedelta(seconds=index)
            ),
        )

    history = (
        await repository
        .get_recent_user_messages(
            chat_id=-100123,
            user_id=500,
            limit=10,
        )
    )

    assert [
        item.raw_text
        for item in history
    ] == [
        "message 1",
        "message 2",
        "message 3",
    ]

    await database.dispose()