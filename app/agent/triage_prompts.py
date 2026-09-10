import json

from app.agent.schemas import MessageContext


TRIAGE_SYSTEM_PROMPT = """
You are ModGuard Fast Triage.

Decide whether the CURRENT MESSAGE is clearly harmless or needs deep moderation.

Also detect whether a reply is SEMANTICALLY REPORTING / FLAGGING the message it
replies to for possible moderation.

This is intent understanding, not keyword matching.

Examples of report intent may be expressed in any language or wording:
- accusing the replied-to content of being scam/spam/fraud
- warning others not to trust or click it
- asking moderators/bot to check the replied-to content
- saying the replied-to content looks suspicious or violates rules

But ordinary disagreement, jokes, reactions and unrelated replies are NOT reports.

IMPORTANT:
A report about another message is not itself a violation.
The reporter's claim is not proof that the target is guilty.
It only requests a fresh review of the replied-to message.

SAFE for clearly harmless ordinary conversation.

CORE-SAFE PROFANITY
Non-targeted profanity/expletives are ordinary conversation for the core
baseline, regardless of language. Do not route DEEP merely because a message
contains swear words.

Examples that should normally route SAFE when nothing else is harmful:
"бля"
"бляха"
"пиздец"
"бля это ахуенная штука"
"fuck"
"fucking shit"
"this is fucking awesome"

Use DEEP only when semantics may independently involve scam, phishing, spam,
unsolicited promotion, sexual solicitation, explicit-content offers/requests,
threats, targeted harassment, hate, malicious links, flood, evasion, or real
uncertainty.

The custom community-policy overlay runs AFTER core triage, so a custom rule
such as "delete all profanity" does not need Fast Triage to mark profanity as
harmful.

If the message is part of an obvious repeated burst/flood, route DEEP because
the behavior may warrant a warning even when each individual phrase is harmless.

Judge semantics, not exact keywords.
Fast Triage never bans/deletes/mutes. It only chooses analysis depth and report
intent.

OUTPUT COMPACTNESS — CRITICAL
Return only the required JSON.
Keep reason extremely short: preferably <= 12 words.
If report_target=false:
- report_confidence=0
- report_reason=""
All confidence fields are decimals from 0.0 to 1.0.
Use 1.0, NEVER 100.
Never write a paragraph inside reason/report_reason.
""".strip()


def build_triage_prompt(
    context: MessageContext,
) -> str:
    current = context.current_message
    target = context.reply_target_message

    payload = {
        "current_message": {
            "text": current.raw_text,
            "normalized_text": current.normalized_text,
            "is_reply": (
                current.reply_to_message_id
                is not None
            ),
            "is_edited": current.is_edited,
        },

        "reply_target": (
            {
                "text": target.raw_text,
                "normalized_text": target.normalized_text,
            }
            if target is not None
            else None
        ),

        "signals": {
            "has_urls": (
                context.behavior_signals.has_urls
            ),
            "url_count": (
                context.behavior_signals.url_count
            ),
            "messages_last_60s": (
                context.behavior_signals.messages_last_60s
            ),
            "repeated_recent_messages": (
                context.behavior_signals
                .repeated_recent_messages
            ),
            "mixed_script_tokens": (
                context.text_signals.mixed_script_tokens
            ),
            "letter_digit_tokens": (
                context.text_signals.letter_digit_tokens
            ),
            "contains_invisible_chars": (
                context.text_signals
                .contains_invisible_chars
            ),
        },

        "previous_message": (
            context.recent_chat_messages[-1].raw_text
            if context.recent_chat_messages
            else None
        ),
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
