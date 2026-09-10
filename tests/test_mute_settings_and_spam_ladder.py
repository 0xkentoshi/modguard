from datetime import datetime, timezone
import pytest
from app.admin import control_models  # noqa
from app.admin.control_repository import ControlRepository
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision, ModerationHistoryItem
from app.database.db import Database
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text

@pytest.mark.asyncio
async def test_mute_duration_defaults_to_one_hour_and_is_chat_scoped(tmp_path):
    db=Database("sqlite+aiosqlite:///" + (tmp_path/"mute.db").as_posix()); await db.init()
    repo=ControlRepository(db.session_factory)
    assert await repo.get_mute_duration_minutes(-1) == 60
    await repo.set_mute_duration_minutes(-1,180)
    assert await repo.get_mute_duration_minutes(-1) == 180
    assert await repo.get_mute_duration_minutes(-2) == 60
    await db.dispose()

def hist(action):
    return ModerationHistoryItem(event_key=f"T-{action}", action=action, category="spam", severity="medium",
        confidence=.96, reason="Confirmed spam", autonomous=True, reversed=False,
        created_at=datetime.now(timezone.utc))

def ctx(history):
    text="buy this now"; snap=MessageSnapshot(db_id=1, telegram_message_id=1, chat_id=-1,user_id=7,
        username="@u",full_name="U",content_type="text",raw_text=text,normalized_text=text,
        telegram_date=datetime.now(timezone.utc))
    return MessageContext(current_message=snap,text_signals=analyze_text(text),behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],recent_user_messages=[],user_moderation_history=history)

def dec(conf=.96):
    return ModerationDecision(detected_language="English",current_message_violation=True,category="spam",
        severity="medium",confidence=conf,action="warn",delete_message=False,mute_minutes=None,
        needs_human_review=False,reason="Clear ordinary spam.",current_message_evidence=["spam"],context_evidence=[])

def test_first_clear_spam_warns_without_delete():
    p=PolicyGate().evaluate(dec(),context=ctx([])); assert p.final_action=="warn"; assert not p.final_delete_message

def test_repeat_spam_after_warn_becomes_mute_delete():
    p=PolicyGate().evaluate(dec(),context=ctx([hist("warn")])); assert p.final_action=="mute"; assert p.final_delete_message

def test_spam_after_confirmed_mute_becomes_ban_delete():
    p=PolicyGate().evaluate(dec(),context=ctx([hist("mute")])); assert p.final_action=="ban"; assert p.final_delete_message

def test_uncertain_spam_does_not_enter_ladder():
    p=PolicyGate().evaluate(dec(.72),context=ctx([])); assert p.final_action in {"warn","allow"}
