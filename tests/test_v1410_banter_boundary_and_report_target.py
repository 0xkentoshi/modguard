from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.moderator import ModeratorAgent
from app.agent.report_intent import ReportIntentDecision
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
)
from app.community_policy.schemas import CommunityPolicyRule
from app.community_policy.service import CommunityPolicyService, rules_json
from app.llm.base import LLMProvider
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeProvider(LLMProvider):
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = 0
        self.prompts = []

    async def generate_structured(
        self,
        *,
        system_prompt,
        user_prompt,
        response_model,
    ):
        self.calls += 1
        self.prompts.append((system_prompt, user_prompt, response_model.__name__))
        payload = self.responses[response_model.__name__]
        return response_model.model_validate(payload)


class FakeRepository:
    def __init__(self, active=None):
        self.active = active

    async def get_active_community_policy(self, chat_id):
        return self.active


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


def report_context(report_text: str, target_text: str) -> MessageContext:
    target = snap(10, 111, target_text)
    report = snap(11, 222, report_text, reply_to=10)
    return MessageContext(
        current_message=report,
        text_signals=analyze_text(report_text),
        behavior_signals=BehaviorSignals(is_reply=True),
        recent_chat_messages=[target],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=target,
    )


@pytest.mark.asyncio
async def test_fast_099_non_report_is_deep_rechecked_for_explicit_report_phrase():
    fast = FakeProvider(
        {
            "ReportIntentDecision": {
                "report_target": False,
                "confidence": 0.99,
                "reporter_has_independent_violation": False,
                "reason": "Ordinary reply.",
            }
        }
    )
    deep = FakeProvider(
        {
            "ReportIntentDecision": {
                "report_target": True,
                "confidence": 0.99,
                "reporter_has_independent_violation": False,
                "reason": "The reply asks admins to inspect the replied-to message.",
            }
        }
    )
    agent = ModeratorAgent(deep, fast_provider=fast)

    result = await agent.classify_reply_intent(
        report_context(
            "админы проверьте это",
            "иди нахуй, я тебя разъебу",
        )
    )

    assert isinstance(result, ReportIntentDecision)
    assert result.report_target is True
    assert result.reporter_has_independent_violation is False
    assert fast.calls == 1
    assert deep.calls == 1


def harassment_warn_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="Russian",
        current_message_violation=True,
        category="harassment",
        severity="medium",
        confidence=0.95,
        action="warn",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=False,
        reason="Targeted harassment after the other participant asked to stop.",
        current_message_evidence=["Targeted degradation in the current message."],
        context_evidence=[],
    )


def boundary_context() -> MessageContext:
    stop = snap(20, 222, "всё, хорош уже, реально заебал", reply_to=19)
    current = snap(21, 111, "иди нахуй чмо, мне похуй что ты сказал", reply_to=20)
    return MessageContext(
        current_message=current,
        text_signals=analyze_text(current.raw_text),
        behavior_signals=BehaviorSignals(is_reply=True),
        recent_chat_messages=[
            snap(18, 111, "ну ты долбоеб ахах", reply_to=17),
            snap(19, 222, "сам иди нахуй ахаха", reply_to=18),
            stop,
        ],
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=stop,
    )


@pytest.mark.asyncio
async def test_learned_banter_allow_is_blocked_after_request_to_stop():
    rule = CommunityPolicyRule(
        rule_id="R1",
        source="shadow_feedback",
        source_ref=33,
        title="Friendly banter",
        condition=(
            "Allow explicit degradation or aggression only when it is mutual, "
            "voluntary and playful, with no request to stop."
        ),
        action="allow",
        policy_family="harassment",
    )
    repository = FakeRepository(
        SimpleNamespace(version=1, rules_json=rules_json([rule]))
    )
    deep = FakeProvider(
        {
            "CommunityPolicyMatch": {
                "matched": True,
                "confidence": 0.95,
                "winning_rule_id": "R1",
                "matched_rule_ids": ["R1"],
                "ambiguous": False,
                "reason": "The current message resembles the learned banter family.",
                "current_message_evidence": ["Targeted profanity."],
            },
            "LearnedAllowBoundaryReview": {
                "blocked": True,
                "confidence": 0.98,
                "reason": "The reply target explicitly asked the participant to stop.",
                "boundary_evidence": ["всё, хорош уже, реально заебал"],
            },
        }
    )
    service = CommunityPolicyService(
        repository=repository,
        deep_provider=deep,
        fast_provider=None,
    )
    context = boundary_context()
    gate = PolicyGate()
    baseline_decision = harassment_warn_decision()
    baseline_policy = gate.evaluate(baseline_decision, context=context)

    decision, policy = await service.apply_overlay(
        context=context,
        baseline_decision=baseline_decision,
        baseline_policy=baseline_policy,
        policy_gate=gate,
    )

    assert decision is baseline_decision
    assert policy is baseline_policy
    assert policy.final_action == "warn"
    assert deep.calls == 2
    assert any(name == "LearnedAllowBoundaryReview" for _, _, name in deep.prompts)


def test_report_prompt_explicitly_anchors_ticket_to_reply_target():
    source = __import__("app.agent.report_intent", fromlist=["REPORT_INTENT_SYSTEM_PROMPT"])
    prompt = source.REPORT_INTENT_SYSTEM_PROMPT
    assert "админы проверьте это" in prompt
    assert "REPLIED-TO MESSAGE" in prompt


def test_banter_match_prompt_has_reply_and_extended_context():
    from app.community_policy.prompts import match_prompt

    context = boundary_context()
    rule = CommunityPolicyRule(
        rule_id="R1",
        source="shadow_feedback",
        source_ref=33,
        title="Friendly banter",
        condition="Mutual voluntary banter with no request to stop.",
        action="allow",
        policy_family="harassment",
    )
    payload = match_prompt(context=context, rules=[rule])
    assert "reply_target" in payload
    assert "всё, хорош уже, реально заебал" in payload
    assert "relationship_signals" in payload
