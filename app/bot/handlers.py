import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated, Message

from app.admin.control_repository import ControlRepository
from app.agent.context import (
    MessageContextBuilder,
    reported_target_context,
)
from app.agent.moderator import ModeratorAgent
from app.agent.schemas import ModerationDecision
from app.community_policy.service import CommunityPolicyService
from app.feedback.service import ModeratorFeedbackService
from app.moderation.executor import ModerationExecutor
from app.moderation.fake_admin import FakeAdminDetector
from app.moderation.policy import PolicyGate
from app.moderation.reputation import apply_light_reputation_decay
from app.raid_guard.service import RaidGuardService
from app.semantic_clustering.service import SemanticClusterService
from app.utils.chat_locks import ChatLockManager


router = Router(
    name="group_moderation"
)

logger = logging.getLogger(__name__)


GROUP_CHAT = F.chat.type.in_(
    {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }
)


def should_moderate_message(
    message: Message,
) -> bool:
    return bool(
        (
            message.text
            and message.text.strip()
        )
        or (
            message.caption
            and message.caption.strip()
        )
    )


async def process_report_rereview(
    *,
    context,
    moderator_agent: ModeratorAgent,
    policy_gate: PolicyGate,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    raid_guard_service: RaidGuardService,
    semantic_cluster_service: SemanticClusterService,
    moderation_executor: ModerationExecutor,
) -> None:
    if context.reply_target_message is None:
        return

    target_context = reported_target_context(
        context
    )

    if target_context is None:
        return

    target_decision = (
        await moderator_agent
        .review_reported_target(
            context
        )
    )

    target_policy = policy_gate.evaluate(
        target_decision,
        context=target_context,
    )

    target_decision, target_policy = (
        await feedback_service.refine_gray_case(
            context=target_context,
            baseline_decision=target_decision,
            baseline_policy=target_policy,
            policy_gate=policy_gate,
        )
    )

    target_decision, target_policy = (
        await community_policy_service.apply_overlay(
            context=target_context,
            baseline_decision=target_decision,
            baseline_policy=target_policy,
            policy_gate=policy_gate,
        )
    )

    raid_result = await raid_guard_service.inspect_before_execution(
        context=target_context,
        decision=target_decision,
        policy=target_policy,
    )

    target_result = (
        await moderation_executor.execute(
            context=target_context,
            decision=target_decision,
            policy=target_policy,
        )
    )

    if raid_result.prepared is not None:
        await semantic_cluster_service.observe_once(
            context=target_context,
            decision=target_decision,
            policy=target_policy,
            prepared=raid_result.prepared,
        )
    else:
        semantic_cluster_service.enqueue(
            context=target_context,
            decision=target_decision,
            policy=target_policy,
        )

    logger.info(
        "REPORT FLOW | "
        "reporter_message=%s | "
        "target_message=%s | "
        "target_user=%s | "
        "category=%s | "
        "LLM=%s | policy=%s | "
        "confidence=%.2f | event=%s",
        context.current_message.telegram_message_id,
        target_context.current_message.telegram_message_id,
        target_context.current_message.user_id,
        target_decision.category,
        target_decision.action,
        target_policy.final_action,
        target_decision.confidence,
        target_result.audit_event_key,
    )


def reporter_allow_decision(
    *,
    confidence: float,
    reason: str,
) -> ModerationDecision:
    """
    A semantic report is safe for the reporter unless the dedicated report
    intent model independently detects another violation in the reply.
    """

    return ModerationDecision(
        detected_language="unknown",
        current_message_violation=False,
        category="safe",
        severity="none",
        confidence=max(
            confidence,
            0.95,
        ),
        action="allow",
        delete_message=False,
        mute_minutes=None,
        needs_human_review=False,
        reason=(
            "This reply is a report about the message it replies to."
        ),
        current_message_evidence=[],
        context_evidence=[],
        report_target=True,
        report_confidence=max(
            confidence,
            0.80,
        ),
        report_reason=reason,
    )


async def process_message(
    *,
    message: Message,
    bot: Bot,
    context_builder: MessageContextBuilder,
    moderator_agent: ModeratorAgent,
    policy_gate: PolicyGate,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    raid_guard_service: RaidGuardService,
    semantic_cluster_service: SemanticClusterService,
    control_repository: ControlRepository,
    moderation_executor: ModerationExecutor,
    chat_locks: ChatLockManager,
    fake_admin_detector: FakeAdminDetector,
    pilot_access_service=None,
    edited: bool,
) -> None:
    await control_repository.ensure_chat_settings(
        chat_id=message.chat.id,
        chat_title=message.chat.title,
    )

    # Optional private Pilot Edition control plane. When a renter is suspended
    # or the platform emergency switch pauses a community, moderation stops
    # before message storage/AI execution. Public builds simply pass None.
    if pilot_access_service is not None:
        try:
            if not await pilot_access_service.is_chat_operational(message.chat.id):
                logger.info(
                    "PILOT PAUSE | moderation skipped | chat=%s",
                    message.chat.id,
                )
                return
        except Exception:
            logger.exception(
                "Pilot access check failed; fail-safe skips moderation | chat=%s",
                message.chat.id,
            )
            return

    if (
        message.from_user
        and message.from_user.id == bot.id
    ):
        return

    # Full immunity is deliberately checked before context persistence,
    # semantic clustering and every AI/rules path. It is intended mainly for
    # trusted service bots and integrations inside a community.
    if message.from_user is not None:
        if await control_repository.is_moderation_immune(
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            username=message.from_user.username,
        ):
            logger.info(
                "IMMUNITY BYPASS | chat=%s | user=%s | username=%s",
                message.chat.id,
                message.from_user.id,
                message.from_user.username,
            )
            return

    if not should_moderate_message(
        message
    ):
        logger.debug(
            "Skipping non-text/service event | "
            "chat=%s | message=%s | "
            "content_type=%s",
            message.chat.id,
            message.message_id,
            message.content_type,
        )
        return

    async with chat_locks.lock(
        message.chat.id
    ):
        context = (
            await context_builder.build(
                message,
                edited=edited,
            )
        )

    # LIGHT reputation is deliberately time-bounded. Old minor spam/flood/
    # harassment warnings must not turn a new offense days later into an
    # unexpected mute/ban. The window is configured per community.
    try:
        light_decay_hours = await control_repository.get_light_offense_decay_hours(
            message.chat.id
        )
    except AttributeError:
        # Compatibility with small repository test doubles / public adapters.
        light_decay_hours = 6
    context = apply_light_reputation_decay(
        context,
        hours=int(light_decay_hours),
    )

    # Deterministic fake-admin/fake-moderator protection runs before the LLM
    # pipeline. It is intentionally conservative: a non-admin identity must
    # look like authority AND the current message must contain dangerous
    # scam/redirect/verification signals. Real Telegram admins are excluded
    # by user ID.
    try:
        fake_admin = await fake_admin_detector.inspect(message)
    except Exception:
        logger.exception(
            "Fake-admin detector failed; normal AI moderation continues | chat=%s",
            message.chat.id,
        )
        fake_admin = None

    if fake_admin is not None:
        decision = fake_admin.decision()
        policy = fake_admin.policy()
        result = await moderation_executor.execute(
            context=context,
            decision=decision,
            policy=policy,
        )
        semantic_cluster_service.enqueue(
            context=context,
            decision=decision,
            policy=policy,
        )
        logger.warning(
            "FAKE ADMIN DETECTED | chat=%s | user=%s | confidence=%.2f | event=%s",
            message.chat.id,
            (message.from_user.id if message.from_user else None),
            decision.confidence,
            result.audit_event_key,
        )
        return

    report_preflight = None

    if (
        context.reply_target_message
        is not None
    ):
        report_preflight = (
            await moderator_agent
            .classify_reply_intent(
                context
            )
        )

        # REPORT-FIRST ROUTE
        #
        # The complaint never enters ordinary moderation unless the dedicated
        # semantic classifier says the reply contains a separate independent
        # violation of its own.
        if (
            report_preflight.report_target
            and not report_preflight
            .reporter_has_independent_violation
        ):
            decision = reporter_allow_decision(
                confidence=(
                    report_preflight.confidence
                ),
                reason=(
                    report_preflight.reason
                ),
            )

            policy = policy_gate.evaluate(
                decision,
                context=context,
            )

            decision, policy = (
                await community_policy_service.apply_overlay(
                    context=context,
                    baseline_decision=decision,
                    baseline_policy=policy,
                    policy_gate=policy_gate,
                )
            )

            result = (
                await moderation_executor
                .execute(
                    context=context,
                    decision=decision,
                    policy=policy,
                )
            )

            semantic_cluster_service.enqueue(
                context=context,
                decision=decision,
                policy=policy,
            )

            logger.info(
                "REPORTER SAFE | "
                "message=%s | user=%s | "
                "policy=%s | confidence=%.2f | "
                "event=%s",
                message.message_id,
                (
                    message.from_user.id
                    if message.from_user
                    else None
                ),
                policy.final_action,
                decision.confidence,
                result.audit_event_key,
            )

            await process_report_rereview(
                context=context,
                moderator_agent=(
                    moderator_agent
                ),
                policy_gate=policy_gate,
                community_policy_service=(
                    community_policy_service
                ),
                feedback_service=(
                    feedback_service
                ),
                raid_guard_service=(
                    raid_guard_service
                ),
                semantic_cluster_service=(
                    semantic_cluster_service
                ),
                moderation_executor=(
                    moderation_executor
                ),
            )

            return

    # Ordinary moderation path.
    # IMPORTANT SAFETY BOUNDARY:
    # semantic similarity is observational evidence for campaign analysis and
    # Raid Guard only. It must NEVER force a per-message moderation verdict.
    # A harmless message posted after a scam burst must still be judged only by
    # the normal current-message AI pipeline.
    prepared_semantic = None
    if semantic_cluster_service.enabled:
        try:
            prepared_semantic = await semantic_cluster_service.prepare_signal(
                context=context
            )
        except Exception:
            logger.exception(
                "Semantic preparation failed; stable moderation continues"
            )
            prepared_semantic = None

    # Reply messages have already passed the dedicated semantic report-intent
    # preflight above. Pure safe reports return before this point. Any reply
    # that reaches ordinary moderation is therefore either an ordinary reply
    # or a report that contains its own independent violation.
    #
    # Route only that runtime path through Deep AI so Fast SAFE cannot miss a
    # clear interpersonal attack. This preserves the report-aware Fast path for
    # direct ModeratorAgent callers and keeps reporter protection intact.
    force_reply_deep = (
        report_preflight is not None
        and (
            not report_preflight.report_target
            or report_preflight.reporter_has_independent_violation
        )
    )

    if force_reply_deep:
        decision = await moderator_agent.analyze(
            context,
            force_deep=force_reply_deep,
        )
    else:
        # Preserve the ordinary moderation path exactly. This keeps semantic
        # clustering observational and only forces Deep AI for reply-conflict
        # cases that already passed report preflight.
        decision = await moderator_agent.analyze(context)

    # If preflight found a report PLUS a separate violation in the reply,
    # ordinary moderation may act on the reporter, but the target still gets
    # its independent re-review afterward.
    if (
        report_preflight is not None
        and report_preflight.report_target
    ):
        decision = decision.model_copy(
            update={
                "report_target": True,
                "report_confidence": max(
                    decision.report_confidence,
                    report_preflight.confidence,
                    0.80,
                ),
                "report_reason": (
                    report_preflight.reason
                ),
            }
        )

    policy = policy_gate.evaluate(
        decision,
        context=context,
    )

    decision, policy = (
        await feedback_service.refine_gray_case(
            context=context,
            baseline_decision=decision,
            baseline_policy=policy,
            policy_gate=policy_gate,
        )
    )

    decision, policy = (
        await community_policy_service.apply_overlay(
            context=context,
            baseline_decision=decision,
            baseline_policy=policy,
            policy_gate=policy_gate,
        )
    )

    raid_result = await raid_guard_service.inspect_before_execution(
        context=context,
        decision=decision,
        policy=policy,
        prepared=prepared_semantic,
    )

    result = (
        await moderation_executor.execute(
            context=context,
            decision=decision,
            policy=policy,
        )
    )

    observation_prepared = raid_result.prepared or prepared_semantic
    if observation_prepared is not None:
        await semantic_cluster_service.observe_once(
            context=context,
            decision=decision,
            policy=policy,
            prepared=observation_prepared,
        )
    else:
        semantic_cluster_service.enqueue(
            context=context,
            decision=decision,
            policy=policy,
        )

    logger.info(
        "MODERATION | "
        "message=%s | user=%s | "
        "current_violation=%s | "
        "category=%s | severity=%s | "
        "LLM=%s | policy=%s | "
        "confidence=%.2f | "
        "report_target=%s | "
        "report_conf=%.2f | "
        "event=%s",
        message.message_id,
        (
            message.from_user.id
            if message.from_user
            else None
        ),
        decision.current_message_violation,
        decision.category,
        decision.severity,
        decision.action,
        policy.final_action,
        decision.confidence,
        decision.report_target,
        decision.report_confidence,
        result.audit_event_key,
    )

    if (
        decision.report_target
        and context.reply_target_message
        is not None
    ):
        await process_report_rereview(
            context=context,
            moderator_agent=(
                moderator_agent
            ),
            policy_gate=policy_gate,
            community_policy_service=(
                community_policy_service
            ),
            feedback_service=(
                feedback_service
            ),
            raid_guard_service=(
                raid_guard_service
            ),
            semantic_cluster_service=(
                semantic_cluster_service
            ),
            moderation_executor=(
                moderation_executor
            ),
        )


@router.my_chat_member()
async def register_managed_chat(
    event: ChatMemberUpdated,
    bot: Bot,
    control_repository: ControlRepository,
    pilot_access_service=None,
) -> None:
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if event.new_chat_member.user.id != bot.id:
        return

    status = event.new_chat_member.status
    if status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        await control_repository.mark_chat_available(
            chat_id=event.chat.id,
        )
        await control_repository.ensure_chat_settings(
            chat_id=event.chat.id,
            chat_title=event.chat.title,
        )
        logger.info(
            "MANAGED CHAT REGISTERED | chat=%s | title=%s | status=%s",
            event.chat.id,
            event.chat.title,
            getattr(status, "value", status),
        )
        if pilot_access_service is not None and event.from_user is not None:
            try:
                await pilot_access_service.assign_chat_from_actor(
                    chat_id=event.chat.id,
                    chat_title=event.chat.title or str(event.chat.id),
                    actor_user_id=event.from_user.id,
                )
            except Exception:
                logger.exception(
                    "Pilot renter chat assignment failed | chat=%s | actor=%s",
                    event.chat.id,
                    event.from_user.id,
                )
        return

    if status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await control_repository.mark_chat_unavailable(
            chat_id=event.chat.id,
            reason=f"my_chat_member status={getattr(status, 'value', status)}",
        )
        logger.info(
            "MANAGED CHAT UNAVAILABLE | chat=%s | title=%s | status=%s",
            event.chat.id,
            event.chat.title,
            getattr(status, "value", status),
        )



@router.message(F.migrate_to_chat_id)
async def register_group_migration_from_old_chat(
    message: Message,
    control_repository: ControlRepository,
    pilot_access_service=None,
) -> None:
    """Telegram service event emitted in the obsolete basic group."""
    if not message.migrate_to_chat_id:
        return
    new_chat_id = await control_repository.register_chat_migration(
        old_chat_id=message.chat.id,
        new_chat_id=int(message.migrate_to_chat_id),
        chat_title=message.chat.title,
    )
    if pilot_access_service is not None:
        try:
            await pilot_access_service.migrate_chat_assignment(
                old_chat_id=message.chat.id,
                new_chat_id=new_chat_id,
                chat_title=message.chat.title,
            )
        except Exception:
            logger.exception("Pilot chat assignment migration failed")
    logger.info(
        "CHAT MIGRATION REGISTERED | old=%s | new=%s | source=migrate_to",
        message.chat.id,
        new_chat_id,
    )


@router.message(F.migrate_from_chat_id)
async def register_group_migration_from_new_chat(
    message: Message,
    control_repository: ControlRepository,
    pilot_access_service=None,
) -> None:
    """Telegram service event emitted in the new supergroup."""
    if not message.migrate_from_chat_id:
        return
    new_chat_id = await control_repository.register_chat_migration(
        old_chat_id=int(message.migrate_from_chat_id),
        new_chat_id=message.chat.id,
        chat_title=message.chat.title,
    )
    if pilot_access_service is not None:
        try:
            await pilot_access_service.migrate_chat_assignment(
                old_chat_id=int(message.migrate_from_chat_id),
                new_chat_id=new_chat_id,
                chat_title=message.chat.title,
            )
        except Exception:
            logger.exception("Pilot chat assignment migration failed")
    logger.info(
        "CHAT MIGRATION REGISTERED | old=%s | new=%s | source=migrate_from",
        message.migrate_from_chat_id,
        new_chat_id,
    )


@router.message(
    GROUP_CHAT
)
async def observe_message(
    message: Message,
    bot: Bot,
    context_builder: MessageContextBuilder,
    moderator_agent: ModeratorAgent,
    policy_gate: PolicyGate,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    raid_guard_service: RaidGuardService,
    semantic_cluster_service: SemanticClusterService,
    control_repository: ControlRepository,
    moderation_executor: ModerationExecutor,
    chat_locks: ChatLockManager,
    fake_admin_detector: FakeAdminDetector,
    safety_circuit=None,
    pilot_access_service=None,
) -> None:
    try:
        await process_message(
        message=message,
        bot=bot,
        context_builder=context_builder,
        moderator_agent=(
            moderator_agent
        ),
        policy_gate=policy_gate,
        community_policy_service=(
            community_policy_service
        ),
        feedback_service=(
            feedback_service
        ),
        raid_guard_service=(
            raid_guard_service
        ),
        semantic_cluster_service=(
            semantic_cluster_service
        ),
        control_repository=control_repository,
        moderation_executor=(
            moderation_executor
        ),
        chat_locks=chat_locks,
            fake_admin_detector=fake_admin_detector,
            pilot_access_service=pilot_access_service,
            edited=False,
        )
    except Exception as exc:
        logger.exception(
            "MODERATION PIPELINE FAILED | chat=%s | message=%s",
            message.chat.id,
            message.message_id,
        )
        if safety_circuit is not None:
            try:
                await safety_circuit.record_pipeline_failure(
                    chat_id=message.chat.id,
                    component="message_pipeline",
                    error=exc,
                )
            except Exception:
                logger.exception(
                    "Safety circuit could not record pipeline failure | chat=%s",
                    message.chat.id,
                )


@router.edited_message(
    GROUP_CHAT
)
async def observe_edited_message(
    message: Message,
    bot: Bot,
    context_builder: MessageContextBuilder,
    moderator_agent: ModeratorAgent,
    policy_gate: PolicyGate,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    raid_guard_service: RaidGuardService,
    semantic_cluster_service: SemanticClusterService,
    control_repository: ControlRepository,
    moderation_executor: ModerationExecutor,
    chat_locks: ChatLockManager,
    fake_admin_detector: FakeAdminDetector,
    safety_circuit=None,
    pilot_access_service=None,
) -> None:
    try:
        await process_message(
        message=message,
        bot=bot,
        context_builder=context_builder,
        moderator_agent=(
            moderator_agent
        ),
        policy_gate=policy_gate,
        community_policy_service=(
            community_policy_service
        ),
        feedback_service=(
            feedback_service
        ),
        raid_guard_service=(
            raid_guard_service
        ),
        semantic_cluster_service=(
            semantic_cluster_service
        ),
        control_repository=control_repository,
        moderation_executor=(
            moderation_executor
        ),
        chat_locks=chat_locks,
            fake_admin_detector=fake_admin_detector,
            pilot_access_service=pilot_access_service,
            edited=True,
        )
    except Exception as exc:
        logger.exception(
            "EDITED MODERATION PIPELINE FAILED | chat=%s | message=%s",
            message.chat.id,
            message.message_id,
        )
        if safety_circuit is not None:
            try:
                await safety_circuit.record_pipeline_failure(
                    chat_id=message.chat.id,
                    component="edited_message_pipeline",
                    error=exc,
                )
            except Exception:
                logger.exception(
                    "Safety circuit could not record edited pipeline failure | chat=%s",
                    message.chat.id,
                )
