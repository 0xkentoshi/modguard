from types import SimpleNamespace

import pytest

from app.admin import test_mode
from app.admin.test_mode import run_delete_probe


class FakeRepository:
    def __init__(self, shadow_mode):
        self.shadow_mode = shadow_mode

    async def get_chat_settings(self, chat_id):
        return SimpleNamespace(shadow_mode=self.shadow_mode)


class FakeBot:
    def __init__(self):
        self.deleted = []
        self.sent = []

    async def send_message(self, *, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=123)

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeDashboard:
    def __init__(self, shadow_mode):
        self.repository = FakeRepository(shadow_mode)
        self.bot = FakeBot()
        self.admin_ids = {999}

    async def _chat_title(self, chat_id):
        return "Sandbox"


def test_test_mode_module_is_not_pytest_suite():
    assert test_mode.__test__ is False
    assert not hasattr(test_mode, "test_mode_payload")
    assert not hasattr(test_mode, "test_ticket_result_payload")


@pytest.mark.asyncio
async def test_delete_probe_respects_shadow_and_does_not_delete():
    dashboard = FakeDashboard(shadow_mode=True)

    ok, status = await run_delete_probe(
        dashboard_service=dashboard,
        chat_id=-100123,
    )

    assert ok is True
    assert "WOULD DELETE" in status
    assert dashboard.bot.deleted == []
    assert any(
        "SHADOW · WOULD DELETE" in text
        for _, text in dashboard.bot.sent
    )
