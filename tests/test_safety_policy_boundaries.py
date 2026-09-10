from datetime import datetime, timezone
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def context(text="message"):
    snap=MessageSnapshot(db_id=1, telegram_message_id=1, chat_id=-100123, user_id=10,
        username="@u", full_name="U", content_type="text", raw_text=text,
        normalized_text=text, telegram_date=datetime.now(timezone.utc))
    return MessageContext(current_message=snap, text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(), recent_chat_messages=[], recent_user_messages=[],
        user_moderation_history=[])


def decision(category, action="delete", confidence=.95, severity="medium", review=False):
    return ModerationDecision(detected_language="English", current_message_violation=True,
        category=category, severity=severity, confidence=confidence,
        action=("escalate" if review else action), delete_message=action in {"delete","mute","ban"},
        mute_minutes=None, needs_human_review=review, reason="Current-message violation.",
        current_message_evidence=["Current-message evidence"], context_evidence=[])


def test_hard_phishing_high_confidence_is_immediate_ban():
    p=PolicyGate().evaluate(decision("phishing", action="ban", severity="high"), context=context())
    assert p.final_action == "ban" and p.final_delete_message is True


def test_clear_first_harassment_warns_without_delete():
    p=PolicyGate().evaluate(decision("harassment", confidence=.95, severity="high"), context=context())
    assert p.final_action == "warn" and p.final_delete_message is False


def test_ambiguous_harassment_goes_to_review():
    p=PolicyGate().evaluate(decision("harassment", confidence=.90, severity="high", review=True), context=context())
    assert p.final_action == "escalate" and p.requires_human_review is True


def test_clear_unsolicited_ad_is_medium_mute_delete():
    p=PolicyGate().evaluate(decision("unsolicited_advertising", action="mute", confidence=.95), context=context())
    assert p.final_action == "mute" and p.final_delete_message is True
