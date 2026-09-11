from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision
from app.database.db import Database
from app.database.repository import AuditRepository
from app.moderation.executor import ModerationExecutor
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeNotifier:
    async def notify_decision(self, **kwargs):
        return True


class FakeBot:
    def __init__(self):
        self.bans = []
        self.deletes = []
        self.mutes = []

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def ban_chat_member(self, *, chat_id, user_id):
        self.bans.append((chat_id, user_id))

    async def delete_message(self, *, chat_id, message_id):
        self.deletes.append((chat_id, message_id))

    async def restrict_chat_member(self, *, chat_id, user_id, permissions, until_date):
        self.mutes.append((chat_id, user_id))


class FakeControlRepo:
    def __init__(self):
        self.shadow = False

    async def ensure_chat_settings(self, *, chat_id):
        return SimpleNamespace(shadow_mode=self.shadow)

    async def get_live_ban_enabled(self, chat_id):
        return True

    async def get_mute_duration_minutes(self, chat_id):
        return 60

    async def record_moderation_ban(self, **kwargs):
        return None

    async def remember_confirmed_pattern(self, **kwargs):
        return None


class BlockingCircuit:
    def __init__(self):
        self.preflight_calls = []
        self.records = []

    async def preflight(self, *, chat_id, planned_action):
        self.preflight_calls.append((chat_id, planned_action))
        return False

    async def record_execution(self, **kwargs):
        self.records.append(kwargs)


class RecordingCircuit:
    def __init__(self):
        self.preflight_calls = []
        self.records = []

    async def preflight(self, *, chat_id, planned_action):
        self.preflight_calls.append((chat_id, planned_action))
        return True

    async def record_execution(self, **kwargs):
        self.records.append(kwargs)


def make_context():
    text = "connect wallet at https://evil.example"
    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=9,
            chat_id=-10055,
            user_id=77,
            username="@fake_support",
            full_name="Fake Support",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=datetime.now(timezone.utc),
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(has_urls=True, url_count=1),
    )


def make_decision():
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="phishing",
        severity="critical",
        confidence=0.99,
        action="ban",
        delete_message=True,
        needs_human_review=False,
        reason="credential theft",
        current_message_evidence=["wallet verification link"],
        context_evidence=[],
    )


@pytest.mark.asyncio
async def test_preflight_trip_converts_live_action_to_shadow_without_touching_telegram(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "block.db").as_posix())
    await db.init()
    bot = FakeBot()
    circuit = BlockingCircuit()
    repo = FakeControlRepo()
    executor = ModerationExecutor(
        audit_repository=AuditRepository(db.session_factory),
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=repo,
        safety_circuit=circuit,
    )
    ctx = make_context()
    decision = make_decision()
    policy = PolicyGate().evaluate(decision, context=ctx)

    result = await executor.execute(context=ctx, decision=decision, policy=policy)

    assert circuit.preflight_calls == [(-10055, "ban")]
    assert result.dry_run is True
    assert result.executed is False
    assert bot.bans == []
    assert bot.mutes == []
    assert bot.deletes == []
    await db.dispose()


@pytest.mark.asyncio
async def test_successful_live_action_is_reported_back_to_circuit(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "record.db").as_posix())
    await db.init()
    bot = FakeBot()
    circuit = RecordingCircuit()
    repo = FakeControlRepo()
    executor = ModerationExecutor(
        audit_repository=AuditRepository(db.session_factory),
        notifier=FakeNotifier(),
        bot=bot,
        dry_run=False,
        live_delete_enabled=True,
        control_repository=repo,
        safety_circuit=circuit,
    )
    ctx = make_context()
    decision = make_decision()
    policy = PolicyGate().evaluate(decision, context=ctx)

    result = await executor.execute(context=ctx, decision=decision, policy=policy)

    assert result.executed is True
    assert result.final_action == "ban"
    assert bot.bans == [(-10055, 77)]
    assert bot.deletes == [(-10055, 9)]
    assert len(circuit.records) == 1
    assert circuit.records[0]["action"] == "ban"
    assert circuit.records[0]["success"] is True
    await db.dispose()
