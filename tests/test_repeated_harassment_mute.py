from datetime import datetime, timezone
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision, ModerationHistoryItem
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def hist(action):
    return ModerationHistoryItem(
        event_key=f"H-{action}", action=action, category="harassment", severity="medium",
        confidence=.96, reason="Confirmed harassment", autonomous=True, reversed=False,
        created_at=datetime.now(timezone.utc),
    )


def ctx(history):
    text="targeted severe abuse"
    snap=MessageSnapshot(
        db_id=1, telegram_message_id=1, chat_id=-1, user_id=5, username="@u",
        full_name="U", content_type="text", raw_text=text, normalized_text=text,
        telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=snap, text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(), recent_chat_messages=[], recent_user_messages=[],
        user_moderation_history=history,
    )


def dec(review=False):
    return ModerationDecision(
        detected_language="English", current_message_violation=True, category="harassment",
        severity="high", confidence=.97, action=("escalate" if review else "warn"),
        delete_message=False, mute_minutes=None, needs_human_review=review,
        reason="Severe targeted harassment.", current_message_evidence=["Direct targeted abuse"],
        context_evidence=[],
    )


def test_first_clear_high_harassment_warns():
    p=PolicyGate().evaluate(dec(), context=ctx([]))
    assert p.final_action=="warn"
    assert not p.final_delete_message


def test_ambiguous_fight_goes_to_review():
    p=PolicyGate().evaluate(dec(True), context=ctx([]))
    assert p.final_action=="escalate"


def test_repeated_harassment_after_warning_goes_to_ticket():
    p=PolicyGate().evaluate(dec(), context=ctx([hist("warn")]))
    assert p.final_action=="escalate"
    assert p.requires_human_review is True


def test_harassment_after_old_mute_still_goes_to_ticket_by_default():
    p=PolicyGate().evaluate(dec(), context=ctx([hist("mute")]))
    assert p.final_action=="escalate"
    assert p.requires_human_review is True
