from datetime import datetime, timezone
from pathlib import Path

from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    ModerationHistoryItem,
)
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def history(action: str) -> ModerationHistoryItem:
    return ModerationHistoryItem(
        event_key=f"h-{action}",
        action=action,
        category="harassment",
        severity="high",
        confidence=0.97,
        reason="Confirmed harassment",
        autonomous=True,
        reversed=False,
        created_at=datetime.now(timezone.utc),
    )


def context(user_id: int, events=None) -> MessageContext:
    text = "clear targeted aggression"
    current = MessageSnapshot(
        db_id=user_id,
        telegram_message_id=user_id,
        chat_id=-10042,
        user_id=user_id,
        username=f"@u{user_id}",
        full_name=f"User {user_id}",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=current,
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],
        recent_user_messages=[],
        user_moderation_history=list(events or []),
    )


def clear_aggression() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="harassment",
        severity="high",
        confidence=0.97,
        action="escalate",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=True,
        conflict_context="clear_current_aggressor",
        reason="The current author directly targets another participant.",
        current_message_evidence=["Direct targeted degradation in the current message."],
        context_evidence=[],
    )


def test_two_participants_each_get_own_first_clear_warning():
    gate = PolicyGate()
    assert gate.evaluate(clear_aggression(), context=context(101)).final_action == "warn"
    assert gate.evaluate(clear_aggression(), context=context(202)).final_action == "warn"


def test_one_users_warning_does_not_count_for_another_user():
    gate = PolicyGate()
    repeat = gate.evaluate(clear_aggression(), context=context(101, [history("warn")]))
    other = gate.evaluate(clear_aggression(), context=context(202))
    assert repeat.final_action == "escalate"
    assert repeat.requires_human_review is True
    assert other.final_action == "warn"


def test_genuinely_ambiguous_multi_party_message_still_goes_to_ticket():
    decision = clear_aggression().model_copy(
        update={"conflict_context": "ambiguous_multi_party"}
    )
    result = PolicyGate().evaluate(decision, context=context(303))
    assert result.final_action == "escalate"
    assert result.requires_human_review is True


def test_prompt_requires_per_participant_chat_scoped_handling():
    source = Path("app/agent/prompts.py").read_text(encoding="utf-8")
    assert "conflict_context=clear_current_aggressor" in source
    assert "conflict_context=ambiguous_multi_party" in source
    assert "each participant in a mutual fight is evaluated independently" in source


def test_member_handler_marks_left_or_kicked_chat_unavailable():
    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert "ChatMemberStatus.LEFT" in source
    assert "ChatMemberStatus.KICKED" in source
    assert "mark_chat_unavailable" in source
