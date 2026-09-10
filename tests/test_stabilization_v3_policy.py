from datetime import datetime, timezone

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    ModerationHistoryItem,
)
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def _context(history=None):
    text = "targeted abuse"
    snap = MessageSnapshot(
        db_id=1,
        telegram_message_id=1,
        chat_id=-1001,
        user_id=42,
        username="@u",
        full_name="U",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=snap,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],
        recent_user_messages=[],
        user_moderation_history=history or [],
    )


def _harassment():
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="harassment",
        severity="medium",
        confidence=.95,
        action="warn",
        delete_message=False,
        needs_human_review=False,
        reason="Clear targeted degradation.",
        current_message_evidence=["Direct targeted degradation"],
        context_evidence=[],
    )


def test_first_clear_harassment_warns_without_delete():
    policy = PolicyGate().evaluate(_harassment(), context=_context())
    assert policy.final_action == "warn"
    assert policy.final_delete_message is False


def test_repeat_harassment_goes_to_ticket_not_mute_or_ban():
    history = [ModerationHistoryItem(
        event_key="H1",
        action="warn",
        category="harassment",
        severity="medium",
        confidence=.95,
        reason="Prior warning",
        autonomous=True,
        reversed=False,
        created_at=datetime.now(timezone.utc),
    )]
    policy = PolicyGate().evaluate(_harassment(), context=_context(history))
    assert policy.final_action == "escalate"
    assert policy.requires_human_review is True
