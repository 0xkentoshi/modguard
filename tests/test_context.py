from datetime import datetime, timedelta, timezone

import pytest
from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User

from app.agent.context import MessageContextBuilder
from app.database.db import Database
from app.database.repository import MessageRepository


def make_message(
    *,
    message_id: int,
    text: str,
    date: datetime,
) -> Message:
    chat = Chat(
        id=-100123456789,
        type=ChatType.SUPERGROUP,
        title="ModGuard Sandbox",
    )

    user = User(
        id=777,
        is_bot=False,
        first_name="Alex",
        username="alex_test",
        language_code="ru",
    )

    return Message(
        message_id=message_id,
        date=date,
        chat=chat,
        from_user=user,
        text=text,
    )


@pytest.mark.asyncio
async def test_context_contains_previous_messages(
    tmp_path,
):
    database_path = (
        tmp_path / "context.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = MessageRepository(
        database.session_factory
    )

    builder = MessageContextBuilder(
        repository,
        chat_history_limit=20,
        user_history_limit=10,
    )

    now = datetime.now(timezone.utc)

    first = make_message(
        message_id=1,
        text="hello",
        date=now,
    )

    second = make_message(
        message_id=2,
        text="sсam",
        date=now + timedelta(seconds=5),
    )

    await builder.build(first)

    context = await builder.build(second)

    assert (
        context.current_message.raw_text
        == "sсam"
    )

    assert len(
        context.recent_chat_messages
    ) == 1

    assert len(
        context.recent_user_messages
    ) == 1

    assert (
        context.recent_user_messages[0]
        .raw_text
        == "hello"
    )

    assert "sсam" in (
        context.text_signals
        .mixed_script_tokens
    )

    await database.dispose()


@pytest.mark.asyncio
async def test_context_does_not_duplicate_current_message(
    tmp_path,
):
    database_path = (
        tmp_path / "duplicate.db"
    )

    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.init()

    repository = MessageRepository(
        database.session_factory
    )

    builder = MessageContextBuilder(
        repository
    )

    now = datetime.now(timezone.utc)

    message = make_message(
        message_id=100,
        text="hello",
        date=now,
    )

    context = await builder.build(
        message
    )

    assert (
        context.recent_chat_messages
        == []
    )

    assert (
        context.recent_user_messages
        == []
    )

    count = await repository.count_messages()

    assert count == 1

    await database.dispose()