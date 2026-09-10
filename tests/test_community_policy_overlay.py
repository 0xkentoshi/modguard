from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
)
from app.community_policy.schemas import (
    CommunityPolicyMatch,
    CommunityPolicyTriage,
)
from app.community_policy.service import (
    CommunityPolicyService,
    rules_json,
)
from app.community_policy.schemas import CommunityPolicyRule
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


class FakeRepository:
    def __init__(self, active=None):
        self.active = active

    async def get_active_community_policy(self, chat_id):
        return self.active


class FakeProvider:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = 0

    async def generate_structured(
        self,
        *,
        system_prompt,
        user_prompt,
        response_model,
    ):
        self.calls += 1
        if self.error is not None:
            raise self.error

        payload = self.responses[response_model.__name__]
        return response_model.model_validate(payload)


def make_context(text: str) -> MessageContext:
    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=10,
            chat_id=-100123,
            user_id=777,
            username="@tester",
            full_name="Tester",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=datetime.now(timezone.utc),
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
    )


def safe_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="Russian",
        current_message_violation=False,
        category="safe",
        severity="none",
        confidence=0.99,
        action="allow",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=False,
        reason="Core says safe.",
        current_message_evidence=[],
        context_evidence=[],
    )


def scam_ban_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="scam",
        severity="critical",
        confidence=0.99,
        action="ban",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Obvious scam.",
        current_message_evidence=["Fake reward and wallet bait"],
        context_evidence=[],
    )


@pytest.mark.asyncio
async def test_custom_rule_can_strengthen_core_allow_to_delete():
    rules = [
        CommunityPolicyRule(
            rule_id="R1",
            title="Любой мат",
            condition="Сообщение содержит ненормативную лексику.",
            action="delete",
        )
    ]

    repository = FakeRepository(
        SimpleNamespace(
            version=3,
            rules_json=rules_json(rules),
        )
    )

    fast = FakeProvider(
        {
            "CommunityPolicyTriage": {
                "route": "deep",
                "confidence": 0.95,
                "reason": "May match profanity rule.",
            }
        }
    )

    deep = FakeProvider(
        {
            "CommunityPolicyMatch": {
                "matched": True,
                "confidence": 0.96,
                "winning_rule_id": "R1",
                "matched_rule_ids": ["R1"],
                "ambiguous": False,
                "reason": "Current message semantically contains profanity.",
                "current_message_evidence": ["Profanity is present in the current message"],
            }
        }
    )

    service = CommunityPolicyService(
        repository=repository,
        deep_provider=deep,
        fast_provider=fast,
    )

    context = make_context("ну это пиздец")
    baseline_decision = safe_decision()
    gate = PolicyGate()
    baseline_policy = gate.evaluate(
        baseline_decision,
        context=context,
    )

    decision, policy = await service.apply_overlay(
        context=context,
        baseline_decision=baseline_decision,
        baseline_policy=baseline_policy,
        policy_gate=gate,
    )

    assert policy.final_action == "delete"
    assert policy.source == "community_policy"
    assert policy.community_policy_version == 3
    assert policy.matched_community_rules == ["R1"]
    assert decision.current_message_violation is True
    assert decision.category == "other"


@pytest.mark.asyncio
async def test_custom_policy_never_weakens_core_ban():
    rules = [
        CommunityPolicyRule(
            rule_id="R1",
            title="Разрешить ссылки",
            condition="Любые ссылки разрешены.",
            action="allow",
        )
    ]

    repository = FakeRepository(
        SimpleNamespace(
            version=1,
            rules_json=rules_json(rules),
        )
    )

    fast = FakeProvider()
    deep = FakeProvider()

    service = CommunityPolicyService(
        repository=repository,
        deep_provider=deep,
        fast_provider=fast,
    )

    context = make_context(
        "FREE 500 USDT connect wallet https://evil.example"
    )
    baseline_decision = scam_ban_decision()
    gate = PolicyGate()
    baseline_policy = gate.evaluate(
        baseline_decision,
        context=context,
    )

    decision, policy = await service.apply_overlay(
        context=context,
        baseline_decision=baseline_decision,
        baseline_policy=baseline_policy,
        policy_gate=gate,
    )

    assert policy.final_action == "ban"
    assert policy.source == "core"
    assert decision.category == "scam"
    assert fast.calls == 0
    assert deep.calls == 0


@pytest.mark.asyncio
async def test_overlay_failure_preserves_stable_core_result():
    rules = [
        CommunityPolicyRule(
            rule_id="R1",
            title="Extra rule",
            condition="Some extra semantic rule.",
            action="delete",
        )
    ]

    repository = FakeRepository(
        SimpleNamespace(
            version=1,
            rules_json=rules_json(rules),
        )
    )

    fast = FakeProvider(error=RuntimeError("fast failed"))
    deep = FakeProvider(error=RuntimeError("deep failed"))

    service = CommunityPolicyService(
        repository=repository,
        deep_provider=deep,
        fast_provider=fast,
    )

    context = make_context("ordinary message")
    baseline_decision = safe_decision()
    gate = PolicyGate()
    baseline_policy = gate.evaluate(
        baseline_decision,
        context=context,
    )

    decision, policy = await service.apply_overlay(
        context=context,
        baseline_decision=baseline_decision,
        baseline_policy=baseline_policy,
        policy_gate=gate,
    )

    assert decision is baseline_decision
    assert policy is baseline_policy
    assert policy.final_action == "allow"
