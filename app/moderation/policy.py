from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)


PROTECTED_HEAVY_CATEGORIES = {
    "scam",
    "phishing",
    "malicious_link",
    "threat",
}

LIGHT_LADDER_CATEGORIES = {
    "spam",
    "flood",
}

MEDIUM_CATEGORIES = {
    "unsolicited_advertising",
    "adult_content",
    "impersonation",
    "evasion_attempt",
    "hate",
}

DESTRUCTIVE_ACTIONS = {
    "delete",
    "mute",
    "ban",
}

SOFT_CONTEXTUAL_DELETE_CATEGORIES = {
    "other",
}

LIGHT_SPAM_CONFIDENCE = 0.90
LIGHT_HARASSMENT_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.90


class PolicyGate:
    def __init__(
        self,
        *,
        ban_threshold: float = 0.92,
        mute_threshold: float = 0.85,
        delete_threshold: float = 0.78,
        warn_threshold: float = 0.70,
    ):
        self.ban_threshold = ban_threshold
        self.mute_threshold = mute_threshold
        self.delete_threshold = delete_threshold
        self.warn_threshold = warn_threshold

    def _force_allow(self, decision: ModerationDecision, reason: str) -> PolicyEvaluation:
        return PolicyEvaluation(
            original_action=decision.action,
            final_action="allow",
            final_delete_message=False,
            final_mute_minutes=None,
            autonomous=True,
            requires_human_review=False,
            policy_reason=reason,
        )

    def _escalate(self, decision: ModerationDecision, reason: str) -> PolicyEvaluation:
        return PolicyEvaluation(
            original_action=decision.action,
            final_action="escalate",
            final_delete_message=False,
            final_mute_minutes=None,
            autonomous=False,
            requires_human_review=True,
            policy_reason=reason,
        )

    @staticmethod
    def _history_actions(
        context: MessageContext | None,
        *,
        categories: set[str],
    ) -> list[str]:
        if context is None:
            return []
        actions: list[str] = []
        for event in (context.user_moderation_history or []):
            if getattr(event, "category", None) not in categories:
                continue
            action = getattr(event, "action", None)
            if action:
                actions.append(str(action))
        return actions

    def _protected_heavy(
        self,
        decision: ModerationDecision,
    ) -> PolicyEvaluation | None:
        if decision.category not in PROTECTED_HEAVY_CATEGORIES:
            return None

        # A high-confidence protected threat skips warn/mute ladders entirely.
        if decision.confidence >= self.ban_threshold:
            if decision.category == "threat" and decision.severity not in {"high", "critical"}:
                return self._escalate(
                    decision,
                    "Threat category is not severe enough for an autonomous ban; human review required.",
                )
            return PolicyEvaluation(
                original_action=decision.action,
                final_action="ban",
                final_delete_message=True,
                final_mute_minutes=None,
                autonomous=True,
                requires_human_review=False,
                policy_reason=(
                    "HEAVY protected offense: high-confidence scam/phishing/"
                    "malicious-link/credible-threat path → BAN + DELETE."
                ),
            )

        if decision.confidence >= max(self.delete_threshold, 0.85):
            return PolicyEvaluation(
                original_action=decision.action,
                final_action="delete",
                final_delete_message=True,
                final_mute_minutes=None,
                autonomous=True,
                requires_human_review=False,
                policy_reason=(
                    "Protected high-risk offense is credible enough to remove, "
                    "but below the autonomous ban threshold."
                ),
            )

        return self._escalate(
            decision,
            "Protected high-risk category is not confident enough for autonomous enforcement.",
        )

    def _light_ladder(
        self,
        decision: ModerationDecision,
        *,
        context: MessageContext | None,
    ) -> PolicyEvaluation | None:
        category = decision.category
        if category not in LIGHT_LADDER_CATEGORIES:
            return None

        if category in {"spam", "flood"}:
            if decision.confidence < LIGHT_SPAM_CONFIDENCE:
                return None
            history_categories = {"spam", "flood"}
        else:
            if (
                decision.confidence < LIGHT_HARASSMENT_CONFIDENCE
                or decision.severity not in {"medium", "high", "critical"}
            ):
                return None
            history_categories = {"harassment"}

        history = self._history_actions(
            context,
            categories=history_categories,
        )

        if "mute" in history:
            return PolicyEvaluation(
                original_action=decision.action,
                final_action="ban",
                final_delete_message=True,
                final_mute_minutes=None,
                autonomous=True,
                requires_human_review=False,
                policy_reason=(
                    "LIGHT ladder: offense repeated after a confirmed mute → BAN + DELETE."
                ),
            )

        if any(action in {"warn", "delete"} for action in history):
            return PolicyEvaluation(
                original_action=decision.action,
                final_action="mute",
                final_delete_message=True,
                final_mute_minutes=None,
                autonomous=True,
                requires_human_review=False,
                policy_reason=(
                    "LIGHT ladder: repeated confirmed offense → MUTE + DELETE."
                ),
            )

        return PolicyEvaluation(
            original_action=decision.action,
            final_action="warn",
            final_delete_message=False,
            final_mute_minutes=None,
            autonomous=True,
            requires_human_review=False,
            policy_reason=(
                "LIGHT ladder: first clear offense → WARN without deleting the message."
            ),
        )

    def _harassment_enforcement(
        self,
        decision: ModerationDecision,
        *,
        context: MessageContext | None,
    ) -> PolicyEvaluation | None:
        if decision.category != "harassment":
            return None

        if (
            decision.confidence < LIGHT_HARASSMENT_CONFIDENCE
            or decision.severity not in {"medium", "high", "critical"}
        ):
            return self._escalate(
                decision,
                "Human conflict is context-heavy and not confident enough for automatic enforcement.",
            )

        history = self._history_actions(
            context,
            categories={"harassment"},
        )
        if any(action in {"warn", "delete", "mute", "ban"} for action in history):
            return self._escalate(
                decision,
                "Repeated harassment/fight after a confirmed prior action → moderator Ticket; no autonomous mute/ban by default.",
            )

        return PolicyEvaluation(
            original_action=decision.action,
            final_action="warn",
            final_delete_message=False,
            final_mute_minutes=None,
            autonomous=True,
            requires_human_review=False,
            policy_reason=(
                "Human-conflict safety boundary: first clear targeted harassment → WARN; message remains."
            ),
        )

    def _medium_enforcement(
        self,
        decision: ModerationDecision,
        *,
        context: MessageContext | None,
    ) -> PolicyEvaluation | None:
        if (
            decision.category not in MEDIUM_CATEGORIES
            or decision.confidence < MEDIUM_CONFIDENCE
            or decision.severity not in {"medium", "high", "critical"}
        ):
            return None

        history = self._history_actions(
            context,
            categories={decision.category},
        )

        if "mute" in history:
            return PolicyEvaluation(
                original_action=decision.action,
                final_action="ban",
                final_delete_message=True,
                final_mute_minutes=None,
                autonomous=True,
                requires_human_review=False,
                policy_reason=(
                    "MEDIUM ladder: offense repeated after a confirmed mute → BAN + DELETE."
                ),
            )

        return PolicyEvaluation(
            original_action=decision.action,
            final_action="mute",
            final_delete_message=True,
            final_mute_minutes=None,
            autonomous=True,
            requires_human_review=False,
            policy_reason=(
                "MEDIUM offense: immediate MUTE + DELETE."
            ),
        )

    def evaluate(
        self,
        decision: ModerationDecision,
        *,
        context: MessageContext | None = None,
    ) -> PolicyEvaluation:
        action = decision.action

        # Absolute anti-history-punishment gate.
        if not decision.current_message_violation:
            return self._force_allow(
                decision,
                "Current-message gate: the current message does not violate policy.",
            )

        if decision.category == "safe":
            return self._force_allow(
                decision,
                "Current-message gate: category is safe.",
            )

        # Destructive actions require current-message evidence. Objective
        # spam/flood repetition may substitute for textual evidence.
        if action in DESTRUCTIVE_ACTIONS and not decision.current_message_evidence:
            behavior_supports_spam = False
            if context is not None and decision.category in {"spam", "flood"}:
                behavior = context.behavior_signals
                behavior_supports_spam = (
                    behavior.repeated_recent_messages >= 1
                    or behavior.messages_last_60s >= 8
                )
            if not behavior_supports_spam:
                return self._escalate(
                    decision,
                    "Evidence gate: destructive action lacks current-message evidence.",
                )

        # Genuine uncertainty always becomes a moderator Ticket. This is
        # especially important for multi-party fights where the instigator is
        # unclear from one message.
        if decision.needs_human_review or action == "escalate":
            return self._escalate(
                decision,
                "LLM explicitly requested human review.",
            )

        protected = self._protected_heavy(decision)
        if protected is not None:
            return protected

        if action == "allow":
            return self._force_allow(decision, "LLM requested allow.")

        harassment = self._harassment_enforcement(decision, context=context)
        if harassment is not None:
            return harassment

        light = self._light_ladder(decision, context=context)
        if light is not None:
            return light

        medium = self._medium_enforcement(decision, context=context)
        if medium is not None:
            return medium

        if action == "ban":
            if (
                decision.severity in {"high", "critical"}
                and decision.confidence >= self.mute_threshold
            ):
                return PolicyEvaluation(
                    original_action=action,
                    final_action="mute",
                    final_delete_message=True,
                    final_mute_minutes=None,
                    autonomous=True,
                    requires_human_review=False,
                    policy_reason=(
                        "Contextual ban request is downgraded to temporary MUTE + DELETE."
                    ),
                )
            return self._escalate(
                decision,
                "Contextual ban request did not meet protected autonomous-ban rules.",
            )

        if action == "mute":
            if (
                decision.confidence >= self.mute_threshold
                and decision.severity in {"medium", "high", "critical"}
            ):
                return PolicyEvaluation(
                    original_action=action,
                    final_action="mute",
                    final_delete_message=True,
                    final_mute_minutes=None,
                    autonomous=True,
                    requires_human_review=False,
                    policy_reason="Mute confidence threshold met.",
                )
            return self._escalate(
                decision,
                "Mute requested but confidence was insufficient.",
            )

        if action == "delete":
            if decision.category in SOFT_CONTEXTUAL_DELETE_CATEGORIES:
                if decision.confidence >= self.warn_threshold:
                    return PolicyEvaluation(
                        original_action=action,
                        final_action="warn",
                        final_delete_message=False,
                        final_mute_minutes=None,
                        autonomous=True,
                        requires_human_review=False,
                        policy_reason=(
                            "Contextual/soft violation → warning to reduce false positives."
                        ),
                    )
                return self._force_allow(
                    decision,
                    "Contextual/soft violation confidence is insufficient.",
                )

            if decision.confidence >= self.delete_threshold:
                return PolicyEvaluation(
                    original_action=action,
                    final_action="delete",
                    final_delete_message=True,
                    final_mute_minutes=None,
                    autonomous=True,
                    requires_human_review=False,
                    policy_reason="Delete confidence threshold met.",
                )

            if decision.confidence >= self.warn_threshold:
                return PolicyEvaluation(
                    original_action=action,
                    final_action="warn",
                    final_delete_message=False,
                    final_mute_minutes=None,
                    autonomous=True,
                    requires_human_review=False,
                    policy_reason="Delete confidence was insufficient; downgraded to warning.",
                )

            return self._force_allow(decision, "Insufficient confidence for moderation.")

        if action == "warn":
            if decision.confidence >= self.warn_threshold:
                return PolicyEvaluation(
                    original_action=action,
                    final_action="warn",
                    final_delete_message=False,
                    final_mute_minutes=None,
                    autonomous=True,
                    requires_human_review=False,
                    policy_reason="Warning confidence threshold met.",
                )
            return self._force_allow(decision, "Warning confidence was too low.")

        return self._escalate(decision, "Unsupported policy state.")
