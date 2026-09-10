from datetime import datetime, timezone

import pytest

from app.agent.moderator import ModeratorAgent
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
)
from app.llm.base import LLMProvider
from app.utils.text_normalization import analyze_text


class FakeLLMProvider(LLMProvider):
    def __init__(self, response_data: dict):
        self.response_data = response_data
        self.last_system_prompt = None
        self.last_user_prompt = None

    async def generate_structured(
        self,
        *,
        system_prompt,
        user_prompt,
        response_model,
    ):
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt

        return response_model.model_validate(
            self.response_data
        )


def make_context(text: str) -> MessageContext:
    now = datetime.now(timezone.utc)

    current = MessageSnapshot(
        db_id=1,
        telegram_message_id=10,
        chat_id=-100123,
        user_id=500,
        username="@tester",
        full_name="Tester",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=now,
    )

    return MessageContext(
        current_message=current,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(
            has_urls=True,
            url_count=1,
        ),
    )


@pytest.mark.asyncio
async def test_moderator_returns_structured_decision():
    provider = FakeLLMProvider(
        {
            "detected_language": "mixed",
            "current_message_violation": True,
            "category": "scam",
            "severity": "critical",
            "confidence": 0.98,
            "action": "ban",
            "delete_message": True,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Очевидная схема мошенничества.",
            "current_message_evidence": [
                "External link",
                "Obfuscated promotional text",
            ],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(provider)

    context = make_context(
        "FRЕЕ UЅDT https://example.com"
    )

    result = await agent.analyze(context)

    assert result.category == "scam"
    assert result.action == "ban"
    assert result.confidence == 0.98
    assert result.current_message_violation is True

    assert (
        "FRЕЕ UЅDT"
        in provider.last_user_prompt
    )


@pytest.mark.asyncio
async def test_prompt_contains_multilingual_rules():
    provider = FakeLLMProvider(
        {
            "detected_language": "Chinese",
            "current_message_violation": False,
            "category": "safe",
            "severity": "none",
            "confidence": 0.95,
            "action": "allow",
            "delete_message": False,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Нарушений не обнаружено.",
            "current_message_evidence": [],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(provider)

    context = make_context(
        "免费领取 USDT"
    )

    result = await agent.analyze(context)

    assert result.action == "allow"
    assert result.current_message_violation is False

    # Current compact prompt wording.
    assert (
        "Analyze any language"
        in provider.last_system_prompt
    )

    assert (
        "mixed languages"
        in provider.last_system_prompt
    )

    assert (
        "免费领取"
        in provider.last_user_prompt
    )
