import json
from datetime import datetime, timezone
import pytest
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot, ModerationDecision, ModerationHistoryItem
from app.database.db import Database
from app.database.repository import AuditRepository
from app.moderation.executor import ModerationExecutor
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text

class FakeBot:
    def __init__(self): self.deleted=[]; self.sent=[]; self.banned=[]; self.restricted=[]
    async def delete_message(self,*,chat_id,message_id): self.deleted.append((chat_id,message_id)); return True
    async def send_message(self,*,chat_id,text,**kwargs): self.sent.append((chat_id,text)); return SimpleMsg()
    async def ban_chat_member(self,*,chat_id,user_id): self.banned.append((chat_id,user_id)); return True
    async def restrict_chat_member(self,*,chat_id,user_id,permissions,until_date): self.restricted.append((chat_id,user_id)); return True
class SimpleMsg: message_id=999
class FakeNotifier:
    async def notify_decision(self,**kwargs): return True

def hist(action):
    return ModerationHistoryItem(event_key=f"T-{action}",action=action,category="spam",severity="medium",
        confidence=.96,reason="Confirmed spam",autonomous=True,reversed=False,created_at=datetime.now(timezone.utc))
def context(history=None,text="ordinary spam"):
    snap=MessageSnapshot(db_id=1,telegram_message_id=1,chat_id=-1001,user_id=7,username="@u",full_name="U",
        content_type="text",raw_text=text,normalized_text=text,telegram_date=datetime.now(timezone.utc))
    return MessageContext(current_message=snap,text_signals=analyze_text(text),behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],recent_user_messages=[],user_moderation_history=history or [])
def spam():
    return ModerationDecision(detected_language="English",current_message_violation=True,category="spam",severity="medium",
        confidence=.96,action="warn",delete_message=False,mute_minutes=None,needs_human_review=False,reason="Spam.",
        current_message_evidence=["spam"],context_evidence=[])
def scam():
    return ModerationDecision(detected_language="English",current_message_violation=True,category="scam",severity="high",
        confidence=.99,action="ban",delete_message=True,mute_minutes=None,needs_human_review=False,reason="Scam.",
        current_message_evidence=["scam"],context_evidence=[])

@pytest.mark.asyncio
async def test_first_spam_warns_without_deleting(tmp_path):
    db=Database("sqlite+aiosqlite:///"+(tmp_path/"a.db").as_posix()); await db.init(); audit=AuditRepository(db.session_factory)
    bot=FakeBot(); ex=ModerationExecutor(audit_repository=audit,notifier=FakeNotifier(),bot=bot,dry_run=False,live_delete_enabled=True)
    c=context(); d=spam(); p=PolicyGate().evaluate(d,context=c); r=await ex.execute(context=c,decision=d,policy=p)
    assert r.final_action=="warn" and r.executed is True; assert bot.deleted==[]; assert bot.sent
    await db.dispose()

@pytest.mark.asyncio
async def test_repeat_spam_mute_deletes_trigger(tmp_path):
    db=Database("sqlite+aiosqlite:///"+(tmp_path/"b.db").as_posix()); await db.init(); audit=AuditRepository(db.session_factory)
    bot=FakeBot(); ex=ModerationExecutor(audit_repository=audit,notifier=FakeNotifier(),bot=bot,dry_run=False,live_delete_enabled=True)
    c=context([hist("warn")]); d=spam(); p=PolicyGate().evaluate(d,context=c); r=await ex.execute(context=c,decision=d,policy=p)
    assert p.final_action=="mute"; assert bot.restricted; assert bot.deleted==[(-1001,1)]
    await db.dispose()

@pytest.mark.asyncio
async def test_heavy_scam_auto_ban_off_still_deletes(tmp_path):
    db=Database("sqlite+aiosqlite:///"+(tmp_path/"c.db").as_posix()); await db.init(); audit=AuditRepository(db.session_factory)
    bot=FakeBot(); ex=ModerationExecutor(audit_repository=audit,notifier=FakeNotifier(),bot=bot,dry_run=False,live_delete_enabled=True)
    c=context(text="scam"); d=scam(); p=PolicyGate().evaluate(d,context=c); r=await ex.execute(context=c,decision=d,policy=p)
    assert p.final_action=="ban"; assert bot.banned==[]; assert bot.deleted==[(-1001,1)]
    await db.dispose()


class WarningFailBot(FakeBot):
    async def send_message(self, *, chat_id, text, **kwargs):
        raise RuntimeError("send failed")


@pytest.mark.asyncio
async def test_failed_warning_is_not_recorded_as_confirmed_warn(tmp_path):
    db=Database("sqlite+aiosqlite:///"+(tmp_path/"warn-fail.db").as_posix()); await db.init(); audit=AuditRepository(db.session_factory)
    bot=WarningFailBot(); ex=ModerationExecutor(audit_repository=audit,notifier=FakeNotifier(),bot=bot,dry_run=False,live_delete_enabled=True)
    c=context(); d=spam(); p=PolicyGate().evaluate(d,context=c); r=await ex.execute(context=c,decision=d,policy=p)
    assert r.final_action == "warn"
    assert r.executed is False
    events = await audit.get_recent_events(chat_id=-1001, target_user_id=7, limit=5)
    assert events
    assert events[0].action == "warn_failed"
    effective = await audit.get_effective_user_events(chat_id=-1001, target_user_id=7, limit=5)
    assert all(event.action != "warn" for event in effective)
    await db.dispose()
