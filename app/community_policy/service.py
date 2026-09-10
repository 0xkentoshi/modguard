import json
import logging
import time

from app.admin.control_repository import ControlRepository
from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)
from app.community_policy.core import PROTECTED_CORE_CATEGORIES
from app.community_policy.prompts import (
    POLICY_COMPILER_SYSTEM_PROMPT,
    POLICY_MATCH_SYSTEM_PROMPT,
    POLICY_TRIAGE_SYSTEM_PROMPT,
    compiler_prompt,
    match_prompt,
    triage_prompt,
)
from app.community_policy.schemas import (
    CommunityPolicyCompilation,
    CommunityPolicyMatch,
    CommunityPolicyRule,
    CommunityPolicyTriage,
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

    def _tier_action(
        self,
        *,
        rule: CommunityPolicyRule,
        context: MessageContext,
        baseline_decision: ModerationDecision,
    ) -> str:
        tier = rule.enforcement_tier
        if tier is None:
            return rule.action

        history = self._rule_history_actions(
            context=context,
            rule=rule,
            baseline_decision=baseline_decision,
        )

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
    ) -> PolicyEvaluation:
        action = self._tier_action(
            rule=rule,
            context=context,
            baseline_decision=baseline_decision,
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

        # Protected Core is intentionally non-configurable.
        # Do not even invoke the custom-policy matcher for an independently
        # confirmed protected violation. Besides preventing weakening, this
        # avoids extra LLM calls and stale/ambiguous custom matches.
        if (
            baseline_decision.category
            in PROTECTED_CORE_CATEGORIES
            and baseline_decision.current_message_violation
        ):
            return (
                baseline_decision,
                baseline_policy,
            )

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

            overlay = self._overlay_evaluation(
                rule=rule,
                match=match,
                version=active.version,
                policy_gate=policy_gate,
                context=context,
                baseline_decision=baseline_decision,
            )

            protected_core = (
                baseline_decision.category
                in PROTECTED_CORE_CATEGORIES
                and baseline_decision.current_message_violation
            )

            overlay_rank = ACTION_RANK[
                overlay.final_action
            ]
            core_rank = ACTION_RANK[
                baseline_policy.final_action
            ]

            # Protected Core can only stay equal or become stricter.
            if protected_core and overlay_rank < core_rank:
                logger.info(
                    "COMMUNITY POLICY CORE GUARD | chat=%s | rule=%s | "
                    "category=%s | core=%s | requested=%s",
                    context.current_message.chat_id,
                    rule.rule_id,
                    baseline_decision.category,
                    baseline_policy.final_action,
                    overlay.final_action,
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
