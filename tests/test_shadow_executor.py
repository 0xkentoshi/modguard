from datetime import datetime, timezone

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision
from app.database.db import Database
from app.database.repository import AuditRepository
from app.moderation.executor import ModerationExecutor
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeNotifier:
    async def notify_decision(self, **kwargs):
        return True


def make_context():
    text = "JOIN https://example.com FREE MONEY"
    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=1,
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
        behavior_signals=BehaviorSignals(has_urls=True, url_count=1),
    )


@pytest.mark.asyncio
async def test_shadow_blocks_real_delete(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'shadow.db').as_posix()}"
    )
    await database.init()

    audit = AuditRepository(database.session_factory)
    control = ControlRepository(database.session_factory)

    await control.ensure_chat_settings(chat_id=-100123)
    await control.toggle_shadow(-100123)

    bot = FakeBot()

    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=control,
    )

    decision = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="spam",
        severity="high",
        confidence=0.99,
        action="delete",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Spam.",
        current_message_evidence=["Current message is spam."],
        context_evidence=[],
    )

    context = make_context()
    policy = PolicyGate().evaluate(decision, context=context)

    result = await executor.execute(
        context=context,
        decision=decision,
        policy=policy,
    )

    assert result.executed is False
    assert result.dry_run is True
    assert bot.deleted == []

    await database.dispose()
