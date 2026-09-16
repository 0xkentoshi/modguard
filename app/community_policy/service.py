import json
import logging
import re
import time

from app.admin.control_repository import ControlRepository
from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)
from app.community_policy.core import PROTECTED_CORE_CATEGORIES
from app.community_policy.prompts import (
    LEARNED_RULE_RECONCILIATION_SYSTEM_PROMPT,
    POLICY_COMPILER_SYSTEM_PROMPT,
    POLICY_MATCH_SYSTEM_PROMPT,
    POLICY_TRIAGE_SYSTEM_PROMPT,
    LEARNED_ALLOW_BOUNDARY_SYSTEM_PROMPT,
    PROTECTED_FEEDBACK_REVIEW_SYSTEM_PROMPT,
    PROTECTED_RUNTIME_REVIEW_SYSTEM_PROMPT,
    compiler_prompt,
    learned_rule_reconciliation_prompt,
    learned_allow_boundary_prompt,
    match_prompt,
    protected_feedback_review_prompt,
    protected_runtime_review_prompt,
    triage_prompt,
)
from app.community_policy.schemas import (
    CommunityPolicyCompilation,
    CommunityPolicyMatch,
    CommunityPolicyRule,
    CommunityPolicyTriage,
    LearnedRuleReconciliation,
    LearnedAllowBoundaryReview,
    ProtectedFeedbackSafetyReview,
)
from app.llm.base import LLMProvider
from app.moderation.policy import PolicyGate


logger = logging.getLogger(__name__)


ACTION_RANK = {
    "allow": 0,
    "warn": 1,
    "escalate": 2,
    "delete": 3,
    "mute": 4,
    "ban": 5,
}


PROTECTED_ENFORCEMENT_FLOOR = {
    "scam": "mute",
    "phishing": "mute",
    "malicious_link": "mute",
    "threat": "ban",
}


POLICY_FAMILY_BY_CATEGORY = {
    "scam": "security_fraud",
    "phishing": "security_fraud",
    "malicious_link": "security_fraud",
    "threat": "threat",
    "harassment": "harassment",
    "spam": "spam_flood",
    "flood": "spam_flood",
    "unsolicited_advertising": "advertising",
    "adult_content": "adult_content",
    "impersonation": "impersonation",
    "evasion_attempt": "evasion",
    "hate": "hate",
}


def parse_rules_json(
    raw: str | None,
) -> list[CommunityPolicyRule]:
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except Exception:
        logger.exception(
            "Invalid stored community policy JSON"
        )
        return []

    if not isinstance(payload, list):
        return []

    rules: list[CommunityPolicyRule] = []

    for item in payload:
        try:
            rules.append(
                CommunityPolicyRule.model_validate(
                    item
                )
            )
        except Exception:
            logger.exception(
                "Invalid stored community policy rule"
            )

    return rules


def rules_json(
    rules: list[CommunityPolicyRule],
) -> str:
    return json.dumps(
        [
            rule.model_dump()
            for rule in rules
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class CommunityPolicyService:
    """
    Per-chat policy overlay with a protected-core boundary.

    Core moderation always runs first. Protected heavy safety categories cannot
    be weakened. Configurable LIGHT/MEDIUM community behavior may intentionally
    be relaxed or strengthened by an explicit admin rule.
    """

    def __init__(
        self,
        *,
        repository: ControlRepository,
        deep_provider: LLMProvider,
        fast_provider: LLMProvider | None = None,
        compiler_provider: LLMProvider | None = None,
    ):
        self.repository = repository
        self.deep_provider = deep_provider
        self.fast_provider = fast_provider
        self.compiler_provider = (
            compiler_provider
            or deep_provider
        )

    async def compile_update(
        self,
        *,
        admin_id: int,
        chat_id: int,
        admin_instruction: str,
    ):
        text = admin_instruction.strip()
        if not text:
            raise ValueError(
                "Community policy instruction is empty."
            )

        # When admin presses "Change" on a preview, use the draft as the base
        # so follow-up wording can refine the pending version. A fresh Edit
        # clears the draft first in the admin handler.
        existing_draft = (
            await self.repository
            .get_community_policy_draft(
                admin_id
            )
        )

        active = (
            await self.repository
            .get_active_community_policy(
                chat_id
            )
        )

        if (
            existing_draft is not None
            and existing_draft.chat_id
            == chat_id
        ):
            current_rules = parse_rules_json(
                existing_draft.rules_json
            )
            base_version = (
                existing_draft.base_version
            )
        else:
            current_rules = parse_rules_json(
                active.rules_json
                if active is not None
                else "[]"
            )
            base_version = (
                active.version
                if active is not None
                else None
            )

        started = time.perf_counter()

        compilation = (
            await self.compiler_provider
            .generate_structured(
                system_prompt=(
                    POLICY_COMPILER_SYSTEM_PROMPT
                ),
                user_prompt=compiler_prompt(
                    current_rules=current_rules,
                    admin_instruction=text,
                ),
                response_model=(
                    CommunityPolicyCompilation
                ),
            )
        )

        normalized_rules: list[
            CommunityPolicyRule
        ] = []

        for index, rule in enumerate(
            compilation.rules[:20],
            start=1,
        ):
            normalized_rules.append(
                rule.model_copy(
                    update={
                        "rule_id": f"R{index}",
                        # One mute duration exists per chat in Settings.
                        "mute_minutes": None,
                    }
                )
            )

        elapsed_ms = int(
            (
                time.perf_counter()
                - started
            )
            * 1000
        )

        draft = (
            await self.repository
            .save_community_policy_draft(
                admin_id=admin_id,
                chat_id=chat_id,
                base_version=base_version,
                source_text=text,
                rules_json=rules_json(
                    normalized_rules
                ),
                summary=(
                    compilation.summary
                ),
                changes_json=json.dumps(
                    compilation.changes,
                    ensure_ascii=False,
                ),
                ignored_json=json.dumps(
                    compilation
                    .ignored_core_conflicts,
                    ensure_ascii=False,
                ),
            )
        )

        logger.info(
            "COMMUNITY POLICY COMPILE | "
            "chat=%s | admin=%s | rules=%s | ms=%s",
            chat_id,
            admin_id,
            len(normalized_rules),
            elapsed_ms,
        )

        return draft

    async def apply_draft(
        self,
        *,
        admin_id: int,
        chat_id: int,
    ):
        version = (
            await self.repository
            .apply_community_policy_draft(
                admin_id=admin_id,
                expected_chat_id=chat_id,
            )
        )

        if version is not None:
            logger.info(
                "COMMUNITY POLICY APPLY | "
                "chat=%s | version=%s | admin=%s",
                version.chat_id,
                version.version,
                admin_id,
            )

        return version

    async def apply_security_preset(
        self,
        *,
        chat_id: int,
        admin_id: int,
        preset: str,
    ):
        """Apply a deterministic security_fraud preset without touching Core.

        `strict` removes only community security_fraud overrides so Protected
        Core owns scam/phishing enforcement. `progressive` installs one stable
        MEDIUM per-user rule: first unique offense -> MUTE + DELETE, repeat ->
        BAN + DELETE. Other policy families and learned rules are preserved.
        """
        preset = str(preset or "").strip().casefold()
        if preset not in {"strict", "progressive"}:
            raise ValueError("Unsupported security preset.")

        active = await self.repository.get_active_community_policy(chat_id)
        rules = parse_rules_json(active.rules_json if active is not None else "[]")
        remaining = [
            rule for rule in rules
            if rule.policy_family != "security_fraud"
        ]

        if preset == "progressive":
            remaining.append(CommunityPolicyRule(
                rule_id="",
                source="manual",
                source_ref=None,
                title="Security · Progressive",
                condition=(
                    "Confirmed scam, phishing, malicious-link, wallet-drain or "
                    "security_fraud violation by this user in this community."
                ),
                action="mute",
                enforcement_tier="medium",
                policy_family="security_fraud",
                exceptions=[],
            ))
            summary = (
                "Security preset: first unique confirmed security_fraud offense "
                "MUTE + DELETE; repeat unique offense BAN + DELETE."
            )
        else:
            summary = (
                "Security preset: Strict Core. Community security_fraud "
                "overrides removed; Protected Core enforcement is unchanged."
            )

        normalized = self._renumber_rules(remaining)
        version = await self.repository.activate_community_policy_version(
            chat_id=int(chat_id),
            admin_id=int(admin_id),
            source_text=f"Security preset: {preset}.",
            rules_json=rules_json(normalized),
            summary=summary,
        )
        logger.info(
            "COMMUNITY SECURITY PRESET | chat=%s | admin=%s | preset=%s | version=%s",
            chat_id,
            admin_id,
            preset,
            version.version,
        )
        return version

    async def rollback_previous(
        self,
        *,
        chat_id: int,
        admin_id: int,
    ):
        version = (
            await self.repository
            .rollback_community_policy(
                chat_id=chat_id,
                admin_id=admin_id,
            )
        )

        if version is not None:
            logger.info(
                "COMMUNITY POLICY ROLLBACK | "
                "chat=%s | new_version=%s | admin=%s",
                chat_id,
                version.version,
                admin_id,
            )

        return version

    async def clear_custom_rules(
        self,
        *,
        chat_id: int,
        admin_id: int,
    ):
        await self.repository.clear_community_policy_draft(
            admin_id
        )

        version = (
            await self.repository
            .clear_custom_community_policy(
                chat_id=chat_id,
                admin_id=admin_id,
            )
        )

        logger.info(
            "COMMUNITY POLICY CLEAR | "
            "chat=%s | version=%s | admin=%s",
            chat_id,
            version.version,
            admin_id,
        )

        return version

    @staticmethod
    def _renumber_rules(
        rules: list[CommunityPolicyRule],
    ) -> list[CommunityPolicyRule]:
        return [
            rule.model_copy(
                update={
                    "rule_id": f"R{index}",
                    "mute_minutes": None,
                }
            )
            for index, rule in enumerate(rules[:20], start=1)
        ]

    async def _review_protected_shadow_feedback(self, *, case):
        """Re-check the original message before a learned rule can weaken policy."""

        previous_category = str(getattr(case, "ai_category", "") or "").strip().lower()

        # Preserve an already-protected earlier classification even if the second
        # model is temporarily less certain. The second review exists mainly to
        # catch false-safe labels such as phishing misclassified as spam.
        if previous_category in PROTECTED_CORE_CATEGORIES:
            previous_protected = previous_category
        else:
            previous_protected = None

        try:
            review = await self.deep_provider.generate_structured(
                system_prompt=PROTECTED_FEEDBACK_REVIEW_SYSTEM_PROMPT,
                user_prompt=protected_feedback_review_prompt(case=case),
                response_model=ProtectedFeedbackSafetyReview,
            )
        except Exception:
            logger.exception(
                "PROTECTED FEEDBACK REVIEW FAILED | chat=%s | case=%s",
                getattr(case, "chat_id", None),
                getattr(case, "id", None),
            )
            return previous_protected, False

        if (
            review.protected
            and review.category in PROTECTED_CORE_CATEGORIES
            and review.confidence >= 0.80
        ):
            category = review.category
        else:
            category = previous_protected

        logger.info(
            "PROTECTED FEEDBACK REVIEW | chat=%s | case=%s | protected=%s | "
            "category=%s | confidence=%.2f",
            getattr(case, "chat_id", None),
            getattr(case, "id", None),
            bool(category),
            category or "none",
            review.confidence,
        )
        return category, True

    @staticmethod
    def _policy_family_for_category(category: str | None) -> str:
        return POLICY_FAMILY_BY_CATEGORY.get(
            str(category or "").strip().lower(),
            "other",
        )

    @classmethod
    def _first_specific_policy_family(cls, *categories: str | None) -> str:
        """Return the first known moderation family, skipping SAFE/OTHER labels.

        Adaptive Shadow corrections that change an action to ALLOW are
        intentionally interpreted with category=safe. That must not erase the
        moderation topic being refined. For example, an ALLOW correction for a
        harassment/banter case still belongs to the harassment policy family.
        """
        for category in categories:
            family = cls._policy_family_for_category(category)
            if family != "other":
                return family
        return "other"

    @classmethod
    def _policy_family_for_shadow_case(
        cls,
        case,
        *,
        protected_category: str | None = None,
    ) -> str:
        return cls._first_specific_policy_family(
            protected_category,
            getattr(case, "corrected_category", None),
            getattr(case, "ai_category", None),
        )

    async def _effective_rule_family(self, rule: CommunityPolicyRule) -> str:
        """Best-effort family recovery for rules saved before v1.4.4/v1.4.5."""
        if rule.policy_family != "other":
            return rule.policy_family
        if rule.source != "shadow_feedback" or not rule.source_ref:
            return "other"
        try:
            previous_case = await self.repository.get_shadow_feedback_case(
                int(rule.source_ref)
            )
        except Exception:
            logger.exception(
                "LEARNED RULE FAMILY LOOKUP FAILED | rule=%s | source_ref=%s",
                rule.rule_id,
                rule.source_ref,
            )
            return "other"
        if previous_case is None:
            return "other"
        # ALLOW feedback is stored with corrected_category=safe by design.
        # Fall back to the original AI category so legacy banter rules recover
        # as harassment instead of becoming the generic `other` family.
        return self._policy_family_for_shadow_case(previous_case)

    async def _family_compatible_rules(
        self,
        *,
        context: MessageContext,
        rules: list[CommunityPolicyRule],
        baseline_decision: ModerationDecision,
    ) -> list[CommunityPolicyRule]:
        """Hard isolation between explicit punitive policy families.

        Semantic matching may choose within one moderation topic, but an
        explicit security_fraud ladder must never consume harassment history
        (and vice versa). Generic legacy/manual rules with family=other stay
        eligible for backwards compatibility and intentionally broad policies.
        SAFE/OTHER Core results keep the broad matcher so a custom rule may
        still classify an otherwise-safe message.
        """
        baseline_family = self._policy_family_for_category(
            baseline_decision.category
        )
        if baseline_family == "other":
            return rules

        compatible: list[CommunityPolicyRule] = []
        rejected: list[str] = []
        for rule in rules:
            family = await self._effective_rule_family(rule)
            if family in {baseline_family, "other"}:
                compatible.append(rule)
            else:
                rejected.append(f"{rule.rule_id}:{family}")

        if rejected:
            logger.info(
                "COMMUNITY POLICY FAMILY GATE | chat=%s | category=%s | "
                "family=%s | rejected=%s",
                context.current_message.chat_id,
                baseline_decision.category,
                baseline_family,
                rejected,
            )
        return compatible

    async def _runtime_protected_guard(
        self,
        *,
        context: MessageContext,
        baseline_decision: ModerationDecision,
        baseline_policy: PolicyEvaluation,
        policy_gate: PolicyGate,
    ) -> tuple[ModerationDecision, PolicyEvaluation]:
        """Independent security re-check before final enforcement.

        This is deliberately narrow: normal links are not auto-classified as
        malicious. The extra Deep review only runs for domain-bearing messages
        that are already suspicious/violating, plus obfuscated-domain messages.
        """
        if (
            baseline_decision.current_message_violation
            and baseline_decision.category in PROTECTED_CORE_CATEGORIES
        ):
            return baseline_decision, baseline_policy

        behavior = context.behavior_signals
        has_domain_signal = bool(behavior.domain_references)
        needs_review = bool(behavior.has_obfuscated_domains) or (
            has_domain_signal
            and (
                baseline_decision.current_message_violation
                or baseline_decision.action != "allow"
                or baseline_decision.category in {"spam", "other"}
            )
        )
        if not needs_review:
            return baseline_decision, baseline_policy

        try:
            review = await self.deep_provider.generate_structured(
                system_prompt=PROTECTED_RUNTIME_REVIEW_SYSTEM_PROMPT,
                user_prompt=protected_runtime_review_prompt(
                    context=context,
                    decision=baseline_decision,
                ),
                response_model=ProtectedFeedbackSafetyReview,
            )
        except Exception:
            logger.exception(
                "PROTECTED RUNTIME REVIEW FAILED | chat=%s | message=%s",
                context.current_message.chat_id,
                context.current_message.telegram_message_id,
            )
            return baseline_decision, baseline_policy

        logger.info(
            "PROTECTED RUNTIME REVIEW | chat=%s | message=%s | protected=%s | "
            "category=%s | confidence=%.2f | previous=%s/%s",
            context.current_message.chat_id,
            context.current_message.telegram_message_id,
            review.protected,
            review.category,
            review.confidence,
            baseline_decision.category,
            baseline_policy.final_action,
        )

        if not (
            review.protected
            and review.category in PROTECTED_CORE_CATEGORIES
            and review.confidence >= 0.80
        ):
            return baseline_decision, baseline_policy

        evidence = list(review.current_message_evidence[:5])
        if not evidence and context.current_message.raw_text.strip():
            evidence = [context.current_message.raw_text.strip()[:500]]

        guarded_decision = baseline_decision.model_copy(
            update={
                "current_message_violation": True,
                "category": review.category,
                "severity": "high",
                # Protected enforcement confidence belongs to the independent
                # security review, not the earlier spam/other classifier.
                "confidence": float(review.confidence),
                "action": "ban",
                "delete_message": True,
                "mute_minutes": None,
                "needs_human_review": False,
                "reason": (
                    "Protected runtime review: " + review.reason.strip()
                )[:800],
                "current_message_evidence": evidence,
            }
        )
        guarded_policy = policy_gate.evaluate(
            guarded_decision,
            context=context,
        )

        logger.info(
            "PROTECTED RUNTIME GUARD | chat=%s | message=%s | previous=%s/%s | "
            "protected=%s | final=%s | confidence=%.2f",
            context.current_message.chat_id,
            context.current_message.telegram_message_id,
            baseline_decision.category,
            baseline_policy.final_action,
            review.category,
            guarded_policy.final_action,
            guarded_decision.confidence,
        )
        return guarded_decision, guarded_policy

    async def apply_core_guard(
        self,
        *,
        context: MessageContext,
        baseline_decision: ModerationDecision,
        baseline_policy: PolicyEvaluation,
        policy_gate: PolicyGate,
    ) -> tuple[ModerationDecision, PolicyEvaluation]:
        """Return the protected-Core baseline that must be shown in UI/audit.

        Community Policy is applied only *after* this baseline is frozen. This
        keeps Shadow cards truthful: if the independent runtime guard upgrades
        spam/warn to scam/ban, "AI/Core recommendation" must show BAN + DELETE
        even when a community rule later relaxes the effective action.
        """
        return await self._runtime_protected_guard(
            context=context,
            baseline_decision=baseline_decision,
            baseline_policy=baseline_policy,
            policy_gate=policy_gate,
        )

    async def _reconcile_learned_rule(
        self,
        *,
        rules: list[CommunityPolicyRule],
        new_condition: str,
        new_action: str,
        new_policy_family: str,
        case,
    ) -> tuple[str, set[str], set[str]]:
        """Decide whether a confirmed correction adds or replaces a policy topic."""

        if not rules:
            return "add", set(), set()

        family_by_id: dict[str, str] = {}
        for rule in rules:
            family_by_id[rule.rule_id] = await self._effective_rule_family(rule)

        # Learned-rule replacement is strictly family-scoped. Manual legacy
        # rules with family=other are still shown to the reconciler so a human
        # rule can conservatively block automatic learning rather than be lost.
        candidate_rules = [
            rule
            for rule in rules
            if (
                rule.source == "manual"
                and family_by_id.get(rule.rule_id) in {new_policy_family, "other"}
            )
            or (
                rule.source == "shadow_feedback"
                and family_by_id.get(rule.rule_id) == new_policy_family
            )
        ]

        if not candidate_rules:
            return "add", set(), set()

        try:
            result = await self.deep_provider.generate_structured(
                system_prompt=LEARNED_RULE_RECONCILIATION_SYSTEM_PROMPT,
                user_prompt=learned_rule_reconciliation_prompt(
                    existing_rules=candidate_rules,
                    new_condition=new_condition,
                    new_action=new_action,
                    new_policy_family=new_policy_family,
                ),
                response_model=LearnedRuleReconciliation,
            )
        except Exception:
            logger.exception(
                "LEARNED RULE RECONCILIATION FAILED | chat=%s | case=%s",
                getattr(case, "chat_id", None),
                getattr(case, "id", None),
            )
            return "uncertain", set(), set()

        existing_by_id = {rule.rule_id: rule for rule in rules}
        replace_ids = {
            rule_id
            for rule_id in result.replace_rule_ids
            if (
                rule_id in existing_by_id
                and existing_by_id[rule_id].source == "shadow_feedback"
                and family_by_id.get(rule_id) == new_policy_family
            )
        }
        manual_ids = {
            rule_id
            for rule_id in result.manual_conflict_rule_ids
            if (
                rule_id in existing_by_id
                and existing_by_id[rule_id].source == "manual"
                and family_by_id.get(rule_id) in {new_policy_family, "other"}
            )
        }

        # The reconciler sometimes correctly says REPLACE but omits the rule id.
        # If there is exactly one learned candidate in the same policy family,
        # infer that target instead of degrading to `uncertain`. We do NOT guess
        # when multiple same-family learned topics exist.
        same_family_learned = [
            rule
            for rule in candidate_rules
            if (
                rule.source == "shadow_feedback"
                and family_by_id.get(rule.rule_id) == new_policy_family
            )
        ]
        if (
            result.operation == "replace"
            and result.confidence >= 0.85
            and not replace_ids
            and len(same_family_learned) == 1
        ):
            inferred = same_family_learned[0].rule_id
            replace_ids = {inferred}
            logger.info(
                "LEARNED RULE RECONCILE TARGET INFERRED | chat=%s | case=%s | "
                "family=%s | rule=%s",
                getattr(case, "chat_id", None),
                getattr(case, "id", None),
                new_policy_family,
                inferred,
            )

        if result.confidence < 0.85:
            operation = "uncertain"
        elif manual_ids or result.operation == "manual_conflict":
            operation = "manual_conflict"
        elif result.operation == "replace" and replace_ids:
            operation = "replace"
        elif result.operation == "add":
            operation = "add"
        else:
            operation = "uncertain"

        logger.info(
            "LEARNED RULE RECONCILE | chat=%s | case=%s | operation=%s | "
            "family=%s | replace=%s | manual_conflict=%s | confidence=%.2f",
            getattr(case, "chat_id", None),
            getattr(case, "id", None),
            operation,
            new_policy_family,
            sorted(replace_ids),
            sorted(manual_ids),
            result.confidence,
        )
        return operation, replace_ids, manual_ids

    @staticmethod
    def _normalize_learned_condition(
        condition: str,
        *,
        action: str,
        policy_family: str,
    ) -> str:
        """Normalize free-text learned rules into stable runtime semantics.

        Shadow feedback is written by an LLM, so wording such as "first
        occurrence in the community" must not accidentally imply a global
        one-shot counter. Tier escalation is tracked per offending user.
        Learned harassment ALLOW exceptions also receive a mandatory stop/
        one-sided-escalation boundary.
        """
        text = str(condition or "").strip()
        text = re.sub(
            r"\bfirst (?:such )?occurrence in (?:the )?community\b",
            "first confirmed offense by the same user",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bfirst (?:such )?offense in (?:the )?community\b",
            "first confirmed offense by the same user",
            text,
            flags=re.IGNORECASE,
        )

        if action == "allow" and policy_family == "harassment":
            guard = (
                "This exception applies only while the exchange is clearly "
                "reciprocal and playful; it does not apply after a clear "
                "request to stop, one-sided degradation, threats, or escalation."
            )
            lower = text.casefold()
            if "request to stop" not in lower and "asks to stop" not in lower:
                text = (text.rstrip(" .") + ". " + guard).strip()

        return text[:600]

    async def promote_shadow_feedback(
        self,
        *,
        case,
        admin_id: int,
    ) -> tuple[object | None, str]:
        """
        Promote one confirmed generic Shadow correction into this chat's active
        Community Policy. Before promotion, independently re-check Protected Core
        safety and reconcile the new learned rule with existing policy topics.

        Pair-specific relationship feedback remains pair-scoped soft memory.
        Manual administrator rules are never silently overwritten by learning.
        """

        corrected_action = str(
            getattr(case, "corrected_action", "") or ""
        ).strip().lower()
        local_rule = str(
            getattr(case, "local_rule", "") or ""
        ).strip()

        if not corrected_action or not local_rule:
            return None, "audit_only"

        if bool(getattr(case, "apply_to_same_pair", False)):
            return None, "pair_memory"

        # v1.4.3: never decide Protected Core safety solely from the earlier AI
        # category. A separate Deep review re-reads the original message. This
        # catches cases where phishing/scam was initially mislabeled as spam.
        protected_category, safety_review_ok = (
            await self._review_protected_shadow_feedback(case=case)
        )

        if not safety_review_ok and corrected_action != "ban":
            # Fail closed for learned policy weakening. The moderator's feedback
            # remains stored for audit, but no auto-policy mutation occurs.
            return None, "safety_review_failed"

        if protected_category in PROTECTED_CORE_CATEGORIES:
            floor_action = PROTECTED_ENFORCEMENT_FLOOR.get(
                protected_category,
                "ban",
            )
            if ACTION_RANK.get(corrected_action, -1) < ACTION_RANK[floor_action]:
                logger.info(
                    "SHADOW POLICY PROMOTION BLOCKED | chat=%s | case=%s | "
                    "protected=%s | requested=%s | floor=%s",
                    getattr(case, "chat_id", None),
                    getattr(case, "id", None),
                    protected_category,
                    corrected_action,
                    floor_action,
                )
                return None, "protected_audit_only"

        new_policy_family = self._policy_family_for_shadow_case(
            case,
            protected_category=protected_category,
        )
        local_rule = self._normalize_learned_condition(
            local_rule,
            action=corrected_action,
            policy_family=new_policy_family,
        )

        active = await self.repository.get_active_community_policy(
            int(case.chat_id)
        )
        rules = parse_rules_json(
            active.rules_json if active is not None else "[]"
        )

        # Re-saving the exact same feedback id replaces its prior generated rule
        # before semantic reconciliation. This keeps callbacks idempotent.
        rules = [
            rule
            for rule in rules
            if not (
                rule.source == "shadow_feedback"
                and rule.source_ref == int(case.id)
            )
        ]

        operation, replace_rule_ids, manual_conflict_ids = (
            await self._reconcile_learned_rule(
                rules=rules,
                new_condition=local_rule,
                new_action=corrected_action,
                new_policy_family=new_policy_family,
                case=case,
            )
        )

        if operation == "manual_conflict":
            logger.info(
                "SHADOW POLICY PROMOTION BLOCKED | chat=%s | case=%s | "
                "reason=manual_rule_conflict | rules=%s",
                getattr(case, "chat_id", None),
                getattr(case, "id", None),
                sorted(manual_conflict_ids),
            )
            return None, "manual_conflict"

        if operation == "uncertain":
            return None, "reconcile_uncertain"

        promotion = "policy"
        if operation == "replace":
            rules = [
                rule for rule in rules
                if rule.rule_id not in replace_rule_ids
            ]
            promotion = "policy_replaced"

        if len(rules) >= 20:
            # Never evict a manually-authored rule. Prefer replacing the oldest
            # learned Shadow rule; otherwise keep the feedback as soft memory.
            learned_index = next(
                (
                    index
                    for index, rule in enumerate(rules)
                    if rule.source == "shadow_feedback"
                ),
                None,
            )
            if learned_index is None:
                return None, "policy_full"
            rules.pop(learned_index)

        # ALLOW is an explicit exception, not a progression tier. Giving an
        # ALLOW rule the LIGHT tier would turn it back into WARN at runtime.
        tier = {
            "allow": None,
            "warn": "light",
            "escalate": "medium",
            "delete": "medium",
            "mute": "medium",
            "ban": "heavy",
        }.get(corrected_action)

        rule = CommunityPolicyRule(
            rule_id="",
            source="shadow_feedback",
            source_ref=int(case.id),
            title=f"Shadow feedback #{int(case.id)}",
            condition=local_rule[:600],
            action=corrected_action,
            enforcement_tier=tier,
            policy_family=new_policy_family,
            exceptions=[],
        )
        rules.append(rule)
        rules = self._renumber_rules(rules)

        if promotion == "policy_replaced":
            summary = (
                f"Adaptive Shadow feedback #{int(case.id)} replaced older learned "
                "rule(s) for the same policy topic."
            )
        else:
            summary = (
                f"Adaptive Shadow feedback #{int(case.id)} promoted to "
                "Community Policy."
            )

        version = await self.repository.activate_community_policy_version(
            chat_id=int(case.chat_id),
            admin_id=int(admin_id),
            source_text=(
                f"Adaptive Shadow feedback #{int(case.id)}: "
                + (
                    str(getattr(case, "moderator_explanation", "") or "")
                    .strip()[:1200]
                )
            ),
            rules_json=rules_json(rules),
            summary=summary,
        )
        return version, promotion

    async def reset_adaptive_memory(
        self,
        *,
        chat_id: int,
        admin_id: int,
    ) -> tuple[object | None, int]:
        """
        Remove learned Shadow policy rules while preserving manual custom rules,
        and revoke confirmed Shadow feedback so it no longer affects decisions.
        """

        active = await self.repository.get_active_community_policy(chat_id)
        version = None

        if active is not None:
            rules = parse_rules_json(active.rules_json)
            remaining = [
                rule
                for rule in rules
                if rule.source != "shadow_feedback"
            ]
            if len(remaining) != len(rules):
                remaining = self._renumber_rules(remaining)
                version = await self.repository.activate_community_policy_version(
                    chat_id=chat_id,
                    admin_id=admin_id,
                    source_text="Reset Adaptive Shadow memory.",
                    rules_json=rules_json(remaining),
                    summary=(
                        "Adaptive Shadow learned rules removed; manual custom "
                        "rules preserved."
                    ),
                )

        revoked = await self.repository.revoke_confirmed_shadow_feedback(
            chat_id=chat_id
        )
        return version, revoked

    async def remove_rule(
        self,
        *,
        chat_id: int,
        admin_id: int,
        rule_id: str,
    ):
        active = await self.repository.get_active_community_policy(chat_id)
        if active is None:
            return None

        rules = parse_rules_json(active.rules_json)
        remaining = [
            rule for rule in rules if rule.rule_id != rule_id
        ]
        if len(remaining) == len(rules):
            return None

        remaining = self._renumber_rules(remaining)
        version = await self.repository.activate_community_policy_version(
            chat_id=chat_id,
            admin_id=admin_id,
            source_text=f"Removed Community Policy rule {rule_id}.",
            rules_json=rules_json(remaining),
            summary=f"Removed rule {rule_id}; remaining rules preserved.",
        )
        return version

    async def _match(
        self,
        *,
        context: MessageContext,
        rules: list[CommunityPolicyRule],
    ) -> CommunityPolicyMatch | None:
        if not rules:
            return None

        current_text = (
            context.current_message.raw_text
            .strip()
        )

        # Very short messages are disproportionately easy for the tiny Fast
        # matcher to false-negative ("бля", "фак", short slang, abbreviations).
        # This is only DEPTH ROUTING, never semantic enforcement.
        #
        # Custom policy is relatively rare and correctness matters more than
        # saving one Deep call for a 1-3 token message.
        short_semantic_case = (
            len(current_text) <= 32
            or len(current_text.split()) <= 3
        )

        if (
            self.fast_provider is not None
            and not short_semantic_case
        ):
            try:
                started = time.perf_counter()

                triage = (
                    await self.fast_provider
                    .generate_structured(
                        system_prompt=(
                            POLICY_TRIAGE_SYSTEM_PROMPT
                        ),
                        user_prompt=triage_prompt(
                            context=context,
                            rules=rules,
                        ),
                        response_model=(
                            CommunityPolicyTriage
                        ),
                    )
                )

                elapsed_ms = int(
                    (
                        time.perf_counter()
                        - started
                    )
                    * 1000
                )

                logger.debug(
                    "COMMUNITY POLICY TRIAGE | "
                    "chat=%s | route=%s | confidence=%.2f | ms=%s",
                    context.current_message.chat_id,
                    triage.route,
                    triage.confidence,
                    elapsed_ms,
                )

                if (
                    triage.route == "no_match"
                    and triage.confidence
                    >= 0.90
                ):
                    return None

            except Exception:
                # An overlay failure must NEVER damage the stable core.
                logger.exception(
                    "Community policy fast matcher failed; "
                    "falling back to deep matcher"
                )

        elif short_semantic_case:
            logger.debug(
                "COMMUNITY POLICY ROUTE | deep | "
                "reason=short_semantic_case | chat=%s | message=%s",
                context.current_message.chat_id,
                context.current_message.telegram_message_id,
            )

        started = time.perf_counter()

        match = (
            await self.deep_provider
            .generate_structured(
                system_prompt=(
                    POLICY_MATCH_SYSTEM_PROMPT
                ),
                user_prompt=match_prompt(
                    context=context,
                    rules=rules,
                ),
                response_model=(
                    CommunityPolicyMatch
                ),
            )
        )

        elapsed_ms = int(
            (
                time.perf_counter()
                - started
            )
            * 1000
        )

        logger.info(
            "COMMUNITY POLICY MATCH | "
            "chat=%s | matched=%s | rule=%s | "
            "confidence=%.2f | ambiguous=%s | ms=%s",
            context.current_message.chat_id,
            match.matched,
            match.winning_rule_id,
            match.confidence,
            match.ambiguous,
            elapsed_ms,
        )

        return match

    @staticmethod
    def _rule_history_actions(
        *,
        context: MessageContext,
        rule: CommunityPolicyRule,
        baseline_decision: ModerationDecision,
    ) -> list[str]:
        actions: list[str] = []

        for event in (context.user_moderation_history or []):
            reason = str(getattr(event, "reason", "") or "")
            category = getattr(event, "category", None)

            same_rule = (
                f"Custom rule {rule.rule_id}" in reason
                or f"Community policy" in reason and rule.rule_id in reason
            )
            same_category = (
                baseline_decision.category not in {"safe", "other"}
                and category == baseline_decision.category
            )

            if not (same_rule or same_category):
                continue

            action = getattr(event, "action", None)
            if action:
                actions.append(str(action))

        return actions

    async def _learned_allow_boundary_blocked(
        self,
        *,
        context: MessageContext,
        rule: CommunityPolicyRule,
    ) -> bool:
        """Guard learned harassment/banter ALLOW rules against stale consent.

        Only learned chat-local ALLOW rules in the harassment family use this
        additional semantic review. Manual rules keep explicit admin authority.
        On verifier failure we preserve Core rather than silently weakening it.
        """
        if not (
            rule.source == "shadow_feedback"
            and rule.action == "allow"
            and rule.policy_family == "harassment"
        ):
            return False

        try:
            review = await self.deep_provider.generate_structured(
                system_prompt=LEARNED_ALLOW_BOUNDARY_SYSTEM_PROMPT,
                user_prompt=learned_allow_boundary_prompt(
                    context=context,
                    rule=rule,
                ),
                response_model=LearnedAllowBoundaryReview,
            )
        except Exception:
            logger.exception(
                "Learned ALLOW boundary review failed; preserving Core | chat=%s | rule=%s",
                context.current_message.chat_id,
                rule.rule_id,
            )
            return True

        blocked = bool(review.blocked and review.confidence >= 0.75)
        logger.info(
            "LEARNED ALLOW BOUNDARY | chat=%s | rule=%s | blocked=%s | confidence=%.2f | reason=%s",
            context.current_message.chat_id,
            rule.rule_id,
            blocked,
            review.confidence,
            review.reason,
        )
        return blocked

    async def _shadow_simulation_actions(
        self,
        *,
        context: MessageContext,
        rule: CommunityPolicyRule,
    ) -> list[str]:
        """Return virtual SHADOW punishments for this user + policy family.

        Real moderation history intentionally excludes dry-run/Shadow events.
        This separate ledger lets policy ladders be exercised safely in Shadow
        without contaminating future LIVE punishment history.
        """
        current = context.current_message
        if current.user_id is None or (rule.policy_family or "other") == "other":
            return []

        settings_getter = getattr(self.repository, "get_chat_settings", None)
        actions_getter = getattr(
            self.repository, "get_shadow_simulation_actions", None
        )
        if not callable(settings_getter) or not callable(actions_getter):
            return []

        try:
            settings = await settings_getter(current.chat_id)
            if not bool(getattr(settings, "shadow_mode", False)):
                return []
            lookup_kwargs = {
                "chat_id": current.chat_id,
                "target_user_id": current.user_id,
                "policy_family": (rule.policy_family or "other"),
                "limit": 20,
            }
            current_message_id = getattr(
                current,
                "telegram_message_id",
                None,
            )
            if current_message_id is not None:
                lookup_kwargs["exclude_telegram_message_id"] = (
                    current_message_id
                )

            try:
                actions = await actions_getter(**lookup_kwargs)
            except TypeError as exc:
                # Compatibility for lightweight test doubles or third-party
                # repositories that predate v1.4.12. Production
                # ControlRepository supports the exclusion argument.
                if (
                    "exclude_telegram_message_id" not in lookup_kwargs
                    or "exclude_telegram_message_id" not in str(exc)
                ):
                    raise
                lookup_kwargs.pop("exclude_telegram_message_id", None)
                actions = await actions_getter(**lookup_kwargs)

            return list(actions)
        except Exception:
            # Shadow simulation is an optional pilot aid. Any lookup failure
            # must preserve the stable policy path instead of blocking moderation.
            logger.exception(
                "Shadow simulation history lookup failed | chat=%s | user=%s | family=%s",
                current.chat_id,
                current.user_id,
                rule.policy_family,
            )
            return []

    def _tier_action(
        self,
        *,
        rule: CommunityPolicyRule,
        context: MessageContext,
        baseline_decision: ModerationDecision,
        shadow_history_actions: list[str] | None = None,
    ) -> str:
        # ALLOW must remain ALLOW even for legacy learned rules that were
        # accidentally stored with enforcement_tier=light before v1.4.6.
        if rule.action == "allow":
            return "allow"

        tier = rule.enforcement_tier
        if tier is None:
            return rule.action

        history = self._rule_history_actions(
            context=context,
            rule=rule,
            baseline_decision=baseline_decision,
        )
        # SHADOW uses an isolated virtual ledger. It is deliberately merged
        # only for action selection and never written into real reputation.
        history.extend(shadow_history_actions or [])

        if tier == "heavy":
            return "ban"

        if tier == "medium":
            return "ban" if "mute" in history else "mute"

        # light
        if "mute" in history:
            return "ban"
        if any(action in {"warn", "delete"} for action in history):
            return "mute"
        return "warn"

    def _overlay_evaluation(
        self,
        *,
        rule: CommunityPolicyRule,
        match: CommunityPolicyMatch,
        version: int,
        policy_gate: PolicyGate,
        context: MessageContext,
        baseline_decision: ModerationDecision,
        shadow_history_actions: list[str] | None = None,
    ) -> PolicyEvaluation:
        action = self._tier_action(
            rule=rule,
            context=context,
            baseline_decision=baseline_decision,
            shadow_history_actions=shadow_history_actions,
        )
        confidence = match.confidence
        uncertain_floor = 0.65

        if action == "allow":
            final_action = "allow"
            human = False
            autonomous = True

        elif match.ambiguous:
            if confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        elif action == "warn":
            if confidence >= policy_gate.warn_threshold:
                final_action = "warn"
                human = False
                autonomous = True
            elif confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        elif action == "delete":
            if confidence >= policy_gate.delete_threshold:
                final_action = "delete"
                human = False
                autonomous = True
            elif confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        elif action == "mute":
            if confidence >= policy_gate.mute_threshold:
                final_action = "mute"
                human = False
                autonomous = True
            elif confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        elif action == "ban":
            if confidence >= policy_gate.ban_threshold:
                final_action = "ban"
                human = False
                autonomous = True
            elif confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        else:  # explicit custom escalate
            if confidence >= uncertain_floor:
                final_action = "escalate"
                human = True
                autonomous = False
            else:
                final_action = "allow"
                human = False
                autonomous = True

        return PolicyEvaluation(
            original_action=action,
            final_action=final_action,
            final_delete_message=(
                final_action
                in {
                    "delete",
                    "mute",
                    "ban",
                }
            ),
            final_mute_minutes=None,
            autonomous=autonomous,
            requires_human_review=human,
            policy_reason=(
                f"Community policy v{version} · "
                f"{rule.rule_id} · {rule.title}"
                f"{(' · ' + rule.enforcement_tier.upper()) if rule.enforcement_tier else ''}: "
                f"{match.reason}"
            )[:1000],
            source="community_policy",
            community_policy_version=version,
            matched_community_rules=(
                match.matched_rule_ids
                or [rule.rule_id]
            ),
            community_policy_family=(rule.policy_family or "other"),
        )

    def _custom_decision(
        self,
        *,
        baseline_decision: ModerationDecision,
        overlay: PolicyEvaluation,
        rule: CommunityPolicyRule,
        match: CommunityPolicyMatch,
    ) -> ModerationDecision:
        severity_by_action = {
            "allow": "none",
            "warn": "low",
            "escalate": "medium",
            "delete": "medium",
            "mute": "high",
            "ban": "high",
        }

        category = (
            baseline_decision.category
            if baseline_decision.category
            != "safe"
            else "other"
        )

        evidence = list(
            match.current_message_evidence
        )[:5]

        if (
            not evidence
            and match.reason.strip()
        ):
            evidence = [
                (
                    "Community policy semantic match: "
                    + match.reason.strip()
                )[:500]
            ]

        return ModerationDecision(
            detected_language=(
                baseline_decision.detected_language
            ),
            current_message_violation=(
                overlay.final_action
                != "allow"
            ),
            category=category,
            severity=severity_by_action[
                overlay.final_action
            ],
            confidence=match.confidence,
            action=overlay.final_action,
            delete_message=(
                overlay.final_delete_message
            ),
            mute_minutes=(
                overlay.final_mute_minutes
            ),
            needs_human_review=(
                overlay.requires_human_review
            ),
            reason=(
                f"Custom rule {rule.rule_id} "
                f"({rule.title}): {match.reason}"
            )[:800],
            current_message_evidence=evidence,
            context_evidence=(
                baseline_decision
                .context_evidence[:3]
            ),
            report_target=(
                baseline_decision.report_target
            ),
            report_confidence=(
                baseline_decision
                .report_confidence
            ),
            report_reason=(
                baseline_decision.report_reason
            ),
        )

    async def apply_overlay(
        self,
        *,
        context: MessageContext,
        baseline_decision: ModerationDecision,
        baseline_policy: PolicyEvaluation,
        policy_gate: PolicyGate,
        core_guard_applied: bool = False,
    ) -> tuple[
        ModerationDecision,
        PolicyEvaluation,
    ]:
        """
        Apply per-chat custom policy.

        Protected Core categories cannot be weakened. Configurable community
        behavior may be relaxed or strengthened intentionally by the admin.
        Any optional-layer failure still preserves the stable Core result.
        """

        # Runtime security guard runs even when no custom policy exists. It
        # independently catches protected scam/phishing semantics that the main
        # model may have mislabeled as ordinary spam before PolicyGate finalizes
        # a weak spam ladder action.
        if not core_guard_applied:
            baseline_decision, baseline_policy = await self._runtime_protected_guard(
                context=context,
                baseline_decision=baseline_decision,
                baseline_policy=baseline_policy,
                policy_gate=policy_gate,
            )

        # Protected Core classification is non-disableable, but enforcement for
        # scam/phishing/malicious-link may be tuned to a safe MUTE + DELETE floor.
        # Therefore custom-policy matching still runs for protected cases; the
        # floor guard below prevents unsafe ALLOW/WARN/DELETE downgrades.

        try:
            active = (
                await self.repository
                .get_active_community_policy(
                    context.current_message.chat_id
                )
            )

            if active is None:
                return (
                    baseline_decision,
                    baseline_policy,
                )

            rules = parse_rules_json(
                active.rules_json
            )

            if not rules:
                return (
                    baseline_decision,
                    baseline_policy,
                )

            protected_core = (
                baseline_decision.category in PROTECTED_CORE_CATEGORIES
                and baseline_decision.current_message_violation
            )
            if protected_core:
                floor_action = PROTECTED_ENFORCEMENT_FLOOR.get(
                    baseline_decision.category,
                    "ban",
                )
                floor_rank = ACTION_RANK[floor_action]
                rules = [
                    rule
                    for rule in rules
                    if rule.action != "allow"
                    and ACTION_RANK.get(rule.action, -1) >= floor_rank
                ]
                # Preserve the old fast path: if this community has no safe
                # protected-enforcement overrides, Core owns the decision and
                # no policy LLM call is needed.
                if not rules:
                    return (baseline_decision, baseline_policy)

            rules = await self._family_compatible_rules(
                context=context,
                rules=rules,
                baseline_decision=baseline_decision,
            )
            if not rules:
                return (baseline_decision, baseline_policy)

            match = await self._match(
                context=context,
                rules=rules,
            )

            if (
                match is None
                or not match.matched
                or not match.winning_rule_id
            ):
                return (
                    baseline_decision,
                    baseline_policy,
                )

            by_id = {
                rule.rule_id: rule
                for rule in rules
            }

            rule = by_id.get(
                match.winning_rule_id
            )

            if rule is None:
                logger.warning(
                    "Community policy matcher returned unknown rule id | "
                    "chat=%s | rule=%s",
                    context.current_message.chat_id,
                    match.winning_rule_id,
                )
                return (
                    baseline_decision,
                    baseline_policy,
                )

            # A relaxing ALLOW exception must itself be a clear semantic
            # match. If it is ambiguous, preserve the already-computed Core
            # decision rather than weakening by uncertainty.
            if (
                rule.action == "allow"
                and (match.ambiguous or match.confidence < 0.85)
            ):
                return (baseline_decision, baseline_policy)

            # Learned friendly-banter exceptions are intentionally soft. If
            # nearby context shows that one participant withdrew consent or the
            # exchange shifted into real harassment, preserve Core instead of
            # letting an old ALLOW rule become permanent immunity.
            if (
                baseline_policy.final_action != "allow"
                and await self._learned_allow_boundary_blocked(
                    context=context,
                    rule=rule,
                )
            ):
                logger.info(
                    "COMMUNITY POLICY ALLOW BOUNDARY BLOCK | chat=%s | rule=%s | core=%s",
                    context.current_message.chat_id,
                    rule.rule_id,
                    baseline_policy.final_action,
                )
                return (baseline_decision, baseline_policy)

            shadow_history_actions = await self._shadow_simulation_actions(
                context=context,
                rule=rule,
            )

            overlay = self._overlay_evaluation(
                rule=rule,
                match=match,
                version=active.version,
                policy_gate=policy_gate,
                context=context,
                baseline_decision=baseline_decision,
                shadow_history_actions=shadow_history_actions,
            )

            if shadow_history_actions:
                logger.info(
                    "SHADOW POLICY LADDER | chat=%s | user=%s | family=%s | rule=%s | prior=%s | selected=%s",
                    context.current_message.chat_id,
                    context.current_message.user_id,
                    rule.policy_family,
                    rule.rule_id,
                    shadow_history_actions,
                    overlay.final_action,
                )

            overlay_rank = ACTION_RANK[
                overlay.final_action
            ]
            core_rank = ACTION_RANK[
                baseline_policy.final_action
            ]

            # Protected Core classification cannot be disabled. Enforcement may
            # only be relaxed to the category-specific safe floor.
            if protected_core:
                floor_action = PROTECTED_ENFORCEMENT_FLOOR.get(
                    baseline_decision.category,
                    "ban",
                )
                floor_rank = ACTION_RANK[floor_action]
                if overlay_rank < floor_rank:
                    logger.info(
                        "COMMUNITY POLICY CORE GUARD | chat=%s | rule=%s | "
                        "category=%s | core=%s | requested=%s | floor=%s",
                        context.current_message.chat_id,
                        rule.rule_id,
                        baseline_decision.category,
                        baseline_policy.final_action,
                        overlay.final_action,
                        floor_action,
                    )
                    return (baseline_decision, baseline_policy)

            # For configurable community behavior, a matched custom rule is an
            # intentional override and may be either stricter OR more relaxed.
            if overlay.final_action == "allow":
                if protected_core:
                    return (baseline_decision, baseline_policy)

                decision = self._custom_decision(
                    baseline_decision=baseline_decision,
                    overlay=overlay,
                    rule=rule,
                    match=match,
                )

                logger.info(
                    "COMMUNITY POLICY OVERRIDE | chat=%s | version=%s | "
                    "rule=%s | core=%s | overlay=allow | confidence=%.2f",
                    context.current_message.chat_id,
                    active.version,
                    rule.rule_id,
                    baseline_policy.final_action,
                    match.confidence,
                )
                return decision, overlay

            # Equal action on an explicit category may keep Core ownership.
            # For safe/other, keep custom ownership so the executor can enforce
            # the explicit community rule.
            if (
                overlay_rank == core_rank
                and baseline_decision.category not in {"safe", "other"}
                and rule.enforcement_tier is None
            ):
                return (baseline_decision, baseline_policy)

            decision = self._custom_decision(
                baseline_decision=(
                    baseline_decision
                ),
                overlay=overlay,
                rule=rule,
                match=match,
            )

            logger.info(
                "COMMUNITY POLICY ENFORCE | "
                "chat=%s | version=%s | rule=%s | "
                "core=%s | overlay=%s | confidence=%.2f",
                context.current_message.chat_id,
                active.version,
                rule.rule_id,
                baseline_policy.final_action,
                overlay.final_action,
                match.confidence,
            )

            return decision, overlay

        except Exception:
            logger.exception(
                "Community policy overlay failed; "
                "preserving stable core moderation result"
            )

            return (
                baseline_decision,
                baseline_policy,
            )
