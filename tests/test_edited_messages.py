from datetime import datetime, timezone

import pytest

from app.database.db import Database
from app.database.repository import MessageRepository


@pytest.mark.asyncio
async def test_edit_can_use_unix_timestamp(
    tmp_path,
):
    database_path = (
        tmp_path
        / "edited_message.db"
    )

    database = Database(
        "sqlite+aiosqlite:///"
        f"{database_path.as_posix()}"
    )

    await database.init()

    repository = MessageRepository(
        database.session_factory
    )

    original_date = datetime(
        2026,
        9,
        7,
        15,
        59,
        23,
        tzinfo=timezone.utc,
    )

    await repository.upsert_message(
        telegram_message_id=11,
        chat_id=-1004326214219,
        user_id=653133067,
        username="@tester",
        full_name="Tester",
        chat_title="ModGuard Sandbox",
        content_type="text",
        raw_text="Привет всем",
        normalized_text="Привет всем",
        is_edited=False,
        is_forwarded=False,
        reply_to_message_id=None,
        telegram_date=original_date,
    )

    # Именно такой тип пришёл у тебя
    # при Telegram edited_message.
    edit_timestamp = 1788796770

    await repository.upsert_message(
        telegram_message_id=11,
        chat_id=-1004326214219,
        user_id=653133067,
        username="@tester",
        full_name="Tester",
        chat_title="ModGuard Sandbox",
        content_type="text",
        raw_text=(
            "FREE USDT "
            "https://example.com"
        ),
        normalized_text=(
            "FREE USDT "
            "https://example.com"
        ),
        is_edited=True,
        is_forwarded=False,
        reply_to_message_id=None,
        telegram_date=edit_timestamp,
    )

    record = await repository.get_message(
        chat_id=-1004326214219,
        telegram_message_id=11,
    )

    assert record is not None

    assert (
        record.raw_text
        == "FREE USDT https://example.com"
    )

    assert record.is_edited is True

    # Главное: edit НЕ создал новую строку.
    assert (
        await repository.count_messages()
        == 1
    )

    await database.dispose()