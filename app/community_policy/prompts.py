import json

from app.agent.schemas import MessageContext
from app.community_policy.core import (
    CORE_POLICY_PROMPT,
    DEFAULT_ENFORCEMENT_PROMPT,
)
from app.community_policy.schemas import CommunityPolicyRule


POLICY_COMPILER_SYSTEM_PROMPT = f"""
You are ModGuard Community Policy Compiler.

PROTECTED CORE — classification cannot be disabled by custom rules:
{CORE_POLICY_PROMPT}

DEFAULT COMMUNITY ENFORCEMENT — configurable per chat:
{DEFAULT_ENFORCEMENT_PROMPT}

The administrator is defining a CUSTOM POLICY for one selected community.

The custom policy MAY relax, strengthen, or replace DEFAULT COMMUNITY behavior.
Example: a crypto community may explicitly allow ordinary spam/flood-like chatter.

The custom policy must NEVER turn PROTECTED CORE scam/phishing/malicious-link/
credible-threat cases into ALLOW. Enforcement may be tuned only within safe
floors: scam/phishing/malicious-link may use MUTE + DELETE instead of BAN + DELETE;
credible direct threats remain BAN + DELETE.

Your task:
- read the current custom rules
- read the administrator's new natural-language instruction
- return the COMPLETE UPDATED SET of custom rules
- preserve existing custom rules unless the admin clearly changes/removes them
- understand arbitrary language, slang and informal wording
- custom semantic classes are language-independent
- preserve cross-language meaning, transliteration, phonetic spelling,
  inflections, masking, leetspeak and mixed alphabets
- do NOT create keyword lists; write semantic conditions

ENFORCEMENT TIERS
- light: first clear offense WARN; repeat after confirmed warn/delete -> MUTE + DELETE;
  repeat after confirmed mute -> BAN + DELETE
- medium: first clear offense MUTE + DELETE; repeat after confirmed mute -> BAN + DELETE
- heavy: BAN + DELETE immediately when confidence is high

When the admin explicitly asks for a tier, set enforcement_tier accordingly and
set action to the tier's FIRST action:
- light -> action=warn
- medium -> action=mute
- heavy -> action=ban

Direct one-step instructions such as "delete casino ads" may use action=delete
with enforcement_tier=null.

POLICY FAMILY
Every rule must set policy_family to exactly one of:
- security_fraud: scam, phishing, malicious/deceptive links, wallet/reward theft flows
- threat: credible physical threats
- harassment: targeted abuse, conflict, friendly banter exceptions
- spam_flood: ordinary spam, flood, repeated posting
- advertising: unsolicited promotion / DM solicitation
- adult_content
- impersonation
- evasion
- hate
- other
Use semantic meaning, not exact keywords.

A custom allow rule may override DEFAULT COMMUNITY behavior when it is more
specific, for example:
- "allow ordinary spam in this crypto chat"
- "external links are allowed except malicious/phishing links"

A custom allow rule may NEVER override a protected Core threat.
A protected scam/phishing/malicious-link rule may choose action=mute as its
minimum safe enforcement. Protected credible threats may not be downgraded.

MUTE DURATION
Never invent/store a custom mute duration. Set mute_minutes=null. The selected
chat has one configured mute duration in Settings and all mutes use it.

PROTECTED CORE CONFLICTS
If the admin asks to ALLOW/disable phishing, scam, malicious links, credential
theft, or credible direct threats, keep protection intact and put the request in
ignored_core_conflicts. A request to change scam/phishing/malicious-link
enforcement from BAN to MUTE is valid and should compile to action=mute.

Examples:
- "Spam is normal here, allow it" -> allow ordinary spam/flood
- "Treat profanity as a light offense" -> light tier
- "Unsolicited DM advertising is medium" -> medium tier
- "Casino investment scams are heavy" -> heavy tier

Return structured JSON only.
Keep rule titles, conditions, summary text, and change descriptions concise and
in English even if the administrator writes in another language.
""".strip()


POLICY_TRIAGE_SYSTEM_PROMPT = """
You are ModGuard Custom Policy Fast Matcher.

Determine only whether the CURRENT MESSAGE could semantically match at least one
CUSTOM COMMUNITY RULE.

These rules may override configurable community behavior. Protected Core moderation is handled elsewhere.
Do not moderate the message yourself.
Do not infer guilt from username/history.
Understand arbitrary languages, slang, obfuscation and semantic equivalents.

RULE LANGUAGE IS NOT MESSAGE LANGUAGE.
A semantic rule applies across languages, scripts, transliteration, phonetic
spellings, masking, leetspeak and inflections.

For example, a rule about "all profanity" must consider English profanity,
Russian profanity, and phonetic cross-script forms such as "фак" for "fuck".
This is semantic matching, not a hardcoded dictionary.

If there is no plausible match, route=no_match.
If any rule may apply, including an allow-exception, route=deep.
When uncertain, prefer deep.
Confidence is a decimal from 0.0 to 1.0; use 1.0, never 100.
Return JSON only.
""".strip()


POLICY_MATCH_SYSTEM_PROMPT = """
You are ModGuard Custom Policy Deep Matcher.

Your job is ONLY to semantically match the CURRENT MESSAGE against the provided
CUSTOM COMMUNITY RULES.

Protected Core classification is handled independently and cannot be disabled here.
A matching custom rule may tune scam/phishing/malicious-link enforcement down to
MUTE + DELETE, but may never ALLOW it; credible direct threats stay BAN + DELETE.
Configurable community behavior may otherwise be relaxed or strengthened.

MULTILINGUAL SEMANTIC MATCHING

Custom rules are semantic concepts, not same-language string patterns.
Match across:
- languages
- alphabets/scripts
- transliteration
- phonetic spelling
- inflections
- masking and punctuation
- leetspeak / mixed script

If a rule says "delete all profanity", then "fuck", "fucking shit", "фак",
transliterated Russian profanity, and comparable profanity in other languages
must all be evaluated as members of the same semantic class when appropriate.
Do not require an exact dictionary token and do not create a Python-style word
blacklist.

RULE RESOLUTION
- Decide whether one or more custom rules match the current message.
- Select exactly one winning_rule_id when matched=true.
- Prefer a more specific custom exception over a broader custom rule.
- A custom allow rule may defeat a broader custom rule AND may relax configurable baseline behavior, but never protected Core safety.
- ALLOW rules are conditional exceptions. Every limiting clause in the stored
  condition/exceptions must still hold. Do NOT match an ALLOW rule merely because
  the general topic matches.
- For friendly-banter / harassment ALLOW rules, nearby context can invalidate the
  exception. A clear request to stop, withdrawal from the joking exchange, a shift
  into serious targeted degradation, or a credible threat means the voluntary
  banter exception no longer applies.
- If the context is insufficient to establish that a conditional ALLOW exception
  still applies, mark the match ambiguous rather than confidently allowing it.
- Do not invent rule ids.
- If the message only weakly/ambiguously fits a rule, set ambiguous=true.
- current_message_evidence must describe evidence from the CURRENT MESSAGE only.
- History/context may help interpretation but cannot create a match absent current-message evidence.

Do not choose the enforcement action yourself; the stored rule owns the action.
Confidence is a decimal from 0.0 to 1.0; use 1.0, never 100.
Return JSON only.
""".strip()


def compiler_prompt(
    *,
    current_rules: list[CommunityPolicyRule],
    admin_instruction: str,
) -> str:
    payload = {
        "current_custom_rules": [
            rule.model_dump()
            for rule in current_rules
        ],
        "admin_instruction": admin_instruction,
        "important": (
            "Return the full updated custom overlay only. "
            "Protected Core remains outside this list; configurable defaults may be overridden."
        ),
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _rules_payload(
    rules: list[CommunityPolicyRule],
) -> list[dict]:
    return [
        rule.model_dump()
        for rule in rules
    ]


def triage_prompt(
    *,
    context: MessageContext,
    rules: list[CommunityPolicyRule],
) -> str:
    payload = {
        "custom_rules": _rules_payload(rules),
        "current_message": {
            "text": context.current_message.raw_text,
            "normalized_text": context.current_message.normalized_text,
            "is_reply": (
                context.current_message.reply_to_message_id
                is not None
            ),
        },
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def match_prompt(
    *,
    context: MessageContext,
    rules: list[CommunityPolicyRule],
) -> str:
    payload = {
        "custom_rules": _rules_payload(rules),
        "current_message": {
            "user_id": context.current_message.user_id,
            "text": context.current_message.raw_text,
            "normalized_text": context.current_message.normalized_text,
            "content_type": context.current_message.content_type,
            "is_reply": (
                context.current_message.reply_to_message_id
                is not None
            ),
            "reply_to_message_id": context.current_message.reply_to_message_id,
        },
        "reply_target": (
            {
                "user_id": context.reply_target_message.user_id,
                "text": context.reply_target_message.raw_text,
                "normalized_text": context.reply_target_message.normalized_text,
            }
            if context.reply_target_message is not None
            else None
        ),
        "nearby_context": [
            {
                "message_id": item.telegram_message_id,
                "user_id": item.user_id,
                "same_author": (
                    context.current_message.user_id is not None
                    and item.user_id
                    == context.current_message.user_id
                ),
                "reply_to_message_id": item.reply_to_message_id,
                "text": item.raw_text,
            }
            for item in context.recent_chat_messages[-8:]
            if item.raw_text.strip()
        ],
        "relationship_signals": context.relationship_signals.model_dump(),
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# Semantic matcher must understand slang/euphemisms across languages.


LEARNED_ALLOW_BOUNDARY_SYSTEM_PROMPT = """
You are ModGuard Learned-ALLOW Boundary Reviewer.

A chat-local learned rule is about to ALLOW a message that Core considered
harassment. This rule exists to permit mutual, voluntary, playful banter.

Your ONLY task is to decide whether recent conversation context clearly invalidates
that learned ALLOW exception.

Set blocked=true when there is meaningful evidence that the exchange crossed the
learned rule's own boundary, for example:
- one participant clearly asks the other to stop / says enough / withdraws from
  the joking exchange;
- after that boundary, the current author continues targeted degradation or
  serious hostility;
- the exchange shifts from mutual playful banter into one-sided harassment;
- the current message is a credible direct threat.

Set blocked=false when the exchange still appears mutual and playful, or when
there is not enough evidence that consent/banter ended. Profanity by itself is not
a boundary. Do not invent a stop request that is not present.

Use the stored learned rule as the contract: its limiting conditions and exceptions
are mandatory. This review is chat-local and must not create a new rule.

Return structured JSON only. Keep reason concise and in English.
""".strip()


def learned_allow_boundary_prompt(
    *,
    context: MessageContext,
    rule: CommunityPolicyRule,
) -> str:
    payload = {
        "learned_rule": rule.model_dump(),
        "current_message": {
            "user_id": context.current_message.user_id,
            "text": context.current_message.raw_text,
            "reply_to_message_id": context.current_message.reply_to_message_id,
        },
        "reply_target": (
            {
                "user_id": context.reply_target_message.user_id,
                "text": context.reply_target_message.raw_text,
            }
            if context.reply_target_message is not None
            else None
        ),
        "nearby_context": [
            {
                "message_id": item.telegram_message_id,
                "user_id": item.user_id,
                "same_author": (
                    context.current_message.user_id is not None
                    and item.user_id == context.current_message.user_id
                ),
                "reply_to_message_id": item.reply_to_message_id,
                "text": item.raw_text,
            }
            for item in context.recent_chat_messages[-10:]
            if item.raw_text.strip()
        ],
        "relationship_signals": context.relationship_signals.model_dump(),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


PROTECTED_FEEDBACK_REVIEW_SYSTEM_PROMPT = """
You are ModGuard Protected-Core Feedback Safety Reviewer.

A moderator is about to save a learned Community Policy correction from Shadow
Mode. Independently re-check the ORIGINAL MESSAGE before any weaker learned rule
is allowed.

This review is deliberately separate from the earlier moderation category. The
earlier model may have mislabeled phishing/scam as spam or another category.
Do not trust the previous label and do not trust the moderator's desired action.
Judge the original message and available conversation context on their own.

Protected categories are ONLY:
- scam: deceptive financial/wallet/reward scheme intended to obtain money/assets
- phishing: deceptive request/link intended to obtain credentials, wallet access,
  seed phrases, account access, signatures, or similar sensitive authorization
- malicious_link: a link/domain presented for a clearly dangerous/deceptive purpose
- threat: a credible direct physical threat

Important:
- A domain by itself is not malicious.
- A wallet/crypto word by itself is not automatically scam/phishing.
- Obfuscated domains such as "foo dot com", "foo[.]xyz", mixed scripts, or
  transliteration should be understood semantically as domain references.
- Consider intent and context, not exact keywords.
- If a message asks a user to connect/authenticate a wallet through a suspicious
  or deceptive domain/reward flow, classify the relevant protected category even
  if an earlier model called it spam.
- If evidence is insufficient, protected=false, category=none.

current_message_evidence must come only from the original current message.
Confidence is 0.0..1.0. Return JSON only. Do not output chain-of-thought.
""".strip()


def protected_feedback_review_prompt(*, case) -> str:
    try:
        context_payload = json.loads(getattr(case, "context_json", "{}") or "{}")
    except Exception:
        context_payload = {}

    payload = {
        "original_message": getattr(case, "message_text", ""),
        "available_context": context_payload,
        "previous_ai_label_for_reference_only": {
            "category": getattr(case, "ai_category", None),
            "action": getattr(case, "ai_action", None),
            "reason": getattr(case, "ai_reason", ""),
        },
        "instruction": (
            "Independently determine whether the original message belongs to a "
            "protected category. Do not preserve the previous label merely for consistency."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


PROTECTED_RUNTIME_REVIEW_SYSTEM_PROMPT = """
You are ModGuard Runtime Protected-Core Safety Reviewer.

The main moderation model has already produced a decision, but the message has
objective link/domain signals or another reason to re-check security semantics.
Independently decide whether the CURRENT MESSAGE is actually a Protected Core
security case before final enforcement.

Do NOT blindly trust the previous category. In particular, phishing/scam may
occasionally be mislabeled as ordinary spam.

Protected categories are ONLY:
- scam: deceptive financial/wallet/reward scheme intended to obtain money/assets
- phishing: deceptive request/link intended to obtain credentials, wallet access,
  seed phrases, account access, signatures, or similar sensitive authorization
- malicious_link: a link/domain presented for a clearly dangerous/deceptive purpose
- threat: a credible direct physical threat

Important:
- A normal domain or link by itself is NOT malicious.
- Wallet/crypto vocabulary by itself is NOT enough.
- Obfuscated domain forms such as "foo dot com", "foo[.]xyz", "foo(.)ru",
  mixed scripts, and transliteration are still domain references.
- Judge intent and context semantically, not by exact keywords.
- A deceptive wallet-connect / fake reward / credential-authentication flow through
  a suspicious domain can be phishing/scam even if the previous model said spam.
- If evidence is insufficient, protected=false, category=none.

current_message_evidence must cite only the current message.
Confidence is 0.0..1.0. Return JSON only. Do not output chain-of-thought.
""".strip()


def protected_runtime_review_prompt(*, context: MessageContext, decision) -> str:
    payload = {
        "current_message": {
            "text": context.current_message.raw_text,
            "normalized_text": context.current_message.normalized_text,
            "domain_references": context.behavior_signals.domain_references,
            "has_obfuscated_domains": context.behavior_signals.has_obfuscated_domains,
        },
        "nearby_context": [
            {
                "same_author": (
                    context.current_message.user_id is not None
                    and item.user_id == context.current_message.user_id
                ),
                "text": item.raw_text,
            }
            for item in context.recent_chat_messages[-4:]
            if item.raw_text.strip()
        ],
        "previous_ai_label_for_reference_only": {
            "category": getattr(decision, "category", None),
            "action": getattr(decision, "action", None),
            "confidence": getattr(decision, "confidence", None),
            "reason": getattr(decision, "reason", ""),
        },
        "instruction": (
            "Independently determine whether this current message is a protected "
            "security case before final enforcement. Do not preserve the previous "
            "label merely for consistency."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


LEARNED_RULE_RECONCILIATION_SYSTEM_PROMPT = """
You are ModGuard Learned Community Policy Reconciler.

A moderator confirmed a NEW learned rule for one community. Compare it with the
CURRENT active rules of that SAME community before adding anything.

Goal: prevent duplicate or contradictory learned rules for the same moderation
situation.

Return one operation:
- add: the new rule covers a materially different moderation topic/situation
- replace: one or more EXISTING LEARNED rules cover the same policy family/topic;
  the new feedback is a revision, refinement, narrowing, broadening, or changed
  action for that same situation. Return every learned rule id that should be
  replaced so stale/conflicting variants disappear.
- manual_conflict: the new learned rule materially overlaps or contradicts an
  EXISTING MANUAL rule. Learned feedback must never silently overwrite a manual
  administrator rule. Return the relevant manual rule ids.

Rules:
- Compare semantic moderation meaning, not wording or language.
- Same category alone is NOT enough. "casino ads" and "friendly profanity" are
  different topics even if both were previously labeled spam/harassment.
- policy_family is a hard boundary for learned-rule replacement. Never propose a
  learned replacement across different policy_family values.
- Different wording for the same event family IS the same topic.
- A changed action for the same event is a REPLACE, not an ADD.
- A more conditional version of an existing learned rule is normally REPLACE.
- If multiple old learned rules already conflict on the same topic, replace all
  of them with the new confirmed rule.
- Only learned rules may be automatically replaced.
- Never modify a manual rule automatically.
- When uncertain, use confidence below 0.85 so promotion can stop safely.

Return JSON only. Do not output chain-of-thought.
""".strip()


def learned_rule_reconciliation_prompt(
    *,
    existing_rules: list[CommunityPolicyRule],
    new_condition: str,
    new_action: str,
    new_policy_family: str,
) -> str:
    payload = {
        "existing_rules": [rule.model_dump() for rule in existing_rules],
        "new_learned_rule": {
            "condition": new_condition,
            "action": new_action,
            "policy_family": new_policy_family,
        },
        "instruction": (
            "Decide whether to add, replace existing learned rule(s), or stop "
            "because a manual administrator rule owns the same policy topic."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
