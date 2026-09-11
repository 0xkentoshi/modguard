from types import SimpleNamespace

import pytest

from app.admin.dashboard import DashboardService
from app.bot.admin_handlers import allowed
from app.bot.handlers import process_message


class AccessService:
    def __init__(self, *, authorized=True, allowed_chats=None, operational=True):
        self.authorized = authorized
        self.allowed_chats = list(allowed_chats or [])
        self.operational = operational

    async def is_authorized(self, user_id):
        return self.authorized

    async def is_superadmin(self, user_id):
        return False

    async def list_allowed_chat_ids(self, user_id):
        return list(self.allowed_chats)

    async def can_access_chat(self, user_id, chat_id):
        return chat_id in self.allowed_chats

    async def is_chat_operational(self, chat_id):
        return self.operational


@pytest.mark.asyncio
async def test_dynamic_renter_can_be_authorized_without_static_admin_id():
    settings = SimpleNamespace(admin_id_list=[1])
    assert await allowed(22, settings, AccessService(authorized=True))
    assert not await allowed(22, settings, AccessService(authorized=False))
    assert await allowed(1, settings, AccessService(authorized=False))


@pytest.mark.asyncio
async def test_dashboard_only_lists_renter_communities():
    chats = [
        SimpleNamespace(chat_id=-1001, title="A"),
        SimpleNamespace(chat_id=-1002, title="B"),
    ]

    class Repo:
        async def list_managed_chats(self):
            return chats

    service = DashboardService(
        bot=SimpleNamespace(),
        repository=Repo(),
        admin_ids=[],
        global_dry_run=False,
        live_delete_enabled=True,
        access_service=AccessService(allowed_chats=[-1002]),
    )
    visible = await service._visible_chats(22)
    assert [c.chat_id for c in visible] == [-1002]


@pytest.mark.asyncio
async def test_immunity_bypass_happens_before_ai_context_build():
    class Repo:
        async def ensure_chat_settings(self, **kwargs):
            return None

        async def is_moderation_immune(self, **kwargs):
            return True

    class NeverCalled:
        async def build(self, *args, **kwargs):
            raise AssertionError("AI context must not be built for immune account")

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, title="Chat"),
        from_user=SimpleNamespace(id=55, username="service_bot"),
        message_id=10,
        content_type="text",
        text="https://example.com repeated automated notice",
        caption=None,
    )
    await process_message(
        message=message,
        bot=SimpleNamespace(id=999),
        context_builder=NeverCalled(),
        moderator_agent=SimpleNamespace(),
        policy_gate=SimpleNamespace(),
        community_policy_service=SimpleNamespace(),
        feedback_service=SimpleNamespace(),
        raid_guard_service=SimpleNamespace(),
        semantic_cluster_service=SimpleNamespace(),
        control_repository=Repo(),
        moderation_executor=SimpleNamespace(),
        chat_locks=SimpleNamespace(),
        fake_admin_detector=SimpleNamespace(),
        pilot_access_service=None,
        edited=False,
    )


@pytest.mark.asyncio
async def test_suspended_renter_chat_stops_before_immunity_or_ai():
    class Repo:
        async def ensure_chat_settings(self, **kwargs):
            return None

        async def is_moderation_immune(self, **kwargs):
            raise AssertionError("paused chat must stop before immunity query")

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, title="Chat"),
        from_user=SimpleNamespace(id=55, username="user"),
        message_id=10,
        content_type="text",
        text="hello",
        caption=None,
    )
    await process_message(
        message=message,
        bot=SimpleNamespace(id=999),
        context_builder=SimpleNamespace(),
        moderator_agent=SimpleNamespace(),
        policy_gate=SimpleNamespace(),
        community_policy_service=SimpleNamespace(),
        feedback_service=SimpleNamespace(),
        raid_guard_service=SimpleNamespace(),
        semantic_cluster_service=SimpleNamespace(),
        control_repository=Repo(),
        moderation_executor=SimpleNamespace(),
        chat_locks=SimpleNamespace(),
        fake_admin_detector=SimpleNamespace(),
        pilot_access_service=AccessService(operational=False),
        edited=False,
    )
