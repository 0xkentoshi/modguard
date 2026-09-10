import json

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import (
    ControlRepository,
)
from app.database.db import Database


@pytest.mark.asyncio
async def test_real_ticket_resolution_can_be_learned_and_is_chat_scoped(
    tmp_path,
):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'feedback.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    ticket = await repository.create_or_update_ticket(
        chat_id=-100111,
        telegram_message_id=10,
        target_user_id=20,
        username="@u",
        category="other",
        severity="medium",
        confidence=0.70,
        reason="Ambiguous promotion.",
        message_text="Есть тема с профитом, детали в личке",
        context={
            "current_message_evidence": [
                "Vague profit solicitation"
            ]
        },
    )

    record = await repository.save_moderator_feedback(
        ticket_id=ticket.id,
        moderator_action="delete",
        moderator_admin_id=999,
    )

    assert record is not None
    assert record.chat_id == -100111
    assert record.moderator_action == "delete"
    assert record.message_text

    same_chat = await repository.recent_moderator_feedback(
        chat_id=-100111
    )
    other_chat = await repository.recent_moderator_feedback(
        chat_id=-100222
    )

    assert len(same_chat) == 1
    assert other_chat == []

    await database.dispose()


@pytest.mark.asyncio
async def test_test_ticket_never_enters_feedback_memory(
    tmp_path,
):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'feedback-test.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    ticket = await repository.create_test_ticket(
        chat_id=-100123,
        source="report",
    )

    record = await repository.save_moderator_feedback(
        ticket_id=ticket.id,
        moderator_action="delete",
        moderator_admin_id=999,
    )

    assert record is None

    assert await repository.count_moderator_feedback(
        chat_id=-100123
    ) == 0

    await database.dispose()


@pytest.mark.asyncio
async def test_feedback_learning_is_idempotent_per_ticket(
    tmp_path,
):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'feedback-idempotent.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    ticket = await repository.create_or_update_ticket(
        chat_id=-100111,
        telegram_message_id=11,
        target_user_id=21,
        username="@u",
        category="other",
        severity="medium",
        confidence=0.70,
        reason="Ambiguous.",
        message_text="Maybe promotional",
        context={},
    )

    first = await repository.save_moderator_feedback(
        ticket_id=ticket.id,
        moderator_action="allow",
        moderator_admin_id=999,
    )

    second = await repository.save_moderator_feedback(
        ticket_id=ticket.id,
        moderator_action="allow",
        moderator_admin_id=999,
    )

    assert first is not None
    assert second is not None
    assert first.id == second.id

    assert await repository.count_moderator_feedback(
        chat_id=-100111
    ) == 1

    await database.dispose()
