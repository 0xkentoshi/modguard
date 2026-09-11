from datetime import datetime, timezone
from pathlib import Path

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
        return response_model.model_validate(self.payload)


def reply_context(text: str) -> MessageContext:
    now = datetime.now(timezone.utc)

    target = MessageSnapshot(
        db_id=10,
        telegram_message_id=10,
        chat_id=-10042,
        user_id=101,
        username="@target",
        full_name="Target",
        content_type="text",
        raw_text="йоу всем привет",
        normalized_text="йоу всем привет",
        telegram_date=now,
    )

    current = MessageSnapshot(
        db_id=11,
        telegram_message_id=11,
        chat_id=-10042,
        user_id=202,
        username="@author",
        full_name="Author",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        reply_to_message_id=10,
        telegram_date=now,
    )

    return MessageContext(
        current_message=current,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(
            is_reply=True,
            messages_last_60s=1,
        ),
        recent_chat_messages=[target],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=target,
    )


@pytest.mark.asyncio
async def test_non_report_reply_cannot_take_fast_safe_shortcut():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.99,
            "reason": "Small model missed the interpersonal attack.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "Russian",
            "current_message_violation": True,
            "category": "harassment",
            "severity": "medium",
            "confidence": 0.98,
            "action": "warn",
            "delete_message": False,
            "mute_minutes": None,
            "needs_human_review": False,
            "conflict_context": "clear_current_aggressor",
            "reason": "Direct targeted degradation of the replied-to participant.",
            "current_message_evidence": ["Direct targeted insult in the current reply."],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(deep, fast_provider=fast)

    result = await agent.analyze(
        reply_context("ты тупой идиот заткнись"),
        force_deep=True,
    )

    assert result.category == "harassment"
    assert result.action == "warn"
    assert result.conflict_context == "clear_current_aggressor"

    # The Telegram runtime requests force_deep only after its separate
    # report-intent preflight has classified this as ordinary moderation.
    assert fast.calls == 0
    assert deep.calls == 1


@pytest.mark.asyncio
async def test_retaliatory_insult_reply_also_goes_to_deep():
    fast = FakeProvider(
        {
            "route": "safe",
            "confidence": 0.99,
            "reason": "Safe.",
        }
    )

    deep = FakeProvider(
        {
            "detected_language": "Russian",
            "current_message_violation": True,
            "category": "harassment",
            "severity": "medium",
            "confidence": 0.98,
            "action": "warn",
            "delete_message": False,
            "mute_minutes": None,
            "needs_human_review": False,
            "conflict_context": "clear_current_aggressor",
            "reason": "Retaliation still contains direct targeted degradation.",
            "current_message_evidence": ["Direct targeted insult in the current reply."],
            "context_evidence": [],
        }
    )

    agent = ModeratorAgent(deep, fast_provider=fast)

    result = await agent.analyze(
        reply_context("сам заткнись, ты тупой идиот"),
        force_deep=True,
    )

    assert result.category == "harassment"
    assert result.action == "warn"
    assert result.conflict_context == "clear_current_aggressor"
    assert fast.calls == 0
    assert deep.calls == 1


def test_deep_prompt_explicitly_treats_both_sides_independently():
    source = Path("app/agent/prompts.py").read_text(encoding="utf-8")

    assert "retaliation does NOT erase the CURRENT AUTHOR'S own targeted abuse" in source
    assert 'semantically equivalent to "ты тупой идиот, заткнись"' in source
    assert 'semantically equivalent to "сам заткнись, ты тупой идиот"' in source


def test_report_prompt_does_not_confuse_direct_insult_with_report():
    source = Path("app/agent/report_intent.py").read_text(encoding="utf-8")

    assert '"ты тупой идиот, заткнись" -> report_target=false' in source
    assert '"сам заткнись, ты тупой идиот" -> report_target=false' in source
    assert "NOT a report merely because it refers to that participant" in source


def test_handler_forces_deep_only_after_reply_report_preflight():
    handler_source = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    report_first_pos = handler_source.find("# REPORT-FIRST ROUTE")
    force_pos = handler_source.find("force_reply_deep = (")
    analyze_pos = handler_source.find("force_deep=force_reply_deep")

    assert report_first_pos >= 0
    assert force_pos > report_first_pos
    assert analyze_pos > force_pos
    assert "not report_preflight.report_target" in handler_source
    assert "report_preflight.reporter_has_independent_violation" in handler_source


def test_global_reply_deep_bypass_is_not_inside_moderator_analyze():
    moderator_source = Path("app/agent/moderator.py").read_text(encoding="utf-8")

    # Report preservation tests call ModeratorAgent directly, so globally forcing
    # every reply to Deep here would bypass Fast report semantics again.
    assert "reason=reply_after_report_preflight" not in moderator_source
