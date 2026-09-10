import json

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.admin.test_mode import is_test_ticket
from app.database.db import Database


@pytest.mark.asyncio
async def test_test_ticket_is_safe_and_unique(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'test-center.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    first = await repository.create_test_ticket(
        chat_id=-100123,
        source="manual",
    )

    second = await repository.create_test_ticket(
        chat_id=-100123,
        source="report",
    )

    assert first.id != second.id
    assert first.ticket_key.startswith("TEST-")
    assert second.ticket_key.startswith("TEST-")

    assert first.telegram_message_id is None
    assert first.target_user_id is None

    assert is_test_ticket(first) is True
    assert is_test_ticket(second) is True

    payload = json.loads(
        first.context_json
    )
    assert payload["test_mode"] is True
    assert payload["simulated"] is True

    assert await repository.count_open_tickets(
        -100123
    ) == 2

    cleared = await repository.clear_test_tickets(
        chat_id=-100123
    )

    assert cleared == 2
    assert await repository.count_open_tickets(
        -100123
    ) == 0

    await database.dispose()


def test_normal_ticket_is_not_test_ticket():
    class Ticket:
        ticket_key = "T-ABC123"
        context_json = '{"test_mode": false}'

    assert is_test_ticket(
        Ticket()
    ) is False
