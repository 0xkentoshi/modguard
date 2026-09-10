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


def make_context():
    now = datetime.now(
        timezone.utc
    )

    text = (
        "FREE USDT "
        "https://example.com"
    )

    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=50,
            chat_id=-100123,
            user_id=777,
            username="@spam",
            full_name="Spam",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=now,
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(
            has_urls=True,
            url_count=1,
        ),
    )


@pytest.mark.asyncio
async def test_dry_run_creates_audit_but_does_not_execute(
    tmp_path,
):
    database_path = (
        tmp_path
        / "executor.db"
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

    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=notifier,
        dry_run=True,
    )

    decision = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="scam",
        severity="critical",
        confidence=0.99,
        action="ban",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Очевидный scam.",
        current_message_evidence=[
            "Current message contains a suspicious promotion",
            "Current message contains an external URL",
        ],
        context_evidence=[],
    )

    context = make_context()

    policy = PolicyGate().evaluate(
        decision,
        context=context,
    )

    result = await executor.execute(
        context=context,
        decision=decision,
        policy=policy,
    )

    assert result.dry_run is True
    assert result.executed is False
    assert result.final_action == "ban"

    events = await audit.get_recent_events(
        chat_id=-100123
    )

    assert len(events) == 1
    assert events[0].action == "ban"
    assert events[0].category == "scam"

    # New Control Center behavior:
    # routine autonomous actions do not create separate DM reports.
    assert notifier.calls == 0

    await database.dispose()
