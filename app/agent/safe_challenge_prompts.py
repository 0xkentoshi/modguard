import json

from app.agent.schemas import MessageContext


SAFE_CHALLENGE_SYSTEM_PROMPT = """
You are ModGuard Safe Challenge.

Fast Triage has just claimed that the CURRENT MESSAGE is clearly safe.
Your job is to challenge that conclusion, not to punish anyone.

Return route=DEEP whenever there is any plausible moderation concern that
needs the stronger model, including:
- scam/phishing/fraud or suspicious financial/private-DM recruitment
- unsolicited promotion, advertising or gambling promotion
- spam/flood/repeated low-value behavior
- targeted degradation, harassment, intimidation or an ongoing fight
- threats, hate, adult-content solicitation, impersonation or evasion
- slang, euphemisms, transliteration or indirect wording that may hide these
- uncertainty caused by surrounding conversation or prior moderation history

Examples that deserve DEEP review include semantic equivalents of:
- "Exclusive earnings group, details in DM."
- "Залетайте со мной в казик покрутим"
- "иди нахуй долбоеб"
- a message that looks mild alone but plausibly continues a recently warned fight

Ordinary profanity by itself is not a violation. Ordinary discussion about
finance, gambling, sex or security is not automatically a violation either.

Return SAFE only when the current message is genuinely ordinary and there is
no meaningful reason for Deep review.

This is routing only. Never decide warn/delete/mute/ban here.
Analyze any language. Return only the required TriageDecision JSON.
Set report_target=false and report_confidence=0 because report intent is handled
by a dedicated classifier before this step.
""".strip()


def build_safe_challenge_prompt(context: MessageContext) -> str:
    current = context.current_message
    payload = {
        "current_message": {
            "text": current.raw_text,
            "normalized_text": current.normalized_text,
            "is_reply": current.reply_to_message_id is not None,
        },
        "behavior": {
            "messages_last_60s": context.behavior_signals.messages_last_60s,
            "repeated_recent_messages": context.behavior_signals.repeated_recent_messages,
            "has_urls": context.behavior_signals.has_urls,
        },
        "recent_chat": [
            {
                "same_author": (
                    current.user_id is not None and item.user_id == current.user_id
                ),
                "text": item.raw_text,
            }
            for item in context.recent_chat_messages[-5:]
            if item.raw_text.strip()
        ],
        "recent_user": [
            item.raw_text
            for item in context.recent_user_messages[-4:]
            if item.raw_text.strip()
        ],
        "moderation_history": [
            {
                "action": item.action,
                "category": item.category,
                "severity": item.severity,
                "confidence": item.confidence,
            }
            for item in context.user_moderation_history[-6:]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
