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


def report_context():
    target = snapshot(
        message_id=10,
        user_id=111,
        text="Есть тема с профитом, напиши в личку",
    )

    report = snapshot(
        message_id=11,
        user_id=222,
        text="что-то мутно выглядит, проверьте это",
        reply_to=10,
    )

    return MessageContext(
        current_message=report,
        text_signals=analyze_text(
            report.raw_text
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
async def test_fast_report_true_with_zero_report_confidence_is_preserved():
    fast = FakeProvider(
        {
            "route": "deep",
            "confidence": 0.80,
            "reason": "Looks like a report.",
            "report_target": True,
            "report_confidence": 0.0,
            "report_reason": "",
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
            "report_target": False,
            "report_confidence": 0.0,
            "report_reason": "",
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    decision = await agent.analyze(
        report_context()
    )

    assert decision.report_target is True
    assert decision.report_confidence >= 0.80
    assert decision.current_message_violation is False
    assert decision.action == "allow"

    # Once Fast AI semantically identifies a report, we do not need to
    # spend a Deep call moderating the reporter before re-reviewing target.
    assert deep.calls == 0


@pytest.mark.asyncio
async def test_deep_report_true_with_default_zero_confidence_is_repaired():
    fast = FakeProvider(
        {
            "route": "deep",
            "confidence": 0.80,
            "reason": "Needs deep.",
            "report_target": False,
            "report_confidence": 0.0,
            "report_reason": "",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "Russian",
            "current_message_violation": False,
            "category": "other",
            "severity": "low",
            "confidence": 0.95,
            "action": "allow",
            "delete_message": False,
            "mute_minutes": None,
            "needs_human_review": False,
            "reason": "Это жалоба на сообщение выше.",
            "current_message_evidence": [],
            "context_evidence": [],
            "report_target": True,
            "report_confidence": 0.0,
            "report_reason": "",
        }
    )

    agent = ModeratorAgent(
        deep,
        fast_provider=fast,
    )

    decision = await agent.analyze(
        report_context()
    )

    assert decision.report_target is True
    assert decision.report_confidence >= 0.80
    assert deep.calls == 1
