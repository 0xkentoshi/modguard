from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    PolicyEvaluation,
)
from app.feedback.service import (
    ModeratorFeedbackService,
)
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeRepository:
    def __init__(self, examples):
        self.examples = examples

    async def recent_moderator_feedback(
        self,
        *,
        chat_id,
        limit,
    ):
        return list(self.examples)


class FakeProvider:
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


def context(text="Есть тема с профитом, напишу в лс"):
    snap = MessageSnapshot(
        db_id=1,
        telegram_message_id=1,
        chat_id=-100123,
        user_id=10,
        username="@u",
        full_name="U",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=datetime.now(
            timezone.utc
        ),
    )

    return MessageContext(
        current_message=snap,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],
        recent_user_messages=[],
        user_moderation_history=[],
    )


def escalate_decision():
    return ModerationDecision(
        detected_language="Russian",
        current_message_violation=True,
        category="other",
        severity="medium",
        confidence=0.62,
        action="escalate",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=True,
        reason="Ambiguous promotion.",
        current_message_evidence=[
            "Vague profit solicitation"
        ],
        context_evidence=[],
    )


def escalate_policy():
    return PolicyEvaluation(
        original_action="escalate",
        final_action="escalate",
        final_delete_message=False,
        final_mute_minutes=None,
        autonomous=False,
        requires_human_review=True,
        policy_reason="Human review required.",
    )


@pytest.mark.asyncio
async def test_clear_core_action_never_calls_feedback_llm():
    provider = FakeProvider(
        {
            "relevant": True,
            "confidence": 1.0,
            "matched_feedback_ids": [1],
            "recommended_action": "allow",
            "category": "safe",
            "severity": "none",
            "current_message_violation": False,
            "reason": "Allow.",
            "current_message_evidence": [],
        }
    )

    service = ModeratorFeedbackService(
        repository=FakeRepository(
            examples=[
                SimpleNamespace(
                    id=1
                )
            ]
        ),
        deep_provider=provider,
    )

    decision = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="scam",
        severity="high",
        confidence=0.99,
        action="delete",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Obvious scam.",
        current_message_evidence=[
            "Fake reward lure"
        ],
        context_evidence=[],
    )

    policy = PolicyEvaluation(
        original_action="delete",
        final_action="delete",
        final_delete_message=True,
        final_mute_minutes=None,
        autonomous=True,
        requires_human_review=False,
        policy_reason="Delete threshold met.",
    )

    new_decision, new_policy = (
        await service.refine_gray_case(
            context=context(),
            baseline_decision=decision,
            baseline_policy=policy,
            policy_gate=PolicyGate(),
        )
    )

    assert provider.calls == 0
    assert new_decision == decision
    assert new_policy == policy


@pytest.mark.asyncio
async def test_similar_human_allow_can_resolve_gray_ticket():
    example = SimpleNamespace(
        id=7,
        message_text=(
            "Есть возможность, расскажу подробнее в личке"
        ),
        ai_category="other",
        ai_reason="Ambiguous solicitation.",
        moderator_action="allow",
        policy_version=None,
    )

    provider = FakeProvider(
        {
            "relevant": True,
            "confidence": 0.96,
            "matched_feedback_ids": [7],
            "recommended_action": "allow",
            "category": "safe",
            "severity": "none",
            "current_message_violation": False,
            "reason": (
                "Same benign local-community pattern "
                "previously allowed by moderator."
            ),
            "current_message_evidence": [],
        }
    )

    service = ModeratorFeedbackService(
        repository=FakeRepository(
            examples=[example]
        ),
        deep_provider=provider,
    )

    decision, policy = await service.refine_gray_case(
        context=context(),
        baseline_decision=escalate_decision(),
        baseline_policy=escalate_policy(),
        policy_gate=PolicyGate(),
    )

    assert provider.calls == 1
    assert decision.action == "allow"
    assert policy.final_action == "allow"
    assert "Moderator feedback memory" in decision.reason


@pytest.mark.asyncio
async def test_one_old_ban_example_cannot_create_autonomous_ban():
    example = SimpleNamespace(
        id=8,
        message_text="Suspicious local ad",
        ai_category="other",
        ai_reason="Ambiguous.",
        moderator_action="ban",
        policy_version=None,
    )

    provider = FakeProvider(
        {
            "relevant": True,
            "confidence": 0.99,
            "matched_feedback_ids": [8],
            "recommended_action": "ban",
            "category": "other",
            "severity": "high",
            "current_message_violation": True,
            "reason": "Similar to banned precedent.",
            "current_message_evidence": [
                "Current message repeats same solicitation pattern"
            ],
        }
    )

    service = ModeratorFeedbackService(
        repository=FakeRepository(
            examples=[example]
        ),
        deep_provider=provider,
    )

    decision, policy = await service.refine_gray_case(
        context=context(),
        baseline_decision=escalate_decision(),
        baseline_policy=escalate_policy(),
        policy_gate=PolicyGate(),
    )

    assert decision.action == "escalate"
    assert policy.final_action == "escalate"


@pytest.mark.asyncio
async def test_feedback_allow_cannot_weaken_uncertain_phishing():
    example = SimpleNamespace(
        id=9,
        message_text="Connect wallet",
        ai_category="phishing",
        ai_reason="Old case.",
        moderator_action="allow",
        policy_version=None,
    )

    provider = FakeProvider(
        {
            "relevant": True,
            "confidence": 0.99,
            "matched_feedback_ids": [9],
            "recommended_action": "allow",
            "category": "safe",
            "severity": "none",
            "current_message_violation": False,
            "reason": "Historical allow.",
            "current_message_evidence": [],
        }
    )

    service = ModeratorFeedbackService(
        repository=FakeRepository(
            examples=[example]
        ),
        deep_provider=provider,
    )

    baseline = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="phishing",
        severity="high",
        confidence=0.74,
        action="escalate",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=True,
        reason="Possible credential theft.",
        current_message_evidence=[
            "Wallet connection request"
        ],
        context_evidence=[],
    )

    decision, policy = await service.refine_gray_case(
        context=context(
            "Connect your wallet to verify"
        ),
        baseline_decision=baseline,
        baseline_policy=escalate_policy(),
        policy_gate=PolicyGate(),
    )

    assert decision == baseline
    assert policy.final_action == "escalate"
