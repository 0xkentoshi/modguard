from datetime import datetime, timezone

import pytest

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
)
from app.database.db import Database
from app.database.repository import AuditRepository
from app.moderation.executor import ModerationExecutor
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeNotifier:
    def __init__(self):
        self.calls = 0

    async def notify_decision(
        self,
        **kwargs,
    ):
        self.calls += 1
        return True


def make_context(
    message_id: int,
) -> MessageContext:
    now = datetime.now(
        timezone.utc
    )

    text = (
        "JOIN https://example.com "
        "FREE MONEY"
    )

    return MessageContext(
        current_message=MessageSnapshot(
            db_id=message_id,
            telegram_message_id=message_id,
            chat_id=-100123,
            user_id=777,
            username="@tester",
            full_name="Tester",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=now,
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(
            has_urls=True,
            url_count=1,
            repeated_recent_messages=2,
            messages_last_60s=4,
        ),
    )


def make_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="spam",
        severity="high",
        confidence=0.99,
        action="ban",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Повторяющийся спам.",
        current_message_evidence=[
            "Текущее сообщение содержит рекламную ссылку."
        ],
        context_evidence=[
            "Сообщение повторяется несколько раз."
        ],
    )


@pytest.mark.asyncio
async def test_equivalent_admin_alert_is_deduplicated_when_notifications_enabled(
    tmp_path,
):
    database_path = (
        tmp_path
        / "dedup.db"
    )

    database = Database(
        "sqlite+aiosqlite:///"
        f"{database_path.as_posix()}"
    )

    await database.init()

    audit = AuditRepository(
        database.session_factory
    )

    notifier = FakeNotifier()

    # Control Center disables routine autonomous DMs by default.
    # This test explicitly enables them to test the throttle itself.
    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=notifier,
        dry_run=True,
        admin_alert_dedup_seconds=30,
        notify_autonomous_actions=True,
    )

    decision = make_decision()

    context1 = make_context(1)
    context2 = make_context(2)

    policy1 = PolicyGate().evaluate(
        decision,
        context=context1,
    )

    policy2 = PolicyGate().evaluate(
        decision,
        context=context2,
    )

    await executor.execute(
        context=context1,
        decision=decision,
        policy=policy1,
    )

    await executor.execute(
        context=context2,
        decision=decision,
        policy=policy2,
    )

    assert notifier.calls == 1

    events = await audit.get_recent_events(
        chat_id=-100123
    )

    # Audit remains complete.
    assert len(events) == 2

    await database.dispose()
