from datetime import datetime, timezone

import pytest

from app.agent.context import (
    reported_target_context,
)
from app.agent.moderator import ModeratorAgent
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
)
from app.llm.base import LLMProvider
from app.utils.text_normalization import analyze_text


class FakeProvider(LLMProvider):
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0
        self.last_user_prompt = None

    async def generate_structured(
        self,
        *,
        system_prompt,
        user_prompt,
        response_model,
    ):
        self.calls += 1
        self.last_user_prompt = user_prompt
        return response_model.model_validate(
            self.response
        )


def snapshot(
    *,
    message_id: int,
    user_id: int,
    text: str,
    reply_to: int | None = None,
):
    return MessageSnapshot(
        db_id=message_id,
        telegram_message_id=message_id,
        chat_id=-100123,
        user_id=user_id,
        username=f"@u{user_id}",
        full_name=f"User {user_id}",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        reply_to_message_id=reply_to,
        telegram_date=datetime.now(
            timezone.utc
        ),
    )


def make_report_context(
    report_text: str,
    target_text: str,
):
    target = snapshot(
        message_id=10,
        user_id=111,
        text=target_text,
    )

    report = snapshot(
        message_id=11,
        user_id=222,
        text=report_text,
        reply_to=10,
    )

    return MessageContext(
        current_message=report,
        text_signals=analyze_text(
            report_text
        ),
        behavior_signals=BehaviorSignals(
            is_reply=True
        ),
        recent_chat_messages=[
            target
        ],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=target,
    )


@pytest.mark.asyncio
async def test_semantic_report_intent_keeps_reporter_safe():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.97,
            "reason": "The reply is a report.",
            "report_target": True,
            "report_confidence": 0.96,
            "report_reason": "User is flagging the replied-to message.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "English",
            "current_message_violation": True,
            "category": "scam",
            "severity": "high",
            "confidence": 0.98,
            "action": "ban",
            "delete_message": True,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Очевидный скам.",
            "current_message_evidence": [
                "Fake giveaway"
            ],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    context = make_report_context(
        "this looks shady, check the message above",
        "FREE 500 USDT connect wallet",
    )

    reporter_decision = await agent.analyze(
        context
    )

    assert reporter_decision.action == "allow"
    assert (
        reporter_decision
        .current_message_violation
        is False
    )
    assert reporter_decision.report_target is True
    assert fast.calls == 1
    assert deep.calls == 0

    target_decision = (
        await agent
        .review_reported_target(
            context
        )
    )

    assert target_decision.category == "scam"
    assert target_decision.action == "ban"
    assert deep.calls == 1

    assert (
        "FREE 500 USDT connect wallet"
        in deep.last_user_prompt
    )

    assert (
        "this looks shady"
        in deep.last_user_prompt
    )


def test_reported_target_becomes_current_message_for_policy():
    context = make_report_context(
        "проверьте сообщение выше",
        "suspicious target",
    )

    target_context = (
        reported_target_context(
            context
        )
    )

    assert target_context is not None

    assert (
        target_context
        .current_message
        .telegram_message_id
        == 10
    )

    assert (
        target_context
        .current_message
        .user_id
        == 111
    )

    assert (
        target_context
        .current_message
        .raw_text
        == "suspicious target"
    )


@pytest.mark.asyncio
async def test_normal_reply_is_not_forced_into_report_flow():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.99,
            "reason": "Ordinary reply.",
            "report_target": False,
            "report_confidence": 0.02,
            "report_reason": "",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "unknown",
            "current_message_violation": False,
            "category": "safe",
            "severity": "none",
            "confidence": 1.0,
            "action": "allow",
            "delete_message": False,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Safe.",
            "current_message_evidence": [],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    context = make_report_context(
        "ахах да",
        "обычное сообщение",
    )

    decision = await agent.analyze(
        context
    )

    assert decision.action == "allow"
    assert decision.report_target is False
    assert deep.calls == 0
