import html
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.admin.control_repository import ControlRepository
from app.agent.schemas import MessageContext, ModerationDecision, PolicyEvaluation
from app.raid_guard.schemas import RaidGuardResult
from app.semantic_clustering.service import SemanticClusterService, SemanticPreparedSignal


logger = logging.getLogger(__name__)


# Screenshot principle: autonomous destructive action requires a high-confidence,
# objective threat. Context-heavy categories are not enough to create a raid.
HARD_CAMPAIGN_CATEGORIES = {
    "scam",
    "phishing",
    "malicious_link",
    "spam",
    "threat",
}

DELETE_ONLY_CAMPAIGN_CATEGORIES = {
    "unsolicited_advertising",
}

SOFT_CONTEXTUAL_CATEGORIES = {
    "harassment",
    "hate",
    "adult_content",
    "impersonation",
    "evasion_attempt",
    "other",
}


class RaidGuardService:
    """
    Conservative semantic campaign protection.

    Requirements before a real raid incident:
    - Raid Guard enabled for THIS chat
    - semantic similarity >= 0.92
    - >= 4 messages in the cluster in 90 seconds
    - >= 3 unique users
    - >= 75% of cluster messages independently classified by Core as
      high-confidence hard/objective violations

    A high-confidence SAFE current message is never overridden by campaign data.
    """

    def __init__(
        self,
        *,
        repository: ControlRepository,
        semantic_service: SemanticClusterService,
        bot: Bot,
        admin_ids: list[int],
        similarity_threshold: float = 0.92,
        window_seconds: int = 90,
        min_messages: int = 4,
        min_users: int = 3,
        hard_ratio_threshold: float = 0.75,
    ):
        self.repository = repository
        self.semantic_service = semantic_service
        self.bot = bot
        self.admin_ids = set(admin_ids)
        self.similarity_threshold = max(
            0.0, min(1.0, similarity_threshold)
        )
        self.window_seconds = max(30, int(window_seconds))
        self.min_messages = max(3, int(min_messages))
        self.min_users = max(2, int(min_users))
        self.hard_ratio_threshold = max(
            0.5, min(1.0, hard_ratio_threshold)
        )

    @staticmethod
    def _row_is_hard(row) -> bool:
        return bool(
            row.current_violation
            and row.decision_category in HARD_CAMPAIGN_CATEGORIES
            and (row.confidence or 0.0) >= 0.90
            and row.final_action in {"delete", "mute", "ban"}
        )

    @staticmethod
    def _row_is_deletable_campaign(row) -> bool:
        return bool(
            row.current_violation
            and row.decision_category
            in (HARD_CAMPAIGN_CATEGORIES | DELETE_ONLY_CAMPAIGN_CATEGORIES)
            and (row.confidence or 0.0) >= 0.90
            and row.final_action in {"delete", "mute", "ban"}
        )

    async def inspect_before_execution(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        prepared: SemanticPreparedSignal | None = None,
    ) -> RaidGuardResult:
        chat_id = context.current_message.chat_id

        if not await self.repository.get_raid_guard_enabled(chat_id):
            return RaidGuardResult()

        if not self.semantic_service.enabled:
            return RaidGuardResult()

        # Preserve the strongest false-positive guard: confidently safe current
        # messages are never punished by historical/campaign context.
        if (
            not decision.current_message_violation
            and decision.confidence >= 0.90
        ):
            return RaidGuardResult()

        if prepared is None:
            try:
                prepared = await self.semantic_service.prepare_signal(
                    context=context
                )
            except Exception:
                logger.exception(
                    "RAID GUARD semantic lookup failed; stable moderation preserved"
                )
                return RaidGuardResult()

        if (
            prepared is None
            or prepared.cluster_key is None
            or prepared.similarity < self.similarity_threshold
        ):
            return RaidGuardResult(prepared=prepared)

        since = datetime.now(timezone.utc) - timedelta(
            seconds=self.window_seconds
        )
        rows = await self.repository.semantic_cluster_records(
            chat_id=chat_id,
            cluster_key=prepared.cluster_key,
            since=since,
            limit=200,
        )

        # The current message is not stored yet, so evaluate it as one extra row.
        unique_users = {
            row.user_id
            for row in rows
            if row.user_id is not None
        }
        if context.current_message.user_id is not None:
            unique_users.add(context.current_message.user_id)

        hard_rows = [row for row in rows if self._row_is_hard(row)]
        current_hard = bool(
            decision.current_message_violation
            and decision.category in HARD_CAMPAIGN_CATEGORIES
            and decision.confidence >= 0.90
        )
        hard_count = len(hard_rows) + (1 if current_hard else 0)
        message_count = len(rows) + 1
        hard_ratio = hard_count / max(1, message_count)

        qualifies = bool(
            message_count >= self.min_messages
            and len(unique_users) >= self.min_users
            and hard_ratio >= self.hard_ratio_threshold
        )

        if not qualifies:
            return RaidGuardResult(
                prepared=prepared,
                cluster_key=prepared.cluster_key,
                similarity=prepared.similarity,
                message_count=message_count,
                unique_users=len(unique_users),
                hard_ratio=hard_ratio,
            )

        # Campaign signal may strengthen only a gray/suspicious current message.
        # It never turns an explicit safe message into a violation.
        if (
            policy.final_action in {"allow"}
            and not decision.current_message_violation
        ):
            return RaidGuardResult(
                prepared=prepared,
                cluster_key=prepared.cluster_key,
                similarity=prepared.similarity,
                message_count=message_count,
                unique_users=len(unique_users),
                hard_ratio=hard_ratio,
            )

        settings = await self.repository.get_chat_settings(chat_id)
        auto_ban = await self.repository.get_live_ban_enabled(chat_id)

        deletable_rows = [
            row for row in rows
            if self._row_is_deletable_campaign(row)
        ]
        affected_messages = sorted(
            {
                row.telegram_message_id
                for row in deletable_rows
            }
        )
        affected_users = sorted(
            {
                row.user_id
                for row in hard_rows
                if row.user_id is not None
            }
        )

        existing_incident = (
            await self.repository.get_open_raid_incident_for_cluster(
                chat_id=chat_id,
                cluster_key=prepared.cluster_key,
            )
        )

        existing_users: set[int] = set()
        existing_messages: set[int] = set()
        prior_deleted = 0
        prior_banned = 0

        if existing_incident is not None:
            try:
                existing_users = {
                    int(value)
                    for value in json.loads(
                        existing_incident.affected_user_ids_json or "[]"
                    )
                }
            except Exception:
                existing_users = set()
            try:
                existing_messages = {
                    int(value)
                    for value in json.loads(
                        existing_incident.affected_message_ids_json or "[]"
                    )
                }
            except Exception:
                existing_messages = set()
            prior_deleted = existing_incident.deleted_messages
            prior_banned = existing_incident.banned_users

        new_messages = [
            message_id
            for message_id in affected_messages
            if message_id not in existing_messages
        ]
        new_users = [
            user_id
            for user_id in affected_users
            if user_id not in existing_users
        ]

        deleted = 0
        banned = 0

        if not settings.shadow_mode:
            # Batch cleanup touches only PREVIOUS messages that already had
            # independent high-confidence hard/delete-only decisions. The
            # current message is left to the normal Executor to avoid duplicate
            # action/audit races.
            for message_id in new_messages:
                try:
                    await self.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    deleted += 1
                except TelegramBadRequest:
                    # Usually already deleted by the normal Executor.
                    pass
                except TelegramAPIError:
                    logger.warning(
                        "RAID GUARD delete failed | chat=%s | message=%s",
                        chat_id,
                        message_id,
                        exc_info=True,
                    )

            if auto_ban:
                for user_id in new_users:
                    try:
                        await self.bot.ban_chat_member(
                            chat_id=chat_id,
                            user_id=user_id,
                        )
                        banned += 1
                    except TelegramAPIError:
                        logger.warning(
                            "RAID GUARD ban failed | chat=%s | user=%s",
                            chat_id,
                            user_id,
                            exc_info=True,
                        )

        incident = await self.repository.create_or_update_raid_incident(
            chat_id=chat_id,
            cluster_key=prepared.cluster_key,
            shadow_mode=settings.shadow_mode,
            auto_ban_enabled=auto_ban,
            similarity=prepared.similarity,
            message_count=message_count,
            unique_users=len(unique_users),
            affected_user_ids=affected_users,
            affected_message_ids=affected_messages,
            deleted_messages=prior_deleted + deleted,
            banned_users=prior_banned + banned,
        )

        logger.warning(
            "RAID GUARD TRIGGER | chat=%s | incident=%s | campaign=%s | "
            "similarity=%.3f | messages=%s | users=%s | hard_ratio=%.2f | "
            "shadow=%s | deleted=%s | banned=%s",
            chat_id,
            incident.incident_key,
            prepared.cluster_key,
            prepared.similarity,
            message_count,
            len(unique_users),
            hard_ratio,
            settings.shadow_mode,
            deleted,
            banned,
        )

        if existing_incident is None:
            await self._notify_incident(incident)

        return RaidGuardResult(
            triggered=True,
            incident_id=incident.id,
            incident_key=incident.incident_key,
            cluster_key=prepared.cluster_key,
            similarity=prepared.similarity,
            message_count=message_count,
            unique_users=len(unique_users),
            hard_ratio=hard_ratio,
            shadow=settings.shadow_mode,
            prepared=prepared,
            deleted_messages=deleted,
            banned_users=banned,
            affected_users=affected_users,
            affected_messages=affected_messages,
        )

    async def _notify_incident(self, incident) -> None:
        mode = "SHADOW" if incident.shadow_mode else "LIVE"
        title = (
            "🛡 <b>RAID DETECTED · SHADOW</b>"
            if incident.shadow_mode
            else "🚨 <b>RAID STOPPED</b>"
        )
        action_line = (
            f"Would delete <b>{len(json.loads(incident.affected_message_ids_json or '[]'))}</b> · "
            f"Would ban <b>{len(json.loads(incident.affected_user_ids_json or '[]')) if incident.auto_ban_enabled else 0}</b>"
            if incident.shadow_mode
            else (
                f"Deleted <b>{incident.deleted_messages}</b> · "
                f"Banned <b>{incident.banned_users}</b>"
            )
        )
        text = (
            f"{title}\n"
            f"{html.escape(incident.incident_key)} · {mode}\n\n"
            f"Campaign <code>{html.escape(incident.cluster_key)}</code>\n"
            f"Similarity <b>{incident.similarity:.0%}</b>\n"
            f"Messages <b>{incident.message_count}</b> · Users <b>{incident.unique_users}</b>\n"
            f"{action_line}\n\n"
            "Raid Guard only used independently confirmed hard-risk campaign evidence."
        )

        rows = [
            [
                InlineKeyboardButton(
                    text="View details",
                    callback_data=f"mg:raid_incident:{incident.id}",
                )
            ]
        ]
        if incident.banned_users > 0:
            rows.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="↩ Unban users",
                        callback_data=f"mg:raid_unban:{incident.id}",
                    )
                ],
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except TelegramAPIError:
                logger.warning(
                    "Could not send Raid Guard alert",
                    exc_info=True,
                )

    async def unban_incident_users(
        self,
        *,
        incident_id: int,
    ) -> tuple[int, int]:
        incident = await self.repository.get_raid_incident(
            incident_id
        )
        if incident is None:
            return 0, 0

        user_ids = [
            int(value)
            for value in json.loads(
                incident.affected_user_ids_json or "[]"
            )
        ]
        already = {
            int(value)
            for value in json.loads(
                incident.unbanned_user_ids_json or "[]"
            )
        }

        success = []
        failed = 0
        for user_id in user_ids:
            if user_id in already:
                continue
            try:
                await self.bot.unban_chat_member(
                    chat_id=incident.chat_id,
                    user_id=user_id,
                    only_if_banned=True,
                )
                success.append(user_id)
            except TelegramAPIError:
                failed += 1
                logger.warning(
                    "Raid rollback unban failed | incident=%s | user=%s",
                    incident.incident_key,
                    user_id,
                    exc_info=True,
                )

        if success:
            await self.repository.mark_raid_unbanned(
                incident_id=incident_id,
                unbanned_user_ids=success,
            )

        logger.info(
            "RAID ROLLBACK | incident=%s | unbanned=%s | failed=%s",
            incident.incident_key,
            len(success),
            failed,
        )
        return len(success), failed
