from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetChat

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository, ManagedChat
from app.admin.dashboard import DashboardService
from app.database.db import Database


@pytest.mark.asyncio
async def test_tombstoned_chat_disappears_and_can_be_revived(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "stale.db").as_posix())
    await db.init()
    repo = ControlRepository(db.session_factory)

    chat_id = -100555001
    await repo.ensure_chat_settings(chat_id=chat_id, chat_title="Deleted Group")
    await repo.toggle_shadow(chat_id)
    assert any(c.chat_id == chat_id for c in await repo.list_managed_chats())

    await repo.mark_chat_unavailable(chat_id=chat_id, reason="bot was kicked")
    assert all(c.chat_id != chat_id for c in await repo.list_managed_chats())

    await repo.mark_chat_available(chat_id=chat_id)
    await repo.ensure_chat_settings(chat_id=chat_id, chat_title="Deleted Group")
    assert (await repo.get_chat_settings(chat_id)).shadow_mode is True
    assert any(c.chat_id == chat_id for c in await repo.list_managed_chats())

    await db.dispose()


class FakeRepo:
    def __init__(self):
        self.hidden = []

    async def list_managed_chats(self):
        return [ManagedChat(chat_id=-100777001, title="Gone Group")]

    async def mark_chat_unavailable(self, *, chat_id, reason):
        self.hidden.append((chat_id, reason))

    async def mark_chat_available(self, *, chat_id):
        raise AssertionError("An inaccessible chat must not be revived")

    async def ensure_chat_settings(self, *, chat_id, chat_title=None):
        raise AssertionError("An inaccessible chat must not be refreshed")


class ForbiddenBot:
    id = 999

    async def get_chat(self, chat_id):
        raise TelegramForbiddenError(
            method=GetChat(chat_id=chat_id),
            message="Forbidden: bot was kicked from the supergroup chat",
        )


@pytest.mark.asyncio
async def test_force_reconcile_hides_inaccessible_supergroup():
    repo = FakeRepo()
    service = DashboardService(
        bot=ForbiddenBot(),
        repository=repo,
        admin_ids={1},
        global_dry_run=False,
        live_delete_enabled=True,
    )

    repaired = await service.reconcile_managed_chats(force=True)

    assert repaired == 0
    assert len(repo.hidden) == 1
    assert repo.hidden[0][0] == -100777001
    assert "kicked" in repo.hidden[0][1].lower()
