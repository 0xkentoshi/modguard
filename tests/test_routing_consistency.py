from datetime import datetime, timezone
import pytest
from app.agent.moderator import ModeratorAgent
from app.agent.schemas import BehaviorSignals, MessageContext, MessageSnapshot
from app.llm.base import LLMProvider
from app.moderation.policy import PolicyGate
from app.utils.text_normalization import analyze_text

class FakeProvider(LLMProvider):
    def __init__(self,payload): self.payload=payload
    async def generate_structured(self,*,system_prompt,user_prompt,response_model): return response_model.model_validate(self.payload)

def ctx(text):
    snap=MessageSnapshot(db_id=1,telegram_message_id=1,chat_id=-1,user_id=7,username="@u",full_name="U",
        content_type="text",raw_text=text,normalized_text=text,telegram_date=datetime.now(timezone.utc))
    return MessageContext(current_message=snap,text_signals=analyze_text(text),behavior_signals=BehaviorSignals(),
        recent_chat_messages=[],recent_user_messages=[],user_moderation_history=[])

@pytest.mark.asyncio
async def test_obvious_ordinary_spam_inconsistent_flags_repair_then_warn():
    provider=FakeProvider({"detected_language":"English","current_message_violation":False,"category":"spam",
        "severity":"medium","confidence":1.0,"action":"delete","delete_message":True,"mute_minutes":None,
        "needs_human_review":False,"reason":"Clear ordinary spam.","current_message_evidence":[],"context_evidence":[]})
    agent=ModeratorAgent(provider); c=ctx("ordinary repeated promo")
    d=await agent.analyze(c); assert d.current_message_violation is True; assert d.current_message_evidence
    p=PolicyGate().evaluate(d,context=c); assert p.final_action=="warn"; assert not p.final_delete_message

@pytest.mark.asyncio
async def test_unsolicited_sexual_dm_solicitation_is_medium_mute_delete():
    provider=FakeProvider({"detected_language":"Russian","current_message_violation":False,
        "category":"unsolicited_advertising","severity":"medium","confidence":.95,"action":"mute",
        "delete_message":True,"mute_minutes":None,"needs_human_review":False,
        "reason":"Unsolicited explicit-content DM solicitation.","current_message_evidence":[],"context_evidence":[]})
    agent=ModeratorAgent(provider); c=ctx("explicit content offer in dm")
    d=await agent.analyze(c); assert d.current_message_violation is True; assert d.current_message_evidence
    p=PolicyGate().evaluate(d,context=c); assert p.final_action=="mute"; assert p.final_delete_message
