import asyncio
from types import SimpleNamespace

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.community_policy.schemas import CommunityPolicyRule
from app.community_policy.service import CommunityPolicyService
from app.database.db import Database


class FakeProvider:
    async def generate_structured(self, **kwargs):
        raise AssertionError("Unexpected LLM call")


def _medium_security_rule() -> CommunityPolicyRule:
    return CommunityPolicyRule(
        rule_id="R1",
        source="shadow_feedback",
        source_ref=64,
        title="First mute, repeat ban",
        condition="Confirmed scam or phishing from the same user.",
        action="mute",
        enforcement_tier="medium",
        policy_family="security_fraud",
    )


def test_repository_can_exclude_same_message_from_shadow_ladder(tmp_path):
    async def run():
        db = Database(
            "sqlite+aiosqlite:///"
            + (tmp_path / "shadow_idempotency.db").as_posix()
        )
        await db.init()
        repo = ControlRepository(db.session_factory)

        inserted = await repo.record_shadow_simulation_event(
            chat_id=-1001,
            telegram_message_id=62,
            target_user_id=111,
            policy_family="security_fraud",
            action="mute",
            category="phishing",
            rule_id="R1",
            policy_version=8,
        )
        assert inserted is True

        # A report re-review of the SAME Telegram message must not see its own
        # previously-recorded strike while selecting the ladder tier.
        assert await repo.get_shadow_simulation_actions(
            chat_id=-1001,
            target_user_id=111,
            policy_family="security_fraud",
            exclude_telegram_message_id=62,
        ) == []

        # The ledger remains idempotent as well: the same unique offense is not
        # inserted twice even if moderation executes again for a report review.
        duplicate = await repo.record_shadow_simulation_event(
            chat_id=-1001,
            telegram_message_id=62,
            target_user_id=111,
            policy_family="security_fraud",
            action="mute",
            category="phishing",
            rule_id="R1",
            policy_version=8,
        )
        assert duplicate is False
        assert await repo.count_shadow_simulation_events(chat_id=-1001) == 1

        # A NEW Telegram message from the same user still sees message 62 as a
        # prior offense and therefore can escalate normally.
        assert await repo.get_shadow_simulation_actions(
            chat_id=-1001,
            target_user_id=111,
            policy_family="security_fraud",
            exclude_telegram_message_id=63,
        ) == ["mute"]

        await db.dispose()

    asyncio.run(run())


def test_medium_ladder_same_message_rereview_stays_mute_new_message_bans(tmp_path):
    async def run():
        db = Database(
            "sqlite+aiosqlite:///"
            + (tmp_path / "shadow_ladder.db").as_posix()
        )
        await db.init()
        repo = ControlRepository(db.session_factory)
        service = CommunityPolicyService(
            repository=repo,
            deep_provider=FakeProvider(),
        )
        rule = _medium_security_rule()
        baseline = SimpleNamespace(category="phishing")

        # _shadow_simulation_actions intentionally reads the virtual ledger
        # only while SHADOW mode is enabled for the community.  The original
        # v1.4.12 regression forgot to enable it, so both lookups correctly
        # returned [] and the test produced a false failure.
        await repo.set_shadow(-1001, True)

        await repo.record_shadow_simulation_event(
            chat_id=-1001,
            telegram_message_id=62,
            target_user_id=111,
            policy_family="security_fraud",
            action="mute",
            category="phishing",
            rule_id="R1",
            policy_version=8,
        )

        same_message_context = SimpleNamespace(
            current_message=SimpleNamespace(
                chat_id=-1001,
                user_id=111,
                telegram_message_id=62,
            ),
            user_moderation_history=[],
        )
        same_message_history = await service._shadow_simulation_actions(
            context=same_message_context,
            rule=rule,
        )
        assert same_message_history == []
        assert service._tier_action(
            rule=rule,
            context=same_message_context,
            baseline_decision=baseline,
            shadow_history_actions=same_message_history,
        ) == "mute"

        new_message_context = SimpleNamespace(
            current_message=SimpleNamespace(
                chat_id=-1001,
                user_id=111,
                telegram_message_id=63,
            ),
            user_moderation_history=[],
        )
        new_message_history = await service._shadow_simulation_actions(
            context=new_message_context,
            rule=rule,
        )
        assert new_message_history == ["mute"]
        assert service._tier_action(
            rule=rule,
            context=new_message_context,
            baseline_decision=baseline,
            shadow_history_actions=new_message_history,
        ) == "ban"

        await db.dispose()

    asyncio.run(run())


def test_shadow_ladder_lookup_is_disabled_when_shadow_mode_is_off(tmp_path):
    async def run():
        db = Database(
            "sqlite+aiosqlite:///"
            + (tmp_path / "shadow_mode_gate.db").as_posix()
        )
        await db.init()
        repo = ControlRepository(db.session_factory)
        service = CommunityPolicyService(
            repository=repo,
            deep_provider=FakeProvider(),
        )
        rule = _medium_security_rule()

        await repo.record_shadow_simulation_event(
            chat_id=-1001,
            telegram_message_id=62,
            target_user_id=111,
            policy_family="security_fraud",
            action="mute",
            category="phishing",
            rule_id="R1",
            policy_version=8,
        )

        context = SimpleNamespace(
            current_message=SimpleNamespace(
                chat_id=-1001,
                user_id=111,
                telegram_message_id=63,
            ),
            user_moderation_history=[],
        )

        # Default chat settings are Shadow OFF.  The ledger must remain
        # invisible to policy tier selection until Shadow is explicitly on.
        assert await service._shadow_simulation_actions(
            context=context,
            rule=rule,
        ) == []

        await db.dispose()

    asyncio.run(run())


def test_v1412_contract_is_message_identity_not_review_count():
    from pathlib import Path

    repository = Path("app/admin/control_repository.py").read_text(
        encoding="utf-8"
    )
    service = Path("app/community_policy/service.py").read_text(
        encoding="utf-8"
    )

    assert "exclude_telegram_message_id" in repository
    assert "exclude_telegram_message_id" in service
    assert "telegram_message_id" in repository
    assert "One unique offense" not in service  # behavior, not brittle prose
