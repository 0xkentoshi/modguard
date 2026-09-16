from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent.moderator import ModeratorAgent
from app.agent.report_intent import ReportIntentDecision, REPORT_INTENT_SYSTEM_PROMPT
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


def snap(message_id: int, user_id: int, text: str, reply_to: int | None = None):
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
        telegram_date=datetime.now(timezone.utc),
    )


def reply_context(current_text: str, target_text: str = "обычное сообщение") -> MessageContext:
    target = snap(10, 111, target_text)
    current = snap(11, 222, current_text, reply_to=10)
    return MessageContext(
        current_message=current,
        text_signals=analyze_text(current_text),
        behavior_signals=BehaviorSignals(is_reply=True),
        recent_chat_messages=[target],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=target,
    )


@pytest.mark.asyncio
async def test_hostile_reply_without_moderation_request_cannot_open_report_branch():
    fast = FakeProvider(
        {
            "report_target": False,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            "explicit_moderation_request": False,
            "reason": "Hostile ordinary reply.",
        }
    )
    deep = FakeProvider(
        {
            "report_target": True,
            "confidence": 0.99,
            "reporter_has_independent_violation": True,
            "explicit_moderation_request": False,
            "reason": "Misclassified reply as a report.",
        }
    )
    agent = ModeratorAgent(deep, fast_provider=fast)

    result = await agent.classify_reply_intent(
        reply_context("я тебя найду и разъебу, это не шутка")
    )

    assert isinstance(result, ReportIntentDecision)
    assert result.report_target is False
    assert result.reporter_has_independent_violation is True
    assert result.explicit_moderation_request is False
    assert fast.calls == 1
    assert deep.calls == 1


@pytest.mark.asyncio
async def test_explicit_mixed_report_can_still_review_target():
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
        reply_context("модеры проверьте это, а автора я найду и убью")
    )

    assert result.report_target is True
    assert result.reporter_has_independent_violation is True
    assert result.explicit_moderation_request is True


def test_report_prompt_distinguishes_threat_from_report_intent():
    assert "explicit_moderation_request" in REPORT_INTENT_SYSTEM_PROMPT
    assert "я тебя найду и разъебу, это не шутка" in REPORT_INTENT_SYSTEM_PROMPT
    assert "report_target=false" in REPORT_INTENT_SYSTEM_PROMPT


def test_pure_reporter_branch_bypasses_community_policy_and_semantic_state():
    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    start = source.index("# REPORT-FIRST ROUTE")
    end = source.index("# Ordinary moderation path.", start)
    branch = source[start:end]

    assert "REPORTER SAFE BYPASS" in branch
    assert "apply_overlay" not in branch
    assert "apply_core_guard" not in branch
    assert "semantic_cluster_service.enqueue" not in branch
    assert "process_report_rereview" in branch
