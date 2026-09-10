from datetime import datetime, timezone

from app.agent.schemas import (
    BehaviorSignals, MessageContext, MessageSnapshot,
    ModerationDecision, ModerationHistoryItem,
)
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def history_item(action: str, category: str):
    return ModerationHistoryItem(
        event_key=f"T-{category}-{action}", action=action, category=category,
        severity="medium", confidence=.96, reason=f"Confirmed {category}",
        autonomous=True, reversed=False, created_at=datetime.now(timezone.utc),
    )


def context(history=None):
    text="test"
    snap=MessageSnapshot(
        db_id=1, telegram_message_id=1, chat_id=-1001, user_id=7,
        username="@u", full_name="U", content_type="text", raw_text=text,
        normalized_text=text, telegram_date=datetime.now(timezone.utc),
    )
    return MessageContext(
        current_message=snap, text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(), recent_chat_messages=[],
        recent_user_messages=[], user_moderation_history=history or [],
    )


def decision(category, *, severity="medium", confidence=.96, action="warn", review=False):
    return ModerationDecision(
        detected_language="English", current_message_violation=True,
        category=category, severity=severity, confidence=confidence,
        action=("escalate" if review else action), delete_message=False,
        mute_minutes=None, needs_human_review=review, reason="Clear violation.",
        current_message_evidence=["Current-message evidence"], context_evidence=[],
    )


def test_spam_light_ladder_warn_mute_ban():
    gate=PolicyGate()
    first=gate.evaluate(decision("spam"), context=context())
    assert first.final_action == "warn"
    assert first.final_delete_message is False

    repeat=gate.evaluate(
        decision("spam"), context=context([history_item("warn","spam")])
    )
    assert repeat.final_action == "mute"
    assert repeat.final_delete_message is True

    after_mute=gate.evaluate(
        decision("spam"), context=context([history_item("mute","spam")])
    )
    assert after_mute.final_action == "ban"
    assert after_mute.final_delete_message is True


def test_harassment_clear_first_warns_but_ambiguous_goes_review():
    gate=PolicyGate()
    clear=gate.evaluate(
        decision("harassment", severity="high", action="delete"), context=context()
    )
    assert clear.final_action == "warn"
    assert clear.final_delete_message is False

    ambiguous=gate.evaluate(
        decision("harassment", severity="high", action="delete", review=True),
        context=context(),
    )
    assert ambiguous.final_action == "escalate"
    assert ambiguous.requires_human_review is True


def test_harassment_repeat_goes_to_human_review_not_auto_mute_ban():
    gate=PolicyGate()
    repeat=gate.evaluate(
        decision("harassment", severity="high", action="delete"),
        context=context([history_item("warn","harassment")]),
    )
    assert repeat.final_action == "escalate"
    assert repeat.requires_human_review is True

    after_old_mute=gate.evaluate(
        decision("harassment", severity="high", action="delete"),
        context=context([history_item("mute","harassment")]),
    )
    assert after_old_mute.final_action == "escalate"
    assert after_old_mute.requires_human_review is True


def test_medium_unsolicited_advertising_starts_at_mute():
    gate=PolicyGate()
    result=gate.evaluate(
        decision("unsolicited_advertising", action="mute"), context=context()
    )
    assert result.final_action == "mute"
    assert result.final_delete_message is True


def test_heavy_scam_and_phishing_start_at_ban():
    gate=PolicyGate()
    for category in ("scam","phishing","malicious_link"):
        result=gate.evaluate(
            decision(category, severity="high", confidence=.97, action="ban"),
            context=context(),
        )
        assert result.final_action == "ban"
        assert result.final_delete_message is True
