from types import SimpleNamespace
import pytest
from app.admin.test_mode import (
    build_test_mode_payload, enforcement_tiers_simulation_status,
    raid_result_payload, spam_ladder_simulation_status,
)

class FakeRepository:
    async def get_chat_settings(self, chat_id): return SimpleNamespace(shadow_mode=False)
    async def get_live_ban_enabled(self, chat_id): return False
    async def get_mute_duration_minutes(self, chat_id): return 60
    async def get_raid_guard_enabled(self, chat_id): return True

class FakeDashboard:
    repository=FakeRepository(); global_dry_run=False; live_delete_enabled=True
    async def _chat_title(self, chat_id): return "Test Chat"

@pytest.mark.asyncio
async def test_test_mode_exposes_critical_simulations():
    text, keyboard=await build_test_mode_payload(dashboard_service=FakeDashboard(), chat_id=-100123)
    labels=[b.text for row in keyboard.inline_keyboard for b in row]
    assert "🔇 Test Mute" in labels
    assert "🔁 Test Spam Ladder" in labels
    assert "⚖️ Test Policy Tiers" in labels
    assert "🚨 Test Raid" in labels

@pytest.mark.asyncio
async def test_spam_ladder_matches_new_light_policy():
    status=await spam_ladder_simulation_status(dashboard_service=FakeDashboard(), chat_id=-100123)
    assert "1st clear spam → <b>WARN</b>" in status
    assert "MUTE 1 hour + DELETE" in status
    assert "Auto-ban OFF" in status

@pytest.mark.asyncio
async def test_policy_tiers_simulation_is_safe_and_complete():
    status=await enforcement_tiers_simulation_status(dashboard_service=FakeDashboard(), chat_id=-100123)
    assert "LIGHT" in status and "MEDIUM" in status and "HEAVY" in status
    assert "WARN" in status and "MUTE" in status and "BAN" in status

@pytest.mark.asyncio
async def test_raid_simulation_still_requires_no_extra_accounts():
    text,_=await raid_result_payload(dashboard_service=FakeDashboard(), chat_id=-100123)
    assert "SC-TEST" in text and "Extra accounts are not required" in text
