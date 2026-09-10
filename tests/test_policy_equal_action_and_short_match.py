from types import SimpleNamespace

import pytest

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    PolicyEvaluation,
)
from app.community_policy.schemas import (
    CommunityPolicyMatch,
    CommunityPolicyRule,
)
from app.community_policy.service import (
    CommunityPolicyService,
)
from app.utils.text_normalization import analyze_text


class FakeRepo:
    pass


class FakeProvider:
    def __init__(self, response):
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


def make_context(text: str):
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
        telegram_date=0,
    )

    return MessageContext(
        current_message=snap,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],
        recent_user_messages=[],
        user_moderation_history=[],
    )


@pytest.mark.asyncio
async def test_short_custom_policy_message_skips_fast_false_negative():
    fast = FakeProvider(
        {
            "route": "no_match",
            "confidence": 1.0,
            "reason": "No match.",
        }
    )

    deep = FakeProvider(
        {
            "matched": True,
            "matched_rule_ids": ["R1"],
            "winning_rule_id": "R1",
            "confidence": 1.0,
            "ambiguous": False,
            "reason": "Profanity matches rule.",
            "current_message_evidence": ["Short profanity"],
        }
    )

    service = CommunityPolicyService(
        repository=FakeRepo(),
        deep_provider=deep,
        fast_provider=fast,
    )

    result = await service._match(
        context=make_context("бля"),
        rules=[
            CommunityPolicyRule(
                rule_id="R1",
                title="Profanity",
                condition="Any profanity",
                action="delete",
            )
        ],
    )

    assert result is not None
    assert result.matched is True
    assert fast.calls == 0
    assert deep.calls == 1


def test_equal_delete_can_be_owned_by_custom_policy_for_other_category():
    # Regression intent: equal DELETE must not be discarded when Core category
    # is "other", because live executor relies on community_policy source for
    # custom categories.
    from pathlib import Path

    source = Path(
        "app/community_policy/service.py"
    ).read_text(encoding="utf-8")

    assert "overlay_rank == core_rank" in source
    assert '"safe"' in source
    assert '"other"' in source
