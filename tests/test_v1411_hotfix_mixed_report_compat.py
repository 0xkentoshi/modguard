from datetime import datetime, timezone

import pytest

from app.agent.moderator import ModeratorAgent
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot
from app.llm.base import LLMProvider
from app.utils.text_normalization import analyze_text


class FakeProvider(LLMProvider):
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def generate_structured(self, *, system_prompt, user_prompt, response_model):
        self.calls += 1
        return response_model.model_validate(self.payload)


def make_context(text: str) -> MessageContext:
    target = MessageSnapshot(
        db_id=10,
        telegram_message_id=10,
        chat_id=-100123,
        user_id=111,
        username="@target",
        full_name="Target",
        content_type="text",
        raw_text="подозрительное сообщение",
        normalized_text="подозрительное сообщение",
        telegram_date=datetime.now(timezone.utc),
    )
    current = MessageSnapshot(
        db_id=11,
        telegram_message_id=11,
        chat_id=-100123,
        user_id=222,
        username="@reporter",
        full_name="Reporter",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        reply_to_message_id=10,
        telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=current,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(is_reply=True),
        recent_chat_messages=[target],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=target,
    )


@pytest.mark.asyncio
async def test_legacy_mixed_report_payload_without_new_field_stays_a_report():
    provider = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            # Intentionally omitted: explicit_moderation_request
            "reason": "The reply reports the target but also contains its own direct threat.",
        }
    )
    agent = ModeratorAgent(provider, fast_provider=provider)
    result = await agent.classify_reply_intent(
        make_context("это скам, а автора я найду и убью")
    )
    assert result.report_target is True
    assert result.reporter_has_independent_violation is True
    assert result.explicit_moderation_request is None


@pytest.mark.asyncio
async def test_explicit_false_still_blocks_fake_report_branch_for_hostile_reply():
    provider = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            "explicit_moderation_request": False,
            "reason": "Hostile reply misclassified as report.",
        }
    )
    agent = ModeratorAgent(provider)
    result = await agent.classify_reply_intent(
        make_context("я тебя найду и разъебу, это не шутка")
    )
    assert result.report_target is False
    assert result.reporter_has_independent_violation is True
    assert result.explicit_moderation_request is False


@pytest.mark.asyncio
async def test_explicit_true_keeps_real_mixed_report_branch():
    provider = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            "explicit_moderation_request": True,
            "reason": "Explicit moderator request plus separate threat.",
        }
    )
    agent = ModeratorAgent(provider)
    result = await agent.classify_reply_intent(
        make_context("модеры проверьте это, а автора я найду и убью")
    )
    assert result.report_target is True
    assert result.reporter_has_independent_violation is True
    assert result.explicit_moderation_request is True
