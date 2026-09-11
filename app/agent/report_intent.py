import json

from pydantic import BaseModel, Field

from app.agent.schemas import MessageContext


class ReportIntentDecision(BaseModel):
    """
    Dedicated semantic intent classification for replies.

    This runs BEFORE ordinary moderation so a complaint cannot accidentally be
    punished just because it contains words such as scam/fraud/spam.
    """

    report_target: bool = False

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    # A report can still independently contain a real violation, e.g. a
    # direct threat toward another participant. This flag is semantic too.
    reporter_has_independent_violation: bool = False

    reason: str = Field(
        min_length=1,
        max_length=400,
    )


REPORT_INTENT_SYSTEM_PROMPT = """
You are ModGuard Report Intent Classifier.

The CURRENT MESSAGE is a REPLY to another Telegram message.

Your only task is to decide whether the CURRENT REPLY is semantically:
1) reporting / flagging / warning about / asking moderators to inspect the
   replied-to message,
or
2) an ordinary reply.

Do NOT use keyword matching.
Understand arbitrary language, slang, sarcasm, transliteration and phrasing.

Examples of semantic report intent include:
- warning others that the replied-to content may be fraudulent/suspicious
- asking mods/bot to check the replied-to message
- saying the replied-to content looks unsafe, scammy, spammy or against rules
- telling others not to trust/click/respond to the replied-to message

Ordinary disagreement, jokes, conversation, compliments and unrelated replies
are NOT reports.

IMPORTANT:
A report is NOT itself a moderation violation merely because it mentions scam,
fraud, spam, sex, threats, etc.

Set reporter_has_independent_violation=true ONLY if the reply itself contains a
separate direct violation, for example a direct threat, targeted severe abuse,
or its own scam/spam solicitation.

Examples:
"это похоже на скам, проверьте" -> report_target=true,
reporter_has_independent_violation=false

"не кликайте, выглядит мутно" -> report_target=true,
reporter_has_independent_violation=false

"проверьте сообщение выше, а автора я найду и убью" -> report_target=true,
reporter_has_independent_violation=true

"ахах норм" -> report_target=false

"ты тупой идиот, заткнись" -> report_target=false,
reporter_has_independent_violation=true

"сам заткнись, ты тупой идиот" -> report_target=false,
reporter_has_independent_violation=true

A direct insult aimed at the replied-to participant is an ordinary hostile reply,
NOT a report merely because it refers to that participant.

"Mods, please check this. This user keeps starting fights." -> report_target=true,
reporter_has_independent_violation=false

"Модеры, посмотрите, он постоянно провоцирует срач." -> report_target=true,
reporter_has_independent_violation=false

The report is only a request for a fresh independent review.
It is never proof that the target is guilty.

CONFIDENCE DISCIPLINE

Use report_target=false with confidence >= 0.99 ONLY when the reply is clearly
ordinary conversation and clearly not asking anyone to inspect/moderate/warn
about the replied-to message.

If there is meaningful uncertainty about report intent, keep confidence below
0.99 so the Deep preflight can verify it.

A phrase such as:
"модеры, не уверен насчёт этого, проверьте"
is a report, even though it does not literally accuse anyone of scam.

Return only the required JSON.
Keep reason very short and in English.
""".strip()


def build_report_intent_prompt(
    context: MessageContext,
) -> str:
    target = context.reply_target_message
    current = context.current_message

    payload = {
        "current_reply": {
            "text": current.raw_text,
            "normalized_text": current.normalized_text,
        },
        "reply_target": (
            {
                "text": target.raw_text,
                "normalized_text": target.normalized_text,
            }
            if target is not None
            else None
        ),
        "nearby_context": [
            {
                "same_author_as_reporter": (
                    current.user_id is not None
                    and item.user_id == current.user_id
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
