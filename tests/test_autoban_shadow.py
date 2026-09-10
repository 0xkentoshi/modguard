from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
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


class FakeBot:
    def __init__(self):
        self.deleted = []
        self.banned = []
        self.sent = []
        self._next_message_id = 1000

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def ban_chat_member(self, *, chat_id, user_id):
        self.banned.append((chat_id, user_id))

    async def send_message(self, *, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)


class FakeNotifier:
    async def notify_decision(self, **kwargs):
        return True


class FakeDashboard:
    def __init__(self, bot):
        self.bot = bot
        self.admin_ids = {999}
        self.refreshed = []

    def request_refresh(self, chat_id):
        self.refreshed.append(chat_id)


def make_context():
    text = "FREE 500 USDT connect wallet https://example.com"
    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=50,
            chat_id=-100123,
            user_id=777,
            username="@scammer",
            full_name="Scammer",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=datetime.now(timezone.utc),
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(
            has_urls=True,
            url_count=1,
        ),
    )


def make_ban_decision():
    return ModerationDecision(
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
        current_message_evidence=["Fake reward and wallet bait"],
        context_evidence=[],
    )


@pytest.mark.asyncio
async def test_autoban_defaults_off_and_can_toggle(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'autoban.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(database.session_factory)

    assert await repository.get_live_ban_enabled(-100123) is False
    assert await repository.toggle_live_ban(-100123) is True
    assert await repository.get_live_ban_enabled(-100123) is True
    assert await repository.toggle_live_ban(-100123) is False

    await database.dispose()


@pytest.mark.asyncio
async def test_autoban_on_bans_and_deletes_live_scam(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'live-ban.db').as_posix()}"
    )
    await database.init()

    audit = AuditRepository(database.session_factory)
    control = ControlRepository(database.session_factory)
    await control.toggle_live_ban(-100123)

    bot = FakeBot()
    dashboard = FakeDashboard(bot)

    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=control,
        dashboard_service=dashboard,
    )

    context = make_context()
    decision = make_ban_decision()
    policy = PolicyGate().evaluate(decision, context=context)

    result = await executor.execute(
        context=context,
        decision=decision,
        policy=policy,
    )

    assert result.executed is True
    assert result.final_action == "ban"
    assert bot.banned == [(-100123, 777)]
    assert bot.deleted == [(-100123, 50)]

    await database.dispose()


@pytest.mark.asyncio
async def test_shadow_blocks_ban_and_delete_and_notifies_admin(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'shadow-ban.db').as_posix()}"
    )
    await database.init()

    audit = AuditRepository(database.session_factory)
    control = ControlRepository(database.session_factory)
    await control.toggle_live_ban(-100123)
    await control.ensure_chat_settings(chat_id=-100123)
    await control.toggle_shadow(-100123)

    bot = FakeBot()
    dashboard = FakeDashboard(bot)

    executor = ModerationExecutor(
        audit_repository=audit,
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=control,
        dashboard_service=dashboard,
    )

    context = make_context()
    decision = make_ban_decision()
    policy = PolicyGate().evaluate(decision, context=context)

    result = await executor.execute(
        context=context,
        decision=decision,
        policy=policy,
    )

    assert result.dry_run is True
    assert result.executed is False
    assert bot.banned == []
    assert bot.deleted == []
    assert any(
        "SHADOW · WOULD BAN + DELETE" in text
        for _, text in bot.sent
    )

    await database.dispose()
