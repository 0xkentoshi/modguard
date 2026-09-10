from datetime import datetime, timezone

from app.agent.prompts import build_moderation_prompt
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
)
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def make_context(
    current_text: str,
    *,
    repeated: int = 0,
    messages_last_60s: int = 1,
) -> MessageContext:
    now = datetime.now(timezone.utc)

    current = MessageSnapshot(
        db_id=1,
        telegram_message_id=10,
        chat_id=-100123,
        user_id=777,
        username="@frolllllll",
        full_name="Tester",
        content_type="text",
        raw_text=current_text,
        normalized_text=current_text,
        telegram_date=now,
    )

    old = MessageSnapshot(
        db_id=2,
        telegram_message_id=9,
        chat_id=-100123,
        user_id=777,
        username="@frolllllll",
        full_name="Tester",
        content_type="text",
        raw_text="FREE USDT https://example.com",
        normalized_text="FREE USDT https://example.com",
        telegram_date=now,
    )

    return MessageContext(
        current_message=current,
        text_signals=analyze_text(
            current_text
        ),
        behavior_signals=BehaviorSignals(
            repeated_recent_messages=repeated,
            messages_last_60s=messages_last_60s,
        ),
        recent_chat_messages=[old],
        recent_user_messages=[old],
    )


def make_decision(
    *,
    violation: bool,
    category: str,
    severity: str,
    confidence: float,
    action: str,
    current_evidence=None,
    context_evidence=None,
):
    return ModerationDecision(
        detected_language="Russian",
        current_message_violation=violation,
        category=category,
        severity=severity,
        confidence=confidence,
        action=action,
        delete_message=action in {
            "delete",
            "mute",
            "ban",
        },
        mute_minutes=None,
        needs_human_review=False,
        reason="Test.",
        current_message_evidence=(
            current_evidence
            or []
        ),
        context_evidence=(
            context_evidence
            or []
        ),
    )


def test_harmless_current_message_cannot_be_banned_for_history():
    context = make_context(
        "Кто сегодня тестировал новую версию Ollama?"
    )

    decision = make_decision(
        violation=False,
        category="spam",
        severity="high",
        confidence=0.99,
        action="ban",
        context_evidence=[
            "User previously posted spam"
        ],
    )

    result = PolicyGate().evaluate(
        decision,
        context=context,
    )

    assert result.final_action == "allow"


def test_scam_warning_message_cannot_be_punished_for_old_scam():
    context = make_context(
        "Ребят, сообщение выше похоже на скам, не переходите по ссылке"
    )

    decision = make_decision(
        violation=False,
        category="spam",
        severity="high",
        confidence=0.98,
        action="ban",
        context_evidence=[
            "Previous message contained a scam link"
        ],
    )

    result = PolicyGate().evaluate(
        decision,
        context=context,
    )

    assert result.final_action == "allow"


def test_destructive_action_without_current_evidence_is_blocked():
    context = make_context(
        "реально????",
        repeated=0,
        messages_last_60s=3,
    )

    decision = make_decision(
        violation=True,
        category="scam",
        severity="high",
        confidence=0.98,
        action="ban",
        current_evidence=[],
        context_evidence=[
            "Old scam activity"
        ],
    )

    result = PolicyGate().evaluate(
        decision,
        context=context,
    )

    assert result.final_action == "escalate"


def test_objective_repeated_spam_can_still_be_moderated():
    """
    Objective repetition is enough to let the current spam be moderated even
    without textual current_message_evidence.

    It is NOT by itself a confirmed prior offense for the progressive ladder.
    With no prior moderation history, ordinary spam starts at WARN without deletion.
    """

    context = make_context(
        "JOIN https://example.com FREE MONEY",
        repeated=2,
        messages_last_60s=4,
    )

    decision = make_decision(
        violation=True,
        category="spam",
        severity="high",
        confidence=0.98,
        action="ban",
        current_evidence=[],
        context_evidence=[
            "Current message repeats recent identical spam"
        ],
    )

    result = PolicyGate().evaluate(
        decision,
        context=context,
    )

    assert result.final_action == "warn"
    assert result.final_delete_message is False
    assert result.autonomous is True
    assert (
        "light ladder"
        in result.policy_reason.lower()
    )


def test_prompt_does_not_expose_username_or_user_id():
    context = make_context(
        "плюс вайб"
    )

    prompt = build_moderation_prompt(
        context
    )

    assert "@frolllllll" not in prompt
    assert '"user_id"' not in prompt
    assert '"username"' not in prompt


def test_prompt_keeps_current_message_and_history_separate():
    context = make_context(
        "плюс вайб"
    )

    prompt = build_moderation_prompt(
        context
    )

    assert '"text":"плюс вайб"' in prompt
    assert (
        "FREE USDT https://example.com"
        in prompt
    )
    assert '"current_message":' in prompt
    assert '"conversation_context":' in prompt
