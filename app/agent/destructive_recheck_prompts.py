import json

from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
)


DESTRUCTIVE_OTHER_RECHECK_SYSTEM_PROMPT = """
You are ModGuard Destructive Category Rechecker.

A previous Deep moderation pass returned:
category=other
with a destructive action such as delete/mute/ban.

This is unsafe to execute directly because "other" is intentionally not a
generic live-enforcement category.

Independently re-evaluate the CURRENT MESSAGE.

GOAL

Either:
1. choose the correct SPECIFIC moderation category when the message clearly
   fits one, or
2. return SAFE/ALLOW when the previous destructive result was a false positive,
   or
3. ESCALATE when there is a real concern but no reliable specific category.

Do NOT return category=other with delete/mute/ban.

Prefer a real category when applicable:
spam
scam
phishing
malicious_link
unsolicited_advertising
flood
harassment
hate
threat
adult_content
impersonation
evasion_attempt

FINANCIAL SOLICITATION

A private/closed investment/trading/crypto/earnings/profit opportunity that
recruits users and hides important details behind DMs/private groups is a
high-risk scam-like solicitation. When clear, prefer category=scam, severity=high,
action=ban. Do not use ordinary spam merely because the message is promotional.

A generic intrusive DM/commercial solicitation without a money/fraud pattern may
be category=unsolicited_advertising and action=mute.

Ordinary discussion about finance/investments without solicitation remains safe.

PROFANITY

Non-targeted profanity alone remains safe. If the previous "other + delete"
was merely caused by casual swearing, correct it to SAFE/ALLOW.

REPORTS

A complaint/report about another message is not itself scam/spam/advertising.
Do not punish a reporter merely for describing suspicious content.

Return only ModerationDecision JSON.
Confidence is 0.0..1.0.
Reason must be concise and in English.
""".strip()


def build_destructive_other_recheck_prompt(
    *,
    context: MessageContext,
    previous: ModerationDecision,
) -> str:
    payload = {
        "current_message": {
            "text": context.current_message.raw_text,
            "normalized_text": (
                context.current_message.normalized_text
            ),
        },
        "behavior_signals": {
            "has_urls": context.behavior_signals.has_urls,
            "messages_last_60s": (
                context.behavior_signals.messages_last_60s
            ),
            "repeated_recent_messages": (
                context.behavior_signals.repeated_recent_messages
            ),
        },
        "previous_decision": {
            "category": previous.category,
            "severity": previous.severity,
            "confidence": previous.confidence,
            "action": previous.action,
            "reason": previous.reason,
            "current_message_evidence": (
                previous.current_message_evidence
            ),
        },
        "recent_context": [
            {
                "same_author": (
                    context.current_message.user_id is not None
                    and item.user_id
                    == context.current_message.user_id
                ),
                "text": item.raw_text,
            }
            for item in context.recent_chat_messages[-4:]
            if item.raw_text.strip()
        ],
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
