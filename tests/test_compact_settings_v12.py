from types import SimpleNamespace

import pytest

from app.admin.dashboard import DashboardService


class Repo:
    async def get_chat_settings(self, chat_id):
        return SimpleNamespace(shadow_mode=False)

    async def get_live_ban_enabled(self, chat_id):
        return True

    async def get_mute_duration_minutes(self, chat_id):
        return 60

    async def get_light_offense_decay_hours(self, chat_id):
        return 6

    async def get_raid_guard_enabled(self, chat_id):
        return True

    async def list_managed_chats(self):
        return [SimpleNamespace(chat_id=-1001, title="Pilot Crypto")]

    async def list_moderation_immunity(self, *, chat_id):
        return [SimpleNamespace(id=1)]

    async def latest_raid_incident(self, *, chat_id):
        return None


@pytest.mark.asyncio
async def test_settings_are_compact_and_tools_are_separated():
    service = DashboardService(
        bot=SimpleNamespace(),
        repository=Repo(),
        admin_ids=[1],
        global_dry_run=False,
        live_delete_enabled=True,
        raid_guard_available=True,
    )

    text, keyboard = await service.settings_payload(chat_id=-1001)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Light memory   <b>6 hours</b>" in text
    assert any("Safety & tools" in label for label in labels)
    assert any("Memory: 6 hours" in label for label in labels)
    assert not any("Diagnostics" in label for label in labels)
    assert not any("Banned users" in label for label in labels)
    assert not any("Immunity" == label for label in labels)

    tools_text, tools_keyboard = await service.tools_payload(chat_id=-1001)
    tool_labels = [button.text for row in tools_keyboard.inline_keyboard for button in row]

    assert "Auto-stop: <b>ARMED</b>" in tools_text
    assert "🩺 Diagnostics" in tool_labels
    assert "🚫 Banned users" in tool_labels
    assert "🧿 Immunity" in tool_labels


@pytest.mark.asyncio
async def test_light_memory_menu_exposes_sane_presets():
    service = DashboardService(
        bot=SimpleNamespace(),
        repository=Repo(),
        admin_ids=[1],
        global_dry_run=False,
        live_delete_enabled=True,
    )
    text, keyboard = await service.light_memory_payload(chat_id=-1001)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Recommended default: <b>6 hours</b>" in text
    assert "6 hours ✓" in labels
    assert "Never expire" in labels
    assert "◀ Settings" in labels
