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


class FakeProvider(LLMProvider):
    def __init__(
        self,
        response: dict,
    ):
        self.response = response
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
            self.response
        )


def make_context(
    text: str,
    *,
    repeated: int = 0,
):
    now = datetime.now(
        timezone.utc
    )

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
            telegram_date=now,
        ),
        text_signals=analyze_text(
            text
        ),
        behavior_signals=BehaviorSignals(
            repeated_recent_messages=repeated,
            messages_last_60s=1,
        ),
    )


@pytest.mark.asyncio
async def test_fast_safe_path_uses_independent_challenge_and_does_not_call_deep():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.99,
            "reason": "Clearly harmless.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "Russian",
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

    result = await agent.analyze(
        make_context(
            "Кто тестировал Ollama?"
        )
    )

    assert result.action == "allow"
    # Stabilization v3 deliberately performs two Fast passes:
    # normal triage + skeptical SAFE challenge.
    assert fast.calls == 2
    assert deep.calls == 0


@pytest.mark.asyncio
async def test_fast_deep_route_calls_deep():
    fast = FakeProvider(
        {
            "route": "deep",
            "confidence": 0.99,
            "reason": "Explicit physical threat.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "Russian",
            "current_message_violation": True,
            "category": "threat",
            "severity": "high",
            "confidence": 0.98,
            "action": "mute",
            "delete_message": True,
            "mute_minutes": 360,
            "needs_human_review": False,
            "reason": "Explicit physical threat.",
            "current_message_evidence": [
                "Я тебя найду и сломаю тебе ебало"
            ],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    result = await agent.analyze(
        make_context(
            "Я тебя найду и сломаю тебе ебало"
        )
    )

    assert result.category == "threat"
    assert fast.calls == 1
    assert deep.calls == 1


@pytest.mark.asyncio
async def test_repeated_message_skips_fast_and_goes_deep():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.99,
            "reason": "Safe.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "English",
            "current_message_violation": True,
            "category": "spam",
            "severity": "high",
            "confidence": 0.98,
            "action": "delete",
            "delete_message": True,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Repeated spam.",
            "current_message_evidence": [
                "JOIN FREE MONEY"
            ],
            "context_evidence": [
                "Repeated message"
            ],
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    result = await agent.analyze(
        make_context(
            "JOIN FREE MONEY",
            repeated=1,
        )
    )

    assert result.category == "spam"
    assert fast.calls == 0
    assert deep.calls == 1
