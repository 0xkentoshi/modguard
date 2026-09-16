import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.admin.dashboard import DashboardService
from app.community_policy.schemas import CommunityPolicyRule
from app.community_policy.service import CommunityPolicyService


class NoopRepo:
    pass


class CapabilityBot:
    def __init__(self, *, chat_type: str, can_restrict: bool = True, status: str = "administrator"):
        self.chat_type = chat_type
        self.can_restrict = can_restrict
        self.status = status

    async def get_chat(self, chat_id):
        return SimpleNamespace(type=self.chat_type)

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.status, can_restrict_members=self.can_restrict)


def _dashboard(bot):
    return DashboardService(
        bot=bot,
        repository=NoopRepo(),
        admin_ids=[1],
        global_dry_run=False,
        live_delete_enabled=True,
    )


def test_security_policy_family_never_claims_harassment():
    async def run():
        service = CommunityPolicyService(repository=NoopRepo(), deep_provider=SimpleNamespace())
        rules = [
            CommunityPolicyRule(
                rule_id="R1",
                title="Security progressive",
                condition="Confirmed scam/phishing",
                action="mute",
                enforcement_tier="medium",
                policy_family="security_fraud",
            ),
            CommunityPolicyRule(
                rule_id="R2",
                title="Harassment",
                condition="Targeted abuse",
                action="warn",
                enforcement_tier="light",
                policy_family="harassment",
            ),
        ]
        context = SimpleNamespace(current_message=SimpleNamespace(chat_id=-1001))
        baseline = SimpleNamespace(category="harassment")
        filtered = await service._family_compatible_rules(
            context=context,
            rules=rules,
            baseline_decision=baseline,
        )
        assert [rule.rule_id for rule in filtered] == ["R2"]

    asyncio.run(run())


def test_basic_group_is_not_live_punitive_ready():
    async def run():
        ready, reason = await _dashboard(CapabilityBot(chat_type="group")).live_punitive_capability(chat_id=-123)
        assert ready is False
        assert "supergroup" in reason.lower()

    asyncio.run(run())


def test_supergroup_with_restrict_permission_is_live_ready():
    async def run():
        ready, reason = await _dashboard(CapabilityBot(chat_type="supergroup")).live_punitive_capability(chat_id=-100123)
        assert ready is True
        assert "ready" in reason.lower()

    asyncio.run(run())


def test_public_build_does_not_ship_private_control_plane():
    assert not Path("private_ops").exists()
    main = Path("app/main.py").read_text(encoding="utf-8")
    config = Path("app/config.py").read_text(encoding="utf-8")
    assert "from private_ops" not in main
    assert "SUPERADMIN_IDS" not in config
    assert "private_ops_db" not in config


def test_release_safety_contracts_present():
    policy = Path("app/community_policy/service.py").read_text(encoding="utf-8")
    dashboard = Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    executor = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    assert "COMMUNITY POLICY FAMILY GATE" in policy
    assert "live_punitive_capability" in dashboard
    assert "LIVE_RESTRICT_UNSUPPORTED" in executor
    assert "Needs review" in dashboard
    clear = handlers[handlers.index('elif action == "shadow_clear"'):]
    clear = clear[:clear.index('elif action == "bans"')]
    assert "purge_admin_alert_artifacts" in clear
    assert "delete_message" in clear
    assert "dashboard_service.render" not in clear


def test_static_admin_id_cannot_be_revoked_by_optional_access_provider():
    from app.bot.admin_handlers import allowed

    class DenyProvider:
        async def is_authorized(self, user_id):
            return False

    async def run():
        settings = SimpleNamespace(admin_id_list=[1])
        assert await allowed(1, settings, DenyProvider()) is True
        assert await allowed(22, settings, DenyProvider()) is False

    asyncio.run(run())
