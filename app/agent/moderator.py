import hashlib
import logging
import time

from app.admin.control_repository import ControlRepository
from app.agent.prompts import SYSTEM_PROMPT, build_moderation_prompt
from app.agent.destructive_recheck_prompts import (
    DESTRUCTIVE_OTHER_RECHECK_SYSTEM_PROMPT,
    build_destructive_other_recheck_prompt,
)
from app.agent.report_prompts import (
    REPORT_REVIEW_SYSTEM_PROMPT,
    build_report_review_prompt,
)
from app.agent.report_intent import (
    ReportIntentDecision,
    REPORT_INTENT_SYSTEM_PROMPT,
    build_report_intent_prompt,
)
from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    TriageDecision,
)
from app.agent.triage_prompts import (
    TRIAGE_SYSTEM_PROMPT,
    build_triage_prompt,
)
from app.agent.safe_challenge_prompts import (
    SAFE_CHALLENGE_SYSTEM_PROMPT,
    build_safe_challenge_prompt,
)
from app.llm.base import LLMError, LLMProvider
from app.utils.decision_cache import AsyncDecisionCache


logger = logging.getLogger(__name__)


DESTRUCTIVE_ACTIONS = {
    "delete",
    "mute",
    "ban",
}

VIOLATION_ACTIONS = {
    "warn",
    "delete",
    "mute",
    "ban",
}

# This is not a keyword/category classifier.
# These are the structured semantic categories the LLM itself is allowed
# to return. The consistency layer only validates its own JSON fields.
HARMFUL_CATEGORIES = {
    "spam",
    "scam",
    "phishing",
    "malicious_link",
    "unsolicited_advertising",
    "flood",
    "harassment",
    "hate",
    "threat",
    "adult_content",
    "impersonation",
    "evasion_attempt",
    "other",
}


class ModeratorAgent:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        fast_provider: LLMProvider | None = None,
        cache: AsyncDecisionCache[ModerationDecision] | None = None,
        control_repository: ControlRepository | None = None,
    ):
        self.provider = provider
        self.fast_provider = fast_provider
        self.control_repository = control_repository

        self.cache = cache or AsyncDecisionCache[
            ModerationDecision
        ](
            ttl_seconds=45
        )

    def _cache_key(
        self,
        context: MessageContext,
        *,
        force_deep: bool = False,
    ) -> str:
        """Cache only decisions made under equivalent user/history/behavior context."""
        current = context.current_message
        normalized = current.normalized_text.strip().casefold()
        text_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        history_payload = "|".join(
            f"{item.action}:{item.category}:{item.severity}:{item.reversed}"
            for item in context.user_moderation_history[-8:]
        )
        history_digest = hashlib.sha256(
            history_payload.encode("utf-8")
        ).hexdigest()[:16]

        behavior = context.behavior_signals
        behavior_scope = (
            f"m60:{behavior.messages_last_60s}:"
            f"repeat:{behavior.repeated_recent_messages}:"
            f"urls:{int(behavior.has_urls)}"
        )

        target_id = (
            context.reply_target_message.telegram_message_id
            if context.reply_target_message is not None
            else current.reply_to_message_id
        )

        return (
            f"{current.chat_id}:user:{current.user_id}:reply:{target_id}:"
            f"deep:{int(force_deep)}:{behavior_scope}:hist:{history_digest}:"
            f"text:{text_digest}"
        )

    def _repair_semantic_consistency(
        self,
        decision: ModerationDecision,
        *,
        source: str,
    ) -> ModerationDecision:
        """
        Repair contradictory STRUCTURED OUTPUT without classifying text.

        The LLM still owns semantics:
        - category
        - action
        - confidence
        - reason

        Examples of contradictions we repair:
        spam + delete + 1.00 confidence + current_message_violation=False
        delete + clear reason + empty current_message_evidence

        This is schema consistency, not keyword moderation.
        """

        updates: dict = {}

        # report_target is a separate safe intent.
        if (
            decision.report_target
            and decision.report_confidence <= 0.0
        ):
            updates["report_confidence"] = max(
                decision.confidence,
                0.80,
            )

        # If the LLM explicitly chose a violation action in a harmful category
        # with meaningful confidence, current_message_violation=False is
        # internally contradictory.
        if (
            decision.action in VIOLATION_ACTIONS
            and decision.category in HARMFUL_CATEGORIES
            and decision.confidence >= 0.70
            and not decision.needs_human_review
            and not decision.report_target
            and not decision.current_message_violation
        ):
            updates["current_message_violation"] = True

            logger.info(
                "DECISION REPAIR | source=%s | "
                "field=current_message_violation | "
                "category=%s | action=%s | confidence=%.2f",
                source,
                decision.category,
                decision.action,
                decision.confidence,
            )

        # PolicyGate requires evidence before destructive actions.
        # If the LLM already provided an explicit current-message reason but
        # forgot the optional evidence array, reuse that semantic explanation
        # instead of routing an obvious 100%% decision to human review.
        if (
            decision.action in DESTRUCTIVE_ACTIONS
            and decision.category in HARMFUL_CATEGORIES
            and decision.confidence >= 0.78
            and not decision.needs_human_review
            and not decision.report_target
            and not decision.current_message_evidence
            and decision.reason.strip()
        ):
            updates[
                "current_message_evidence"
            ] = [
                (
                    "LLM semantic finding about the current message: "
                    + decision.reason.strip()
                )[:500]
            ]

            logger.info(
                "DECISION REPAIR | source=%s | "
                "field=current_message_evidence | "
                "category=%s | action=%s | confidence=%.2f",
                source,
                decision.category,
                decision.action,
                decision.confidence,
            )

        if not updates:
            return decision

        return decision.model_copy(
            update=updates
        )


    async def classify_reply_intent(
        self,
        context: MessageContext,
    ) -> ReportIntentDecision:
        """
        Dedicated semantic preflight for replies.

        This runs BEFORE ordinary moderation. Its purpose is to separate
        "the user is reporting the replied-to message" from
        "the reply itself violates policy".
        """

        if context.reply_target_message is None:
            return ReportIntentDecision(
                report_target=False,
                confidence=1.0,
                reporter_has_independent_violation=False,
                reason="No reply target.",
            )

        provider = (
            self.fast_provider
            if self.fast_provider is not None
            else self.provider
        )

        started = time.perf_counter()

        try:
            result = await provider.generate_structured(
                system_prompt=REPORT_INTENT_SYSTEM_PROMPT,
                user_prompt=build_report_intent_prompt(
                    context
                ),
                response_model=ReportIntentDecision,
            )

            elapsed_ms = int(
                (time.perf_counter() - started)
                * 1000
            )

            logger.info(
                "REPORT PREFLIGHT | model=%s | ms=%s | "
                "report=%s | independent_violation=%s | confidence=%.2f",
                (
                    "fast"
                    if provider is self.fast_provider
                    else "deep"
                ),
                elapsed_ms,
                result.report_target,
                result.reporter_has_independent_violation,
                result.confidence,
            )

            # If the small model is unsure that this is an ordinary reply,
            # ask Deep AI before exposing the reply to ordinary moderation.
            if (
                self.fast_provider is not None
                and provider is self.fast_provider
                and not result.report_target
                and result.confidence < 0.99
            ):
                deep_started = time.perf_counter()

                deep_result = await self.provider.generate_structured(
                    system_prompt=REPORT_INTENT_SYSTEM_PROMPT,
                    user_prompt=build_report_intent_prompt(
                        context
                    ),
                    response_model=ReportIntentDecision,
                )

                deep_ms = int(
                    (time.perf_counter() - deep_started)
                    * 1000
                )

                logger.info(
                    "REPORT PREFLIGHT DEEP | ms=%s | "
                    "report=%s | independent_violation=%s | confidence=%.2f",
                    deep_ms,
                    deep_result.report_target,
                    deep_result.reporter_has_independent_violation,
                    deep_result.confidence,
                )

                return deep_result

            return result

        except Exception:
            logger.exception(
                "Report intent preflight failed"
            )

            # Do not invent report intent on technical failure.
            # The ordinary moderation pipeline remains available.
            return ReportIntentDecision(
                report_target=False,
                confidence=0.0,
                reporter_has_independent_violation=False,
                reason="Report intent classifier unavailable.",
            )

    async def _known_pattern(
        self,
        context: MessageContext,
    ) -> ModerationDecision | None:
        if self.control_repository is None:
            return None

        if context.current_message.reply_to_message_id is not None:
            return None

        raw = await self.control_repository.match_confirmed_pattern(
            chat_id=context.current_message.chat_id,
            normalized_text=context.current_message.normalized_text,
        )

        if raw is None:
            return None

        try:
            decision = ModerationDecision.model_validate_json(
                raw
            )
        except Exception:
            logger.exception(
                "Known moderation pattern is invalid JSON"
            )
            return None

        decision = decision.model_copy(
            update={
                "context_evidence": [
                    "Matched a previously confirmed and successfully removed "
                    "malicious message in this chat."
                ],
                "report_target": False,
                "report_confidence": 0.0,
                "report_reason": "",
            }
        )

        logger.info(
            "KNOWN PATTERN HIT | "
            "chat=%s | message=%s | category=%s",
            context.current_message.chat_id,
            context.current_message.telegram_message_id,
            decision.category,
        )

        return decision

    def _force_deep_from_signals(
        self,
        context: MessageContext,
    ) -> bool:
        text = context.text_signals
        behavior = context.behavior_signals

        # Replies are handled earlier in _analyze_uncached after the dedicated
        # report-intent preflight. This helper only covers non-reply objective
        # routing signals.
        if context.current_message.reply_to_message_id is not None:
            return False

        return any(
            [
                behavior.has_urls,
                text.contains_invisible_chars,
                bool(text.mixed_script_tokens),
                bool(text.letter_digit_tokens),
                behavior.repeated_recent_messages >= 1,
                behavior.messages_last_60s >= 8,
                behavior.mention_count >= 5,
            ]
        )

    async def _recheck_destructive_other(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
    ) -> ModerationDecision:
        """
        A destructive category=other is not directly executable.

        Ask Deep AI once more for a specific semantic category. This preserves
        the existing executor safety allowlist instead of making "other" a
        generic live-delete escape hatch.
        """

        if not (
            decision.category == "other"
            and decision.action in DESTRUCTIVE_ACTIONS
            and decision.confidence >= 0.85
            and not decision.needs_human_review
            and not decision.report_target
        ):
            return decision

        started = time.perf_counter()

        try:
            reviewed = await self.provider.generate_structured(
                system_prompt=(
                    DESTRUCTIVE_OTHER_RECHECK_SYSTEM_PROMPT
                ),
                user_prompt=(
                    build_destructive_other_recheck_prompt(
                        context=context,
                        previous=decision,
                    )
                ),
                response_model=ModerationDecision,
            )

            reviewed = self._repair_semantic_consistency(
                reviewed,
                source="destructive-other-recheck",
            )

        except Exception:
            logger.exception(
                "Destructive other recheck failed; "
                "routing to human review"
            )

            return decision.model_copy(
                update={
                    "action": "escalate",
                    "delete_message": False,
                    "mute_minutes": None,
                    "needs_human_review": True,
                    "reason": (
                        "A second check could not reliably classify "
                        "destructive category=other."
                    ),
                }
            )

        elapsed_ms = int(
            (time.perf_counter() - started)
            * 1000
        )

        # Never let a second pass return the same unsafe generic destructive
        # shape. If it cannot name the category, humans review it.
        if (
            reviewed.category == "other"
            and reviewed.action in DESTRUCTIVE_ACTIONS
        ):
            reviewed = reviewed.model_copy(
                update={
                    "action": "escalate",
                    "delete_message": False,
                    "mute_minutes": None,
                    "needs_human_review": True,
                    "reason": (
                        "Deep recheck confirms risk but cannot reliably "
                        "select a specific moderation category."
                    ),
                }
            )

        logger.info(
            "DESTRUCTIVE OTHER RECHECK | "
            "ms=%s | from=%s/%s | to=%s/%s | "
            "confidence=%.2f",
            elapsed_ms,
            decision.category,
            decision.action,
            reviewed.category,
            reviewed.action,
            reviewed.confidence,
        )

        return reviewed

    async def _run_safe_challenge(
        self,
        context: MessageContext,
    ) -> TriageDecision:
        """A second, skeptical Fast pass before accepting a SAFE shortcut."""
        provider = self.fast_provider or self.provider
        started = time.perf_counter()
        result = await provider.generate_structured(
            system_prompt=SAFE_CHALLENGE_SYSTEM_PROMPT,
            user_prompt=build_safe_challenge_prompt(context),
            response_model=TriageDecision,
        )
        logger.info(
            "SAFE CHALLENGE | ms=%s | route=%s | confidence=%.2f",
            int((time.perf_counter() - started) * 1000),
            result.route,
            result.confidence,
        )
        return result

    async def _run_deep(
        self,
        context: MessageContext,
    ) -> ModerationDecision:
        started = time.perf_counter()

        result = await self.provider.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_moderation_prompt(context),
            response_model=ModerationDecision,
        )

        result = self._repair_semantic_consistency(
            result,
            source="deep",
        )

        result = await self._recheck_destructive_other(
            context=context,
            decision=result,
        )

        elapsed_ms = int(
            (time.perf_counter() - started)
            * 1000
        )

        logger.info(
            "DEEP LLM | ms=%s | category=%s | "
            "action=%s | confidence=%.2f | "
            "violation=%s | evidence=%s | "
            "report=%s | report_conf=%.2f",
            elapsed_ms,
            result.category,
            result.action,
            result.confidence,
            result.current_message_violation,
            len(result.current_message_evidence),
            result.report_target,
            result.report_confidence,
        )

        return result

    async def _run_triage(
        self,
        context: MessageContext,
    ) -> TriageDecision:
        if self.fast_provider is None:
            return TriageDecision(
                route="deep",
                confidence=1.0,
                reason="Fast provider disabled.",
            )

        started = time.perf_counter()

        result = await self.fast_provider.generate_structured(
            system_prompt=TRIAGE_SYSTEM_PROMPT,
            user_prompt=build_triage_prompt(context),
            response_model=TriageDecision,
        )

        if (
            result.report_target
            and result.report_confidence <= 0.0
        ):
            result = result.model_copy(
                update={
                    "report_confidence": max(
                        result.confidence,
                        0.80,
                    )
                }
            )

        elapsed_ms = int(
            (time.perf_counter() - started)
            * 1000
        )

        logger.info(
            "FAST TRIAGE | ms=%s | route=%s | "
            "confidence=%.2f | report=%s | "
            "report_conf=%.2f",
            elapsed_ms,
            result.route,
            result.confidence,
            result.report_target,
            result.report_confidence,
        )

        return result

    async def review_reported_target(
        self,
        context: MessageContext,
    ) -> ModerationDecision:
        if context.reply_target_message is None:
            raise ValueError(
                "No reported reply target available"
            )

        started = time.perf_counter()

        result = await self.provider.generate_structured(
            system_prompt=REPORT_REVIEW_SYSTEM_PROMPT,
            user_prompt=build_report_review_prompt(context),
            response_model=ModerationDecision,
        )

        result = self._repair_semantic_consistency(
            result,
            source="report-rereview",
        )

        report_text = (
            context.current_message.raw_text
            .strip()
            .replace("\n", " ")
        )

        report_context = list(
            result.context_evidence
        )

        if report_text:
            report_context.insert(
                0,
                (
                    "Member report: "
                    + report_text[:220]
                ),
            )

        # A re-review that says ALLOW but is not highly certain is precisely
        # the "спорная ситуация" that belongs in Tickets.
        #
        # A clearly safe target (>= 0.96) stays ALLOW and creates no ticket.
        if (
            result.action == "allow"
            and not result.current_message_violation
            and result.confidence < 0.96
        ):
            original_reason = result.reason

            result = result.model_copy(
                update={
                    "current_message_violation": True,
                    "category": (
                        result.category
                        if result.category != "safe"
                        else "other"
                    ),
                    "severity": (
                        result.severity
                        if result.severity != "none"
                        else "medium"
                    ),
                    "action": "escalate",
                    "delete_message": False,
                    "mute_minutes": None,
                    "needs_human_review": True,
                    "reason": (
                        "After the report, Deep AI could not confidently "
                        "confirm that the target message is safe. "
                        f"Previous conclusion: {original_reason}"
                    )[:800],
                }
            )

            logger.info(
                "REPORT ROUTE | ambiguous_allow_to_ticket | "
                "target=%s | confidence=%.2f",
                context.reply_target_message.telegram_message_id,
                result.confidence,
            )

        # This result is about TARGET, never about a nested report.
        result = result.model_copy(
            update={
                "report_target": False,
                "report_confidence": 0.0,
                "report_reason": "",
                "context_evidence": report_context[:5],
            }
        )

        elapsed_ms = int(
            (time.perf_counter() - started)
            * 1000
        )

        logger.info(
            "REPORT REREVIEW | ms=%s | target=%s | "
            "category=%s | action=%s | "
            "confidence=%.2f | violation=%s | "
            "human_review=%s",
            elapsed_ms,
            context.reply_target_message.telegram_message_id,
            result.category,
            result.action,
            result.confidence,
            result.current_message_violation,
            result.needs_human_review,
        )

        return result

    async def _analyze_uncached(
        self,
        context: MessageContext,
        *,
        force_deep: bool = False,
    ) -> ModerationDecision:
        if force_deep or self._force_deep_from_signals(
            context
        ):
            logger.info(
                "ROUTE | deep | reason=%s",
                ("forced_deep" if force_deep else "objective_signals"),
            )

            return await self._run_deep(
                context
            )

        if self.fast_provider is None:
            return await self._run_deep(
                context
            )

        try:
            triage = await self._run_triage(
                context
            )

        except Exception:
            logger.exception(
                "Fast triage failed; "
                "falling back to deep moderator"
            )

            return await self._run_deep(
                context
            )

        # A report is a REQUEST FOR RE-REVIEW, not evidence of guilt.
        # Therefore report_target=True is safe to honor without an arbitrary
        # confidence gate.
        if (
            triage.report_target
            and context.reply_target_message
            is not None
        ):
            return ModerationDecision(
                detected_language="unknown",
                current_message_violation=False,
                category="safe",
                severity="none",
                confidence=max(
                    triage.confidence,
                    0.95,
                ),
                action="allow",
                delete_message=False,
                mute_minutes=None,
                needs_human_review=False,
                reason=(
                    "This message is reporting the message it replies to."
                ),
                current_message_evidence=[],
                context_evidence=[],
                report_target=True,
                report_confidence=max(
                    triage.report_confidence,
                    triage.confidence,
                    0.80,
                ),
                report_reason=(
                    triage.report_reason
                    or triage.reason
                ),
            )

        if (
            triage.route == "safe"
            and triage.confidence >= 0.92
        ):
            try:
                challenge = await self._run_safe_challenge(context)
            except Exception:
                logger.exception(
                    "Safe challenge failed; routing to Deep instead of trusting SAFE"
                )
                return await self._run_deep(context)

            if challenge.route == "safe" and challenge.confidence >= 0.97:
                return ModerationDecision(
                    detected_language="unknown",
                    current_message_violation=False,
                    category="safe",
                    severity="none",
                    confidence=min(triage.confidence, challenge.confidence),
                    action="allow",
                    delete_message=False,
                    mute_minutes=None,
                    needs_human_review=False,
                    reason="Fast triage and independent safe challenge both found no violation.",
                    current_message_evidence=[],
                    context_evidence=[],
                    report_target=False,
                    report_confidence=triage.report_confidence,
                    report_reason=triage.report_reason,
                )

            logger.info(
                "ROUTE | deep | reason=safe_challenge | challenge=%s/%.2f",
                challenge.route,
                challenge.confidence,
            )
            return await self._run_deep(context)

        deep = await self._run_deep(
            context
        )

        if (
            triage.report_target
            and context.reply_target_message
            is not None
        ):
            deep = deep.model_copy(
                update={
                    "report_target": True,
                    "report_confidence": max(
                        triage.report_confidence,
                        deep.report_confidence,
                        triage.confidence,
                        0.80,
                    ),
                    "report_reason": (
                        triage.report_reason
                        or deep.report_reason
                        or triage.reason
                    ),
                    "current_message_violation": False,
                    "category": "safe",
                    "severity": "none",
                    "action": "allow",
                    "delete_message": False,
                    "mute_minutes": None,
                    "needs_human_review": False,
                }
            )

        return self._repair_semantic_consistency(
            deep,
            source="deep-after-triage",
        )

    async def analyze(
        self,
        context: MessageContext,
        *,
        force_deep: bool = False,
    ) -> ModerationDecision:
        try:
            known = await self._known_pattern(
                context
            )

            if known is not None:
                return known

            key = self._cache_key(
                context,
                force_deep=force_deep,
            )

            (
                result,
                cache_hit,
            ) = await self.cache.get_or_create(
                key,
                lambda: (
                    self._analyze_uncached(
                        context,
                        force_deep=force_deep,
                    )
                ),
            )

            if cache_hit:
                logger.info(
                    "DECISION CACHE HIT | "
                    "user=%s | message=%s",
                    context.current_message.user_id,
                    context.current_message.telegram_message_id,
                )

            return result

        except LLMError:
            logger.exception(
                "LLM moderation failed"
            )

            return ModerationDecision(
                detected_language="unknown",
                current_message_violation=True,
                category="other",
                severity="medium",
                confidence=0.0,
                action="escalate",
                delete_message=False,
                mute_minutes=None,
                needs_human_review=True,
                reason=(
                    "Could not obtain a reliable moderation decision from the LLM."
                ),
                current_message_evidence=[],
                context_evidence=[
                    "LLM analysis unavailable"
                ],
            )

        except Exception:
            logger.exception(
                "Unexpected moderator error"
            )

            return ModerationDecision(
                detected_language="unknown",
                current_message_violation=True,
                category="other",
                severity="medium",
                confidence=0.0,
                action="escalate",
                delete_message=False,
                mute_minutes=None,
                needs_human_review=True,
                reason=(
                    "Internal AI moderator error."
                ),
                current_message_evidence=[],
                context_evidence=[
                    "Moderator pipeline error"
                ],
            )
