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

RELATIONSHIP / SAME-PAIR FEEDBACK
A feedback example may have same_user_pair=true and a moderator-provided
relationship_note. That means a moderator previously said the relationship
between these same participants mattered. Treat it as stronger chat-local soft
context than raw reply counts, but still verify that the CURRENT exchange looks
compatible with the stored rule. Never use friendship/familiarity to ignore a
clear request to stop, one-sided abuse, a credible threat, or protected safety
harm. Raw relationship_context is only weak familiarity evidence.

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
        "relationship_context": (
            context.relationship_signals.model_dump()
            if context.relationship_signals.counterpart_user_id is not None
            else None
        ),
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
                "source": getattr(
                    item,
                    "source",
                    "ticket",
                ),
                "moderator_explanation": getattr(
                    item,
                    "moderator_note",
                    "",
                ),
                "local_rule": getattr(
                    item,
                    "local_rule",
                    "",
                ),
                "same_user_pair": bool(getattr(
                    item,
                    "same_pair",
                    False,
                )),
                "relationship_note": getattr(
                    item,
                    "relationship_note",
                    "",
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


SHADOW_FEEDBACK_INTERPRET_SYSTEM_PROMPT = """
You are ModGuard Shadow Feedback Interpreter.

A real moderator disagreed with a Shadow-mode recommendation and explained the
correction in free text. Your job is to translate that explanation into a safe,
reusable, chat-local moderation precedent.

IMPORTANT
- Do not invent facts the moderator did not provide.
- The correction is soft memory for THIS CHAT, not a global Core rule.
- The correction must not become a blanket permission to ignore obvious scam,
  phishing, malicious links, or credible physical threats.
- Prefer rules phrased in terms of observable conversation context.
- If the moderator relies on a personal relationship such as "these two are
  friends", mark relationship_relevant=true.
- apply_to_same_pair=true only when the case has a known reply counterpart AND
  the moderator clearly says the relationship between these same users matters.
- Raw reply-history familiarity is only a weak signal; it is not proof of
  friendship or consent to abuse.
- If the moderator asks to allow banter, the reusable rule should remain
  conditional on reciprocal/playful context and must not ignore a clear request
  to stop, one-sided degradation, threats, or protected safety harms.

ACTION CONSISTENCY
- corrected_action=allow => current_message_violation=false, category=safe,
  severity=none.
- warn/delete/mute/ban => current_message_violation=true.
- Use escalate when the moderator explicitly says a human should decide or when
  their explanation is too ambiguous to infer another action safely.

unsupported_assumptions lists claims that cannot be observed from the supplied
case/context and should not be generalized automatically.

Write summary, local_rule, relationship_note, and unsupported_assumptions in
concise English. Return only the required JSON. Do not output chain-of-thought.
""".strip()


def build_shadow_feedback_interpret_prompt(
    *,
    case,
    moderator_explanation: str,
) -> str:
    try:
        context_payload = json.loads(
            getattr(case, "context_json", "{}") or "{}"
        )
    except Exception:
        context_payload = {}

    payload = {
        "shadow_case": {
            "message": getattr(case, "message_text", ""),
            "ai_action": getattr(case, "ai_action", None),
            "ai_category": getattr(case, "ai_category", None),
            "ai_severity": getattr(case, "ai_severity", None),
            "ai_confidence": getattr(case, "ai_confidence", None),
            "ai_reason": getattr(case, "ai_reason", ""),
            "target_user_id": getattr(case, "target_user_id", None),
            "counterpart_user_id": getattr(case, "counterpart_user_id", None),
        },
        "available_context": context_payload,
        "moderator_explanation": moderator_explanation,
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
