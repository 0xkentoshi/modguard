from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    PolicyEvaluation,
)
from app.raid_guard.service import RaidGuardService
from app.semantic_clustering.service import SemanticPreparedSignal
from app.utils.text_normalization import analyze_text


class FakeSemantic:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def prepare_signal(self, *, context):
        self.calls += 1
        return SemanticPreparedSignal(
            vector=[1.0, 0.0],
            nearest_record=SimpleNamespace(id=1),
            similarity=.97,
            cluster_key="SC-000001",
        )


class FakeRepo:
    def __init__(self, *, shadow=True, auto_ban=True):
        self.shadow = shadow
        self.auto_ban = auto_ban
        self.incident = None

    async def get_raid_guard_enabled(self, chat_id):
        return True

    async def semantic_cluster_records(self, *, chat_id, cluster_key, since, limit):
        return [
            SimpleNamespace(
                current_violation=True,
                decision_category="scam",
                confidence=.98,
                final_action="delete",
                user_id=101,
                telegram_message_id=11,
            ),
            SimpleNamespace(
                current_violation=True,
                decision_category="phishing",
                confidence=.97,
                final_action="delete",
                user_id=102,
                telegram_message_id=12,
            ),
            SimpleNamespace(
                current_violation=True,
                decision_category="spam",
                confidence=.96,
                final_action="delete",
                user_id=103,
                telegram_message_id=13,
            ),
        ]

    async def get_chat_settings(self, chat_id):
        return SimpleNamespace(shadow_mode=self.shadow)

    async def get_live_ban_enabled(self, chat_id):
        return self.auto_ban

    async def get_open_raid_incident_for_cluster(self, *, chat_id, cluster_key):
        return None

    async def create_or_update_raid_incident(self, **kwargs):
        self.incident = SimpleNamespace(
            id=7,
            incident_key="RAID-TEST",
            chat_id=kwargs["chat_id"],
            cluster_key=kwargs["cluster_key"],
            status="shadow" if kwargs["shadow_mode"] else "active",
            shadow_mode=kwargs["shadow_mode"],
            auto_ban_enabled=kwargs["auto_ban_enabled"],
            similarity=kwargs["similarity"],
            message_count=kwargs["message_count"],
            unique_users=kwargs["unique_users"],
            deleted_messages=kwargs["deleted_messages"],
            banned_users=kwargs["banned_users"],
            affected_user_ids_json="[101,102,103]",
            affected_message_ids_json="[11,12,13]",
            unbanned_user_ids_json="[]",
        )
        return self.incident


class FakeBot:
    def __init__(self):
        self.deleted = []
        self.banned = []

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def ban_chat_member(self, *, chat_id, user_id):
        self.banned.append((chat_id, user_id))


def context(text="variant campaign message"):
    snap = MessageSnapshot(
        db_id=1,
        telegram_message_id=20,
        chat_id=-100123,
        user_id=104,
        username="@u",
        full_name="U",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=snap,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],
        recent_user_messages=[],
        user_moderation_history=[],
    )


def hard_decision():
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="scam",
        severity="high",
        confidence=.98,
        action="delete",
        delete_message=True,
        needs_human_review=False,
        reason="Confirmed scam variant.",
        current_message_evidence=["Fraud solicitation"],
        context_evidence=[],
    )


def delete_policy():
    return PolicyEvaluation(
        original_action="delete",
        final_action="delete",
        final_delete_message=True,
        autonomous=True,
        requires_human_review=False,
        policy_reason="Hard-risk delete.",
    )


@pytest.mark.asyncio
async def test_shadow_raid_triggers_without_real_actions():
    repo = FakeRepo(shadow=True, auto_ban=True)
    semantic = FakeSemantic()
    bot = FakeBot()
    service = RaidGuardService(
        repository=repo,
        semantic_service=semantic,
        bot=bot,
        admin_ids=[],
    )

    result = await service.inspect_before_execution(
        context=context(),
        decision=hard_decision(),
        policy=delete_policy(),
    )

    assert result.triggered is True
    assert result.shadow is True
    assert result.message_count == 4
    assert result.unique_users == 4
    assert bot.deleted == []
    assert bot.banned == []


@pytest.mark.asyncio
async def test_confident_safe_current_message_is_never_overridden_by_raid():
    repo = FakeRepo(shadow=False, auto_ban=True)
    semantic = FakeSemantic()
    bot = FakeBot()
    service = RaidGuardService(
        repository=repo,
        semantic_service=semantic,
        bot=bot,
        admin_ids=[],
    )

    safe = ModerationDecision(
        detected_language="English",
        current_message_violation=False,
        category="safe",
        severity="none",
        confidence=.99,
        action="allow",
        delete_message=False,
        needs_human_review=False,
        reason="Safe message.",
        current_message_evidence=[],
        context_evidence=[],
    )
    allow = PolicyEvaluation(
        original_action="allow",
        final_action="allow",
        autonomous=True,
        requires_human_review=False,
        policy_reason="Safe.",
    )

    result = await service.inspect_before_execution(
        context=context("normal conversation"),
        decision=safe,
        policy=allow,
    )

    assert result.triggered is False
    assert semantic.calls == 0
    assert bot.deleted == []
    assert bot.banned == []
