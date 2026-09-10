from app.agent.schemas import ModerationDecision
from app.moderation.policy import PolicyGate


def make_decision(
    *, category="safe", severity="none", confidence=.95,
    action="allow", review=False,
):
    violation = action != "allow"
    return ModerationDecision(
        detected_language="English",
        current_message_violation=violation,
        category=category,
        severity=severity,
        confidence=confidence,
        action=action,
        delete_message=action in {"delete","mute","ban"},
        mute_minutes=None,
        needs_human_review=review,
        reason="Test decision",
        current_message_evidence=(
            ["Current message contains test violation evidence"] if violation else []
        ),
        context_evidence=[],
    )


def test_obvious_scam_can_be_autonomously_banned():
    result=PolicyGate().evaluate(make_decision(
        category="scam",severity="critical",confidence=.98,action="ban"
    ))
    assert result.final_action=="ban"
    assert result.autonomous is True
    assert result.final_delete_message is True


def test_first_clear_harassment_ban_request_enters_light_warning_ladder():
    result=PolicyGate().evaluate(make_decision(
        category="harassment",severity="high",confidence=.96,action="ban"
    ))
    assert result.final_action=="warn"
    assert result.autonomous is True
    assert result.final_delete_message is False


def test_uncertain_ban_is_escalated():
    result=PolicyGate().evaluate(make_decision(
        category="scam",severity="high",confidence=.60,action="ban"
    ))
    assert result.final_action=="escalate"
    assert result.requires_human_review is True


def test_explicit_review_is_respected():
    result=PolicyGate().evaluate(make_decision(
        category="harassment",severity="medium",confidence=.75,action="delete",review=True
    ))
    assert result.final_action=="escalate"


def test_safe_message_is_allowed():
    assert PolicyGate().evaluate(make_decision()).final_action=="allow"


def test_safe_current_message_blocks_destructive_llm_action():
    decision=ModerationDecision(
        detected_language="Russian",current_message_violation=False,category="spam",
        severity="high",confidence=.99,action="ban",delete_message=True,mute_minutes=None,
        needs_human_review=False,reason="Bad historical context.",current_message_evidence=[],
        context_evidence=["Previous spam"],
    )
    assert PolicyGate().evaluate(decision).final_action=="allow"
