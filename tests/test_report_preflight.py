from datetime import datetime, timezone

import pytest

from app.agent.moderator import ModeratorAgent
from app.agent.report_intent import ReportIntentDecision
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
)
from app.llm.base import LLMProvider
from app.utils.text_normalization import analyze_text


class FakeProvider(LLMProvider):
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def generate_structured(
        self,
        *,
        system_prompt,
        user_prompt,
        response_model,
    ):
        self.calls += 1
        return response_model.model_validate(
            self.payload
        )


def make_context(
    report_text: str,
    target_text: str,
):
    target = MessageSnapshot(
        db_id=10,
        telegram_message_id=10,
        chat_id=-100123,
        user_id=111,
        username="@target",
        full_name="Target",
        content_type="text",
        raw_text=target_text,
        normalized_text=target_text,
        telegram_date=datetime.now(
            timezone.utc
        ),
    )

    report = MessageSnapshot(
        db_id=11,
        telegram_message_id=11,
        chat_id=-100123,
        user_id=222,
        username="@reporter",
        full_name="Reporter",
        content_type="text",
        raw_text=report_text,
        normalized_text=report_text,
        reply_to_message_id=10,
        telegram_date=datetime.now(
            timezone.utc
        ),
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
async def test_report_preflight_detects_complaint_before_moderation():
    fast = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.97,
            "reporter_has_independent_violation": False,
            "reason": (
                "The reply asks moderators to inspect "
                "the replied-to message."
            ),
        }
    )

    deep = FakeProvider(
        {
            "report_target": False,
            "confidence": 1.0,
            "reporter_has_independent_violation": False,
            "reason": "Unused.",
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    result = await agent.classify_reply_intent(
        make_context(
            "что за хрень, модеры проверьте",
            "есть каки буду сраки",
        )
    )

    assert isinstance(
        result,
        ReportIntentDecision,
    )

    assert result.report_target is True

    assert (
        result
        .reporter_has_independent_violation
        is False
    )

    assert fast.calls == 1
    assert deep.calls == 0


@pytest.mark.asyncio
async def test_report_preflight_can_mark_separate_reporter_violation():
    fast = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            "reason": (
                "The reply reports the target but also "
                "contains its own direct threat."
            ),
        }
    )

    agent = ModeratorAgent(
        fast,
        fast_provider=fast,
    )

    result = await agent.classify_reply_intent(
        make_context(
            "это скам, а автора я найду и убью",
            "подозрительное сообщение",
        )
    )

    assert result.report_target is True
    assert (
        result
        .reporter_has_independent_violation
        is True
    )


@pytest.mark.asyncio
async def test_uncertain_fast_non_report_uses_deep_intent_check():
    fast = FakeProvider(
        {
            "report_target": False,
            "confidence": 0.55,
            "reporter_has_independent_violation": False,
            "reason": "Uncertain.",
        }
    )

    deep = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.95,
            "reporter_has_independent_violation": False,
            "reason": "Semantic complaint.",
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    result = await agent.classify_reply_intent(
        make_context(
            "я бы это перепроверил",
            "есть схема заработка",
        )
    )

    assert result.report_target is True
    assert fast.calls == 1
    assert deep.calls == 1
