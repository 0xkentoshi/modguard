import pytest

from app.admin.control_repository import ManagedChat
from app.admin.dashboard import DashboardService
import app.admin.dashboard as dashboard_module


class FakeMigration(Exception):
    def __init__(self, new_id: int):
        self.migrate_to_chat_id = new_id


class FakeRepo:
    def __init__(self):
        self.registered = []
        self.titles = []

    async def list_managed_chats(self):
        return [
            ManagedChat(chat_id=-123456789, title="Sandbox 4"),
            ManagedChat(chat_id=-1001234567890, title="Sandbox 4"),
        ]

    async def register_chat_migration(self, *, old_chat_id, new_chat_id, chat_title=None):
        self.registered.append((old_chat_id, new_chat_id, chat_title))
        return new_chat_id

    async def ensure_chat_settings(self, *, chat_id, chat_title=None):
        self.titles.append((chat_id, chat_title))


class FakeChat:
    def __init__(self, title):
        self.title = title


class FakeBot:
    async def get_chat_member_count(self, chat_id):
        if chat_id == -123456789:
            raise FakeMigration(-1001234567890)
        return 3

    async def get_chat(self, chat_id):
        return FakeChat("Sandbox 4")


@pytest.mark.asyncio
async def test_reconcile_uses_passive_member_count_probe_when_getchat_would_not_reveal_migration(monkeypatch):
    monkeypatch.setattr(dashboard_module, "TelegramMigrateToChat", FakeMigration)

    repo = FakeRepo()
    service = DashboardService(
        bot=FakeBot(),
        repository=repo,
        admin_ids={1},
        global_dry_run=False,
        live_delete_enabled=True,
    )

    repaired = await service.reconcile_managed_chats(force=True)

    assert repaired == 1
    assert repo.registered == [
        (-123456789, -1001234567890, "Sandbox 4")
    ]
