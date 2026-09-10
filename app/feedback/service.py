import logging
import time

from app.admin.control_repository import (
    ControlRepository,
)
from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)
from app.feedback.prompts import (
    FEEDBACK_REVIEW_SYSTEM_PROMPT,
    build_feedback_review_prompt,
)
from app.feedback.schemas import (
    FeedbackReviewDecision,
)
from app.llm.base import LLMProvider
from app.moderation.policy import PolicyGate


logger = logging.getLogger(__name__)


HARD_SAFETY_CATEGORIES = {
    "scam",
    "phishing",
    "malicious_link",
    "threat",
}


class ModeratorFeedbackService:
    """
    Chat-scoped soft memory learned only from REAL moderator ticket decisions.

    Stable Core always runs first.
    Explicit Community Policy is applied later and therefore keeps priority.
    """

    def __init__(
        self,
        *,
        repository: ControlRepository,
        deep_provider: LLMProvider,
        max_examples: int = 12,
    ):
        self.repository = repository
        self.deep_provider = deep_provider
        self.max_examples = max(
            1,
            min(
                int(max_examples),
                30,
            ),
        )

    @staticmethod
    def _eligible_feedback_example(
        item,
    ) -> bool:
        """
        Human ALLOW on a historical hard-safety ticket is retained in the DB
        for audit, but never used as soft precedent.

        This prevents an accidental/test ALLOW on scam/phishing from teaching
        future gray cases to become more permissive.
        """

        return not (
            getattr(
                item,
                "moderator_action",
                None,
            )
            == "allow"
            and getattr(
                item,
                "ai_category",
                None,
            )
            in HARD_SAFETY_CATEGORIES
        )

    @staticmethod
    def _is_gray_case(
        *,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
    ) -> bool:
        # Do not touch a clear Core action.
        if (
            policy.final_action
            in {
                "delete",
                "mute",
                "ban",
            }
            and decision.confidence
            >= 0.85
            and not policy.requires_human_review
        ):
            return False

        return bool(
            policy.final_action
            == "escalate"
            or policy.requires_human_review
            or decision.needs_human_review
            or (
                decision.confidence
                < 0.72
                and policy.final_action
                in {
                    "allow",
                    "warn",
                }
            )
        )

    async def learn_from_ticket(
        self,
        *,
        ticket,
        moderator_action: str,
        moderator_admin_id: int | None,
    ) -> bool:
        """
        Best-effort learning.

        Failure to save memory must never break a moderator's real action.
        """

        try:
            record = (
                await self.repository
                .save_moderator_feedback(
                    ticket_id=ticket.id,
                    moderator_action=(
                        moderator_action
                    ),
                    moderator_admin_id=(
                        moderator_admin_id
                    ),
                )
            )

            if record is None:
                logger.debug(
                    "FEEDBACK MEMORY SKIP | "
                    "ticket=%s",
                    ticket.id,
                )
                return False

            logger.info(
                "FEEDBACK MEMORY LEARNED | "
                "chat=%s | ticket=%s | "
                "action=%s | feedback=%s",
                record.chat_id,
                ticket.id,
                record.moderator_action,
                record.id,
            )

            return True

        except Exception:
            logger.exception(
                "Feedback memory save failed | "
                "ticket=%s",
                getattr(
                    ticket,
                    "id",
                    None,
                ),
            )
            return False

    async def refine_gray_case(
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
        Refine ONLY uncertain Core decisions.

        No examples / no semantic relevance / unsafe recommendation:
        original Core decision is returned unchanged.
        """

        if not self._is_gray_case(
            decision=baseline_decision,
            policy=baseline_policy,
        ):
            return (
                baseline_decision,
                baseline_policy,
            )

        examples = (
            await self.repository
            .recent_moderator_feedback(
                chat_id=(
                    context
                    .current_message
                    .chat_id
                ),
                limit=self.max_examples,
            )
        )

        raw_example_count = len(
            examples
        )

        examples = [
            item
            for item in examples
            if self._eligible_feedback_example(
                item
            )
        ]

        filtered_count = (
            raw_example_count
            - len(examples)
        )

        if filtered_count:
            logger.info(
                "FEEDBACK MEMORY FILTER | "
                "chat=%s | excluded_hard_safety_allow=%s",
                context.current_message.chat_id,
                filtered_count,
            )

        if not examples:
            return (
                baseline_decision,
                baseline_policy,
            )

        started = time.perf_counter()

        try:
            review = (
                await self.deep_provider
                .generate_structured(
                    system_prompt=(
                        FEEDBACK_REVIEW_SYSTEM_PROMPT
                    ),
                    user_prompt=(
                        build_feedback_review_prompt(
                            context=context,
                            baseline_decision=(
                                baseline_decision
                            ),
                            baseline_policy=(
                                baseline_policy
                            ),
                            examples=examples,
                        )
                    ),
                    response_model=(
                        FeedbackReviewDecision
                    ),
                )
            )

        except Exception:
            logger.exception(
                "Feedback memory review failed; "
                "stable Core decision preserved"
            )
            return (
                baseline_decision,
                baseline_policy,
            )

        elapsed_ms = int(
            (
                time.perf_counter()
                - started
            )
            * 1000
        )

        available = {
            item.id: item
            for item in examples
        }

        matched_ids = [
            item_id
            for item_id
            in review.matched_feedback_ids
            if item_id in available
        ]

        logger.info(
            "FEEDBACK MEMORY REVIEW | "
            "chat=%s | relevant=%s | "
            "confidence=%.2f | matched=%s | "
            "recommend=%s | ms=%s",
            context.current_message.chat_id,
            review.relevant,
            review.confidence,
            matched_ids,
            review.recommended_action,
            elapsed_ms,
        )

        if (
            not review.relevant
            or review.confidence < 0.85
            or not matched_ids
            or review.recommended_action
            == "escalate"
        ):
            return (
                baseline_decision,
                baseline_policy,
            )

        # Feedback may not use historical ALLOW to weaken high-risk Core
        # semantics even when the current Core result is uncertain.
        if (
            baseline_decision.category
            in HARD_SAFETY_CATEGORIES
            and baseline_decision
            .current_message_violation
            and review.recommended_action
            == "allow"
        ):
            logger.info(
                "FEEDBACK MEMORY BLOCKED | "
                "reason=core_hard_safety | "
                "category=%s",
                baseline_decision.category,
            )

            return (
                baseline_decision,
                baseline_policy,
            )

        # Strong account-level restrictions require repeated human precedent.
        if review.recommended_action in {
            "mute",
            "ban",
        }:
            same_action_count = sum(
                1
                for item_id in matched_ids
                if (
                    available[
                        item_id
                    ].moderator_action
                    == review
                    .recommended_action
                )
            )

            if (
                same_action_count < 2
                or review.confidence
                < 0.95
            ):
                logger.info(
                    "FEEDBACK MEMORY BLOCKED | "
                    "reason=insufficient_repeated_precedent | "
                    "action=%s | count=%s",
                    review.recommended_action,
                    same_action_count,
                )

                return (
                    baseline_decision,
                    baseline_policy,
                )

        if (
            review.recommended_action
            in {
                "warn",
                "delete",
                "mute",
                "ban",
            }
            and not review
            .current_message_evidence
        ):
            return (
                baseline_decision,
                baseline_policy,
            )

        if review.recommended_action == "allow":
            refined = ModerationDecision(
                detected_language=(
                    baseline_decision
                    .detected_language
                ),
                current_message_violation=False,
                category="safe",
                severity="none",
                confidence=(
                    review.confidence
                ),
                action="allow",
                delete_message=False,
                mute_minutes=None,
                needs_human_review=False,
                reason=(
                    "Moderator feedback memory: "
                    + review.reason
                )[:800],
                current_message_evidence=[],
                context_evidence=[
                    (
                        "Relevant prior moderator "
                        f"feedback IDs: {matched_ids}"
                    )
                ],
                report_target=(
                    baseline_decision
                    .report_target
                ),
                report_confidence=(
                    baseline_decision
                    .report_confidence
                ),
                report_reason=(
                    baseline_decision
                    .report_reason
                ),
            )

        else:
            refined = ModerationDecision(
                detected_language=(
                    baseline_decision
                    .detected_language
                ),
                current_message_violation=True,
                category=review.category,
                severity=review.severity,
                confidence=(
                    review.confidence
                ),
                action=(
                    review.recommended_action
                ),
                delete_message=(
                    review.recommended_action
                    in {
                        "delete",
                        "mute",
                        "ban",
                    }
                ),
                mute_minutes=(
                    30
                    if review.recommended_action
                    == "mute"
                    else None
                ),
                needs_human_review=False,
                reason=(
                    "Moderator feedback memory: "
                    + review.reason
                )[:800],
                current_message_evidence=(
                    review
                    .current_message_evidence
                ),
                context_evidence=[
                    (
                        "Relevant prior moderator "
                        f"feedback IDs: {matched_ids}"
                    )
                ],
                report_target=(
                    baseline_decision
                    .report_target
                ),
                report_confidence=(
                    baseline_decision
                    .report_confidence
                ),
                report_reason=(
                    baseline_decision
                    .report_reason
                ),
            )

        refined_policy = policy_gate.evaluate(
            refined,
            context=context,
        )

        if (
            refined_policy.final_action
            == "escalate"
            or refined_policy
            .requires_human_review
        ):
            return (
                baseline_decision,
                baseline_policy,
            )

        logger.info(
            "FEEDBACK MEMORY HIT | "
            "chat=%s | core=%s | refined=%s | "
            "confidence=%.2f | examples=%s",
            context.current_message.chat_id,
            baseline_policy.final_action,
            refined_policy.final_action,
            refined.confidence,
            matched_ids,
        )

        return (
            refined,
            refined_policy,
        )
