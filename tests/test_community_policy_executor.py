from datetime import datetime, timezone

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    PolicyEvaluation,
)
from app.database.db import Database
from app.database.repository import AuditRepository
from app.moderation.executor import ModerationExecutor
from app.utils.text_normalization import analyze_text


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeNotifier:
    async def notify_decision(self, **kwargs):
        return True


@pytest.mark.asyncio
async def test_custom_policy_delete_can_enforce_other_category(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'community-executor.db').as_posix()}"
    )
    await database.init()

    audit = AuditRepository(database.session_factory)
    control = ControlRepository(database.session_factory)
    bot = FakeBot()

    text = "custom-rule message"
    context = MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=50,
            chat_id=-100123,
            user_id=777,
            username="@tester",
            full_name="Tester",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=datetime.now(timezone.utc),
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
    )

    decision = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="other",
        severity="medium",
        confidence=0.95,
        action="delete",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Matched explicit community rule.",
        current_message_evidence=["Current message matches custom rule"],
        context_evidence=[],
    )

    policy = PolicyEvaluation(
        original_action="delete",
        final_action="delete",
        final_delete_message=True,
        autonomous=True,
        requires_human_review=False,
        policy_reason="Community policy v1 · R1",
        source="community_policy",
        community_policy_version=1,
        matched_community_rules=["R1"],
    )

    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=control,
    )

    result = await executor.execute(
        context=context,
        decision=decision,
        policy=policy,
    )

    assert result.executed is True
    assert bot.deleted == [(-100123, 50)]

    # Custom policy enforcement must not poison the stable core exact-pattern
    # memory. Removing the custom rule later must really remove its effect.
    assert await control.match_confirmed_pattern(
        chat_id=-100123,
        normalized_text=text,
    ) is None

    await database.dispose()
