from datetime import datetime, timedelta, timezone

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
    ModerationHistoryItem,
)
from app.database.db import Database
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text


def history(action: str, category: str, *, age_hours: float) -> ModerationHistoryItem:
    return ModerationHistoryItem(
        event_key=f"T-{category}-{action}-{age_hours}",
        action=action,
        category=category,
        severity="medium",
        confidence=0.97,
        reason="confirmed",
        autonomous=True,
        reversed=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )


def context(items, *, decay_hours: int = 6) -> MessageContext:
    text = "test message"
    snap = MessageSnapshot(
        db_id=1,
        telegram_message_id=1,
        chat_id=-1001,
        user_id=7,
        username="@user",
        full_name="User",
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
        user_moderation_history=items,
        light_offense_decay_hours=decay_hours,
    )


def spam_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="spam",
        severity="medium",
        confidence=0.97,
        action="warn",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=False,
        reason="clear spam",
        current_message_evidence=["spam"],
        context_evidence=[],
    )


def harassment_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="harassment",
        severity="medium",
        confidence=0.95,
        action="warn",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=False,
        conflict_context="clear_current_aggressor",
        reason="clear targeted harassment",
        current_message_evidence=["targeted insult"],
        context_evidence=[],
    )


def medium_decision() -> ModerationDecision:
    return ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="unsolicited_advertising",
        severity="medium",
        confidence=0.97,
        action="mute",
        delete_message=True,
        mute_minutes=60,
        needs_human_review=False,
        reason="intrusive advertising",
        current_message_evidence=["unsolicited solicitation"],
        context_evidence=[],
    )


def test_recent_light_spam_warning_still_escalates():
    result = PolicyGate().evaluate(
        spam_decision(),
        context=context([history("warn", "spam", age_hours=2)], decay_hours=6),
    )
    assert result.final_action == "mute"
    assert result.final_delete_message is True


def test_expired_light_spam_warning_resets_to_warning():
    result = PolicyGate().evaluate(
        spam_decision(),
        context=context([history("warn", "spam", age_hours=7)], decay_hours=6),
    )
    assert result.final_action == "warn"
    assert result.final_delete_message is False


def test_expired_light_mute_does_not_cause_surprise_ban_days_later():
    result = PolicyGate().evaluate(
        spam_decision(),
        context=context([history("mute", "spam", age_hours=24)], decay_hours=6),
    )
    assert result.final_action == "warn"


def test_harassment_warning_expires_and_new_incident_warns_again():
    result = PolicyGate().evaluate(
        harassment_decision(),
        context=context([history("warn", "harassment", age_hours=8)], decay_hours=6),
    )
    assert result.final_action == "warn"
    assert result.requires_human_review is False


def test_recent_harassment_history_still_routes_repeat_to_review():
    result = PolicyGate().evaluate(
        harassment_decision(),
        context=context([history("warn", "harassment", age_hours=2)], decay_hours=6),
    )
    assert result.final_action == "escalate"
    assert result.requires_human_review is True


def test_zero_decay_setting_means_light_history_never_expires():
    result = PolicyGate().evaluate(
        spam_decision(),
        context=context([history("warn", "spam", age_hours=500)], decay_hours=0),
    )
    assert result.final_action == "mute"


def test_medium_history_does_not_decay_with_light_window():
    result = PolicyGate().evaluate(
        medium_decision(),
        context=context(
            [history("mute", "unsolicited_advertising", age_hours=72)],
            decay_hours=1,
        ),
    )
    assert result.final_action == "ban"


@pytest.mark.asyncio
async def test_light_memory_setting_is_per_chat_and_defaults_to_six_hours(tmp_path):
    db = Database("sqlite+aiosqlite:///" + (tmp_path / "decay.db").as_posix())
    await db.init()
    repo = ControlRepository(db.session_factory)

    assert await repo.get_light_offense_decay_hours(-1) == 6
    await repo.set_light_offense_decay_hours(-1, 12)
    assert await repo.get_light_offense_decay_hours(-1) == 12
    assert await repo.get_light_offense_decay_hours(-2) == 6
    await repo.set_light_offense_decay_hours(-2, 0)
    assert await repo.get_light_offense_decay_hours(-2) == 0

    with pytest.raises(ValueError):
        await repo.set_light_offense_decay_hours(-1, 5)

    await db.dispose()

from app.moderation.reputation import apply_light_reputation_decay


def test_decay_is_applied_before_ai_and_keeps_medium_heavy_history():
    base = context([
        history("warn", "spam", age_hours=10),
        history("mute", "unsolicited_advertising", age_hours=72),
        history("warn", "harassment", age_hours=1),
    ], decay_hours=6)
    decayed = apply_light_reputation_decay(
        base,
        hours=6,
        now=datetime.now(timezone.utc),
    )
    categories = [item.category for item in decayed.user_moderation_history]
    assert "spam" not in categories
    assert "unsolicited_advertising" in categories
    assert "harassment" in categories
    assert decayed.light_offense_decay_hours == 6
