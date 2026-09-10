import json

from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)


FEEDBACK_REVIEW_SYSTEM_PROMPT = """
You are ModGuard Moderator Feedback Reviewer.

The stable Core moderator has already evaluated the CURRENT MESSAGE.
It is uncertain enough that human-review history MAY help.

You receive:
- the current message
- Core's current decision
- REAL prior moderator decisions from THIS SAME CHAT

Your job is NOT to blindly copy moderators.
Your job is to determine whether prior examples are genuinely semantically
similar enough to guide this ambiguous case.

FEEDBACK IS SOFT GUIDANCE

Human feedback:
- may help resolve gray/community-specific situations
- is chat-specific
- is NOT a replacement for Core safety
- is NOT an explicit Community Policy
- never authorizes ignoring obvious current-message harm

Do not use an old ALLOW example to excuse obvious:
- scam
- phishing
- malicious links
- credible physical threats

Do not force similarity merely because two messages share a word.
Compare intent, context and moderation meaning.

If examples are unrelated:
relevant=false
recommended_action=escalate

If relevant examples clearly establish local moderator preference:
relevant=true
and recommend the appropriate action.

Examples may be multilingual. Match meaning across language, slang,
transliteration and paraphrase.

EVIDENCE

current_message_evidence may contain evidence ONLY from the CURRENT MESSAGE.
Never copy evidence from a historical example as if it appeared now.

CONSISTENCY

If recommended_action=allow:
- current_message_violation=false
- category=safe

If recommended_action is warn/delete/mute/ban:
- current_message_violation=true
- current_message_evidence must be non-empty

If still uncertain:
- recommended_action=escalate

Confidence is 0.0..1.0. Use 1.0, never 100.
Write reason and evidence descriptions in concise English.

Return only the required JSON.
Do not output chain-of-thought.
""".strip()


def build_feedback_review_prompt(
    *,
    context: MessageContext,
    baseline_decision: ModerationDecision,
    baseline_policy: PolicyEvaluation,
    examples,
) -> str:
    payload = {
        "current_message": {
            "text": (
                context.current_message.raw_text
            ),
            "normalized_text": (
                context.current_message
                .normalized_text
            ),
        },
        "core_decision": {
            "category": (
                baseline_decision.category
            ),
            "severity": (
                baseline_decision.severity
            ),
            "confidence": (
                baseline_decision.confidence
            ),
            "action": (
                baseline_decision.action
            ),
            "final_action": (
                baseline_policy.final_action
            ),
            "reason": (
                baseline_decision.reason
            ),
            "current_message_evidence": (
                baseline_decision
                .current_message_evidence
            ),
        },
        "moderator_feedback_examples": [
            {
                "feedback_id": item.id,
                "message": item.message_text,
                "ai_category": (
                    item.ai_category
                ),
                "ai_reason": (
                    item.ai_reason
                ),
                "moderator_action": (
                    item.moderator_action
                ),
                "policy_version": (
                    item.policy_version
                ),
            }
            for item in examples
        ],
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
