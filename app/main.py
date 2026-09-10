import asyncio
import logging
import os

from aiogram import (
    Bot,
    Dispatcher,
)
from aiogram.exceptions import TelegramNetworkError

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.admin.dashboard import DashboardService
from app.admin.notifications import AdminNotifier
from app.agent.context import MessageContextBuilder
from app.agent.moderator import ModeratorAgent
from app.agent.schemas import ModerationDecision
from app.bot.admin_handlers import (
    router as admin_router,
)
from app.bot.handlers import (
    router as moderation_router,
)
from app.community_policy.service import CommunityPolicyService
from app.feedback.service import ModeratorFeedbackService
from app.config import settings
from app.database.db import Database
from app.database.repository import (
    AuditRepository,
    MessageRepository,
)
from app.llm.ollama import OllamaProvider
from app.moderation.executor import ModerationExecutor
from app.moderation.policy import PolicyGate
from app.raid_guard.service import RaidGuardService
from app.semantic_clustering.ollama_embeddings import OllamaEmbeddingProvider
from app.semantic_clustering.service import SemanticClusterService
from app.utils.alert_throttle import AdminAlertThrottle
from app.utils.chat_locks import ChatLockManager
from app.utils.decision_cache import AsyncDecisionCache


async def telegram_get_me_with_retry(
    bot: Bot,
    *,
    attempts: int = 6,
    base_delay_seconds: float = 2.0,
):
    """
    Resolve bot identity with bounded retry for transient Telegram/VPN outages.

    aiogram polling already has its own network recovery once it is running;
    this protects the initial getMe call that happens before polling starts.
    """

    attempts = max(
        1,
        int(attempts),
    )

    delay = max(
        0.0,
        float(base_delay_seconds),
    )

    last_error = None

    for attempt in range(
        1,
        attempts + 1,
    ):
        try:
            return await bot.get_me()

        except TelegramNetworkError as exc:
            last_error = exc

            if attempt >= attempts:
                break

            logging.warning(
                "Telegram unavailable during startup | "
                "attempt=%s/%s | retry_in=%.1fs | %s",
                attempt,
                attempts,
                delay,
                exc,
            )

            if delay > 0:
                await asyncio.sleep(
                    delay
                )

            delay = min(
                max(
                    delay * 2,
                    1.0,
                ),
                20.0,
            )

    assert last_error is not None

    logging.error(
        "Telegram startup connection failed "
        "after %s attempts.",
        attempts,
    )

    raise last_error



async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
    )

    database = Database(
        settings.database_url
    )

    await database.init()

    message_repository = MessageRepository(
        database.session_factory
    )

    audit_repository = AuditRepository(
        database.session_factory
    )

    control_repository = ControlRepository(
        database.session_factory
    )

    context_builder = MessageContextBuilder(
        message_repository,
        audit_repository,
        chat_history_limit=(
            settings.chat_history_limit
        ),
        user_history_limit=(
            settings.user_history_limit
        ),
        moderation_history_limit=(
            settings.moderation_history_limit
        ),
    )

    fast_provider = OllamaProvider(
        base_url=(
            settings.ollama_base_url
        ),
        model=(
            settings.ollama_fast_model
        ),
        timeout_seconds=(
            settings.llm_timeout_seconds
        ),
        think=(
            settings.ollama_think
        ),
        keep_alive=(
            settings.ollama_keep_alive
        ),
        num_ctx=(
            settings.fast_num_ctx
        ),
        num_predict=(
            settings.fast_num_predict
        ),
        name="fast-triage",
    )

    deep_provider = OllamaProvider(
        base_url=(
            settings.ollama_base_url
        ),
        model=(
            settings.ollama_model
        ),
        timeout_seconds=(
            settings.llm_timeout_seconds
        ),
        think=(
            settings.ollama_think
        ),
        keep_alive=(
            settings.ollama_keep_alive
        ),
        num_ctx=(
            settings.deep_num_ctx
        ),
        num_predict=(
            settings.deep_num_predict
        ),
        name="deep-moderator",
    )

    # Same Deep model, separate response budget for infrequent policy editing.
    # This does not change normal moderation latency or prediction limits.
    policy_compiler_provider = OllamaProvider(
        base_url=(
            settings.ollama_base_url
        ),
        model=(
            settings.ollama_model
        ),
        timeout_seconds=(
            settings.llm_timeout_seconds
        ),
        think=(
            settings.ollama_think
        ),
        keep_alive=(
            settings.ollama_keep_alive
        ),
        num_ctx=(
            settings.deep_num_ctx
        ),
        num_predict=max(
            settings.deep_num_predict,
            900,
        ),
        name="policy-compiler",
    )

    if settings.warmup_models_on_start:
        logging.info(
            "Warming up local LLMs "
            "before polling..."
        )

        await fast_provider.warmup()
        await deep_provider.warmup()

    decision_cache = (
        AsyncDecisionCache[
            ModerationDecision
        ](
            ttl_seconds=(
                settings
                .decision_cache_ttl_seconds
            )
        )
    )

    moderator_agent = ModeratorAgent(
        deep_provider,
        fast_provider=fast_provider,
        cache=decision_cache,
        control_repository=(
            control_repository
        ),
    )

    community_policy_service = CommunityPolicyService(
        repository=control_repository,
        deep_provider=deep_provider,
        fast_provider=fast_provider,
        compiler_provider=(
            policy_compiler_provider
        ),
    )

    feedback_service = ModeratorFeedbackService(
        repository=control_repository,
        deep_provider=deep_provider,
        max_examples=12,
    )

    semantic_embedding_provider = OllamaEmbeddingProvider(
        base_url=settings.ollama_base_url,
        model=os.getenv(
            "OLLAMA_EMBEDDING_MODEL",
            "qwen3-embedding:0.6b",
        ),
        timeout_seconds=min(
            settings.llm_timeout_seconds,
            45,
        ),
        keep_alive=settings.ollama_keep_alive,
    )

    semantic_cluster_service = SemanticClusterService(
        repository=control_repository,
        embedding_provider=semantic_embedding_provider,
        similarity_threshold=0.88,
        window_seconds=600,
        min_cluster_size=3,
        queue_size=128,
        recent_limit=160,
        enabled=(
            os.getenv(
                "SEMANTIC_CLUSTERING_ENABLED",
                "true",
            ).strip().casefold()
            not in {"0", "false", "off", "no"}
        ),
    )

    await semantic_cluster_service.warmup()
    await semantic_cluster_service.start()

    policy_gate = PolicyGate(
        ban_threshold=(
            settings
            .autonomous_ban_threshold
        ),
        mute_threshold=(
            settings
            .autonomous_mute_threshold
        ),
        delete_threshold=(
            settings
            .autonomous_delete_threshold
        ),
        warn_threshold=(
            settings
            .autonomous_warn_threshold
        ),
    )

    bot = Bot(
        token=(
            settings.telegram_bot_token
        )
    )

    raid_guard_service = RaidGuardService(
        repository=control_repository,
        semantic_service=semantic_cluster_service,
        bot=bot,
        admin_ids=settings.admin_id_list,
        similarity_threshold=0.92,
        window_seconds=90,
        min_messages=4,
        min_users=3,
        hard_ratio_threshold=0.75,
    )

    notifier = AdminNotifier(
        bot=bot,
        admin_ids=(
            settings.admin_id_list
        ),
    )

    dashboard_service = DashboardService(
        bot=bot,
        repository=(
            control_repository
        ),
        admin_ids=(
            settings.admin_id_list
        ),
        global_dry_run=(
            settings.dry_run
        ),
        live_delete_enabled=(
            settings.live_delete_enabled
        ),
        raid_guard_available=(
            semantic_cluster_service.enabled
        ),
    )

    alert_throttle = (
        AdminAlertThrottle(
            cooldown_seconds=(
                settings
                .admin_alert_cooldown_seconds
            )
        )
    )

    moderation_executor = (
        ModerationExecutor(
            audit_repository=(
                audit_repository
            ),
            notifier=notifier,
            bot=bot,
            dry_run=(
                settings.dry_run
            ),
            live_delete_enabled=(
                settings
                .live_delete_enabled
            ),
            live_delete_categories=(
                settings
                .live_delete_category_set
            ),
            live_delete_chat_ids=(
                settings
                .live_delete_chat_id_set
            ),
            admin_alert_dedup_seconds=(
                settings
                .admin_alert_cooldown_seconds
            ),
            admin_alert_throttle=(
                alert_throttle
            ),
            control_repository=(
                control_repository
            ),
            dashboard_service=(
                dashboard_service
            ),
            notify_autonomous_actions=(
                settings
                .notify_autonomous_actions
            ),
            notify_new_tickets=(
                settings.notify_new_tickets
            ),
        )
    )

    chat_locks = ChatLockManager()

    dispatcher = Dispatcher()

    dispatcher.include_router(
        admin_router
    )

    dispatcher.include_router(
        moderation_router
    )

    try:
        me = await telegram_get_me_with_retry(
            bot,
            attempts=6,
            base_delay_seconds=2.0,
        )

        logging.info(
            "ModGuard started | "
            "id=%s | username=@%s",
            me.id,
            me.username,
        )

        logging.info(
            "Fast=%s | Deep=%s | "
            "think=%s | keep_alive=%s",
            settings.ollama_fast_model,
            settings.ollama_model,
            settings.ollama_think,
            settings.ollama_keep_alive,
        )

        logging.info(
            "Moderation mode: %s",
            (
                "DRY RUN"
                if settings.dry_run
                else "LIVE"
            ),
        )

        logging.info(
            "Report re-review ready | "
            "new ticket alerts=%s",
            settings.notify_new_tickets,
        )

        logging.info(
            "Community policy overlay ready | core baseline preserved"
        )

        logging.info(
            "Moderator feedback memory ready | "
            "chat-scoped | TEST tickets excluded"
        )

        logging.info(
            "Semantic campaigns ready | observational=%s | model=%s",
            semantic_cluster_service.enabled,
            semantic_embedding_provider.model,
        )

        logging.info(
            "Raid Guard ready | per-chat default=OFF | "
            "threshold=0.92 | min_messages=4 | min_users=3"
        )

        await dispatcher.start_polling(
            bot,
            allowed_updates=(
                dispatcher
                .resolve_used_update_types()
            ),
            context_builder=(
                context_builder
            ),
            moderator_agent=(
                moderator_agent
            ),
            policy_gate=(
                policy_gate
            ),
            moderation_executor=(
                moderation_executor
            ),
            chat_locks=(
                chat_locks
            ),
            dashboard_service=(
                dashboard_service
            ),
            control_repository=(
                control_repository
            ),
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
            app_settings=settings,
        )

    finally:
        # get_me() may fail before polling ever starts. Cleanup must still run.
        try:
            await bot.session.close()
        except Exception:
            logging.exception(
                "Telegram session cleanup failed"
            )

        await semantic_cluster_service.close()
        await fast_provider.close()
        await deep_provider.close()
        await policy_compiler_provider.close()
        await database.dispose()
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())