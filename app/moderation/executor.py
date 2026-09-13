import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.admin.control_repository import ControlRepository
from app.admin.dashboard import DashboardService
from app.admin.notifications import AdminNotifier
from app.agent.schemas import (
    ExecutionResult,
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)
from app.database.repository import AuditRepository
from app.moderation.actions import (
    get_action_name,
    is_reversible,
)
from app.moderation.safety_circuit import SafetyCircuitBreaker
from app.utils.alert_throttle import AdminAlertThrottle


logger = logging.getLogger(__name__)


MEMORY_CATEGORIES = {
    "spam",
    "scam",
    "phishing",
    "malicious_link",
    "unsolicited_advertising",
}


class ModerationExecutor:
    def __init__(
        self,
        *,
        audit_repository: AuditRepository,
        notifier: AdminNotifier,
        bot: Bot | None = None,
        dry_run: bool = True,
        live_delete_enabled: bool = False,
        live_delete_categories: set[str] | None = None,
        live_delete_chat_ids: set[int] | None = None,
        admin_alert_dedup_seconds: int = 60,
        admin_alert_throttle: AdminAlertThrottle | None = None,
        control_repository: ControlRepository | None = None,
        dashboard_service: DashboardService | None = None,
        notify_autonomous_actions: bool = False,
        notify_new_tickets: bool = True,
        safety_circuit: SafetyCircuitBreaker | None = None,
    ):
        self.audit_repository = audit_repository
        self.notifier = notifier
        self.bot = bot
        self.dry_run = dry_run

        self.live_delete_enabled = (
            live_delete_enabled
        )

        self.live_delete_categories = (
            live_delete_categories
            or {
                "spam",
                "scam",
                "phishing",
                "malicious_link",
                "unsolicited_advertising",
                "flood",
            }
        )

        self.live_delete_chat_ids = (
            live_delete_chat_ids
            or set()
        )

        self.alert_throttle = (
            admin_alert_throttle
            or AdminAlertThrottle(
                cooldown_seconds=(
                    admin_alert_dedup_seconds
                )
            )
        )

        self.control_repository = (
            control_repository
        )

        self.dashboard_service = (
            dashboard_service
        )

        self.notify_autonomous_actions = (
            notify_autonomous_actions
        )

        self.notify_new_tickets = (
            notify_new_tickets
        )

        self.safety_circuit = safety_circuit

    async def _shadow_mode(
        self,
        context: MessageContext,
    ) -> bool:
        if self.control_repository is None:
            return False

        settings = (
            await self.control_repository
            .ensure_chat_settings(
                chat_id=(
                    context
                    .current_message
                    .chat_id
                ),
            )
        )

        return bool(
            settings.shadow_mode
        )

    async def _live_ban_enabled(
        self,
        chat_id: int,
    ) -> bool:
        if self.control_repository is None:
            return False

        return await self.control_repository.get_live_ban_enabled(
            chat_id
        )

    async def _mute_duration_minutes(self, chat_id: int) -> int:
        if self.control_repository is None:
            return 60
        try:
            return int(await self.control_repository.get_mute_duration_minutes(chat_id))
        except Exception:
            logger.exception("Could not load mute duration; using 60 minutes")
            return 60

    async def _target_can_be_restricted(
        self,
        *,
        chat_id: int,
        user_id: int,
    ) -> tuple[bool, str | None]:
        """Telegram does not allow bots to restrict/ban chat owners/admins."""
        if self.bot is None:
            return False, "Telegram Bot instance is unavailable"
        # Real aiogram Bot always exposes get_chat_member. Some lightweight
        # test doubles and alternative Bot-compatible adapters do not. In that
        # compatibility case we cannot preflight the role, so let Telegram (or
        # the adapter) remain the source of truth for the actual action call.
        get_chat_member = getattr(
            self.bot,
            "get_chat_member",
            None,
        )

        if not callable(get_chat_member):
            logger.debug(
                "Restriction precheck unavailable | chat=%s | user=%s | "
                "reason=get_chat_member_missing",
                chat_id,
                user_id,
            )
            return True, None

        try:
            member = await get_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )
            status = str(getattr(member, "status", ""))
            protected = {
                str(ChatMemberStatus.CREATOR),
                str(ChatMemberStatus.ADMINISTRATOR),
                "creator",
                "administrator",
            }
            if status in protected or getattr(member, "is_chat_owner", False):
                return False, f"target is protected chat member ({status})"
            return True, None
        except TelegramAPIError as exc:
            # A failed precheck must not disable moderation entirely; Telegram
            # remains the source of truth for the actual action attempt.
            logger.warning(
                "Restriction precheck failed | chat=%s | user=%s | %s",
                chat_id,
                user_id,
                exc,
            )
            return True, None

    async def _mute_user(
        self, *, chat_id: int, user_id: int, minutes: int
    ) -> tuple[bool, str | None]:
        if self.bot is None:
            return False, "Telegram Bot instance is unavailable"
        allowed, guard_reason = await self._target_can_be_restricted(
            chat_id=chat_id, user_id=user_id
        )
        if not allowed:
            logger.info(
                "LIVE MUTE SKIPPED | chat=%s | user=%s | reason=%s",
                chat_id, user_id, guard_reason,
            )
            return False, guard_reason
        try:
            await self.bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False, can_send_audios=False,
                    can_send_documents=False, can_send_photos=False,
                    can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False,
                    can_send_other_messages=False, can_add_web_page_previews=False,
                ),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes),
            )
            return True, None
        except TelegramAPIError as exc:
            message = str(exc)
            if "chat owner" in message.casefold() or "administrator" in message.casefold():
                logger.info("LIVE MUTE SKIPPED | chat=%s | user=%s | reason=%s", chat_id, user_id, message)
            else:
                logger.exception("LIVE MUTE FAILED | chat=%s | user=%s", chat_id, user_id)
            return False, message
        except Exception as exc:
            logger.exception("Unexpected LIVE MUTE failure | chat=%s | user=%s", chat_id, user_id)
            return False, str(exc)

    async def _ban_user(
        self,
        *,
        chat_id: int,
        user_id: int,
    ) -> tuple[bool, str | None]:
        if self.bot is None:
            return False, "Telegram Bot instance is unavailable"

        allowed, guard_reason = await self._target_can_be_restricted(
            chat_id=chat_id, user_id=user_id
        )
        if not allowed:
            logger.info(
                "LIVE BAN SKIPPED | chat=%s | user=%s | reason=%s",
                chat_id, user_id, guard_reason,
            )
            return False, guard_reason

        try:
            await self.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )
            return True, None

        except TelegramAPIError as exc:
            message = str(exc)
            if "chat owner" in message.casefold() or "administrator" in message.casefold():
                logger.info("LIVE BAN SKIPPED | chat=%s | user=%s | reason=%s", chat_id, user_id, message)
            else:
                logger.exception(
                    "LIVE BAN FAILED | chat=%s | user=%s",
                    chat_id,
                    user_id,
                )
            return False, message

        except Exception as exc:
            logger.exception(
                "Unexpected LIVE BAN failure | chat=%s | user=%s",
                chat_id,
                user_id,
            )
            return False, str(exc)

    async def _notify_shadow_action(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        live_ban_enabled: bool,
    ) -> bool:
        if (
            self.dashboard_service is None
            or policy.final_action == "allow"
        ):
            return False

        current = context.current_message

        if policy.final_action == "ban":
            if live_ban_enabled:
                action = (
                    "BAN + DELETE"
                    if (
                        self.live_delete_enabled
                        and policy.final_delete_message
                    )
                    else "BAN"
                )
            elif (
                self.live_delete_enabled
                and policy.final_delete_message
            ):
                action = "MUTE + DELETE · Auto-ban OFF"
            else:
                action = "BAN BLOCKED · Auto-ban OFF"

        elif policy.final_action == "delete":
            action = (
                "DELETE"
                if self.live_delete_enabled
                else "DELETE BLOCKED · Live cleanup OFF"
            )
        else:
            action = get_action_name(policy.final_action)

        alert_key = (
            f"shadow:{current.chat_id}:"
            f"{current.user_id}:"
            f"{decision.category}:"
            f"{action}"
        )

        should_notify, suppressed = await self.alert_throttle.reserve(
            alert_key
        )

        if not should_notify:
            return False

        who = (
            current.username
            or (
                f"user {current.user_id}"
                if current.user_id is not None
                else "unknown"
            )
        )

        snippet = (
            current.raw_text.strip().replace("\n", " ")[:240]
            or "[non-text message]"
        )

        policy_label = decision.category

        if policy.source == "community_policy":
            rules = (
                ", ".join(policy.matched_community_rules)
                or "custom"
            )
            version = (
                f"v{policy.community_policy_version}"
                if policy.community_policy_version is not None
                else "custom"
            )
            policy_label = f"policy {version} · {rules}"

        relationship = context.relationship_signals
        relationship_line = ""
        if (
            relationship.counterpart_user_id is not None
            and relationship.familiarity != "none"
        ):
            relationship_line = (
                "\n<b>Familiarity:</b> "
                f"{html.escape(relationship.familiarity)} · "
                f"{relationship.total_pair_replies} observed mutual replies"
            )

        text = (
            f"🛡 <b>SHADOW · WOULD {html.escape(action)}</b>\n"
            f"{html.escape(who)} · {html.escape(policy_label)} · "
            f"{decision.confidence:.0%}\n\n"
            f"<code>{html.escape(snippet)}</code>\n\n"
            f"<b>Why:</b> {html.escape(decision.reason[:500])}"
            f"{relationship_line}"
        )

        if suppressed:
            text += f"\n\nSuppressed similar alerts: {suppressed}"

        shadow_case = None
        if self.control_repository is not None:
            try:
                shadow_case = (
                    await self.control_repository.create_shadow_feedback_case(
                        chat_id=current.chat_id,
                        telegram_message_id=current.telegram_message_id,
                        target_user_id=current.user_id,
                        counterpart_user_id=(
                            context.reply_target_message.user_id
                            if context.reply_target_message is not None
                            else None
                        ),
                        username=current.username,
                        message_text=current.raw_text,
                        context={
                            "recent_chat_messages": [
                                {
                                    "user_id": item.user_id,
                                    "username": item.username,
                                    "text": item.raw_text,
                                    "reply_to_message_id": item.reply_to_message_id,
                                }
                                for item in context.recent_chat_messages[-8:]
                                if item.raw_text.strip()
                            ],
                            "reply_target": (
                                {
                                    "user_id": context.reply_target_message.user_id,
                                    "username": context.reply_target_message.username,
                                    "text": context.reply_target_message.raw_text,
                                }
                                if context.reply_target_message is not None
                                else None
                            ),
                            "relationship_signals": relationship.model_dump(),
                            "policy_source": policy.source,
                            "matched_community_rules": policy.matched_community_rules,
                        },
                        ai_action=policy.final_action,
                        ai_category=decision.category,
                        ai_severity=decision.severity,
                        ai_confidence=decision.confidence,
                        ai_reason=decision.reason,
                        policy_version=policy.community_policy_version,
                    )
                )
            except Exception:
                logger.exception(
                    "Could not persist Shadow feedback case | chat=%s | message=%s",
                    current.chat_id,
                    current.telegram_message_id,
                )

        keyboard_rows = []
        if shadow_case is not None:
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Agree",
                        callback_data=f"mg:sagree:{shadow_case.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Disagree",
                        callback_data=f"mg:sdisagree:{shadow_case.id}",
                    ),
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 Clear shadow alerts",
                    callback_data=f"mg:shadow_clear:{current.chat_id}",
                )
            ]
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=keyboard_rows
        )

        recipients = set(self.dashboard_service.admin_ids)
        access_service = getattr(
            self.dashboard_service,
            "access_service",
            None,
        )
        if access_service is not None:
            try:
                recipients.update(
                    int(item)
                    for item in await access_service.notification_recipients(
                        current.chat_id
                    )
                )
            except Exception:
                logger.exception(
                    "Could not resolve Shadow feedback recipients | chat=%s",
                    current.chat_id,
                )

        sent = False
        for admin_id in sorted(recipients):
            try:
                message = await self.dashboard_service.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                sent = True
                if self.control_repository is not None:
                    await self.control_repository.register_admin_alert_artifact(
                        managed_chat_id=current.chat_id,
                        admin_chat_id=admin_id,
                        telegram_message_id=message.message_id,
                        kind="shadow_alert",
                    )
            except TelegramAPIError:
                logger.warning(
                    "Could not send shadow action notification.",
                    exc_info=True,
                )

        return sent

    async def _send_user_warning(
        self,
        *,
        context: MessageContext,
        reason: str,
    ) -> tuple[bool, str | None]:
        if self.bot is None:
            return False, "Telegram Bot instance is unavailable"

        current = context.current_message

        warning_key = (
            f"user-warning:{current.chat_id}:"
            f"{current.user_id}:{reason}"
        )

        should_send, _ = await self.alert_throttle.reserve(
            warning_key
        )

        if not should_send:
            return True, None

        who = (
            f"@{current.username.lstrip('@')}"
            if current.username
            else (
                f"user {current.user_id}"
                if current.user_id is not None
                else "member"
            )
        )

        if reason == "flood":
            text = (
                f"⚠️ {who}, stop flooding the chat. Repeating it may result "
                "in a temporary mute and then a ban."
            )
        elif reason == "spam":
            text = (
                f"⚠️ {who}, stop spamming. Repeating it may result in a "
                "temporary mute and then a ban."
            )
        elif reason == "harassment":
            text = (
                f"⚠️ {who}, keep the discussion civil. Repeated targeted "
                "aggression may result in a mute and then a ban."
            )
        else:
            text = f"⚠️ {who}, please follow the community rules."

        try:
            await self.bot.send_message(
                chat_id=current.chat_id,
                text=text,
            )
            return True, None

        except TelegramAPIError as exc:
            logger.exception(
                "LIVE WARNING FAILED | chat=%s | user=%s",
                current.chat_id,
                current.user_id,
            )
            return False, str(exc)

        except Exception as exc:
            logger.exception(
                "Unexpected LIVE WARNING failure | "
                "chat=%s | user=%s",
                current.chat_id,
                current.user_id,
            )
            return False, str(exc)

    def _sustained_flood_context(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
    ) -> bool:
        # This is a behavior guard, not a text classifier.
        #
        # "other" is included because local LLMs occasionally describe a
        # clear high-frequency burst correctly in the reason/action while
        # failing to select the dedicated flood enum.
        return (
            context.behavior_signals.messages_last_60s
            >= 8
            and decision.category
            in {
                "flood",
                "spam",
                "other",
            }
        )

    def _chat_is_live_allowed(
        self,
        chat_id: int,
    ) -> bool:
        return (
            not self.live_delete_chat_ids
            or chat_id
            in self.live_delete_chat_ids
        )

    def _can_live_delete(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        shadow_mode: bool,
    ) -> bool:
        if (
            self.dry_run
            or shadow_mode
            or not self.live_delete_enabled
            or self.bot is None
        ):
            return False

        if not decision.current_message_violation:
            return False

        enforcement_includes_cleanup = (
            policy.final_action in {"mute", "ban"}
            and policy.final_delete_message
        )

        if (
            not enforcement_includes_cleanup
            and policy.source != "community_policy"
            and decision.category not in self.live_delete_categories
        ):
            return False

        if not self._chat_is_live_allowed(
            context.current_message.chat_id
        ):
            return False

        return bool(
            policy.final_delete_message
            or policy.final_action
            == "delete"
        )

    async def _delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
    ) -> tuple[
        bool,
        str | None,
    ]:
        if self.bot is None:
            return (
                False,
                "Telegram Bot instance is unavailable",
            )

        try:
            await self.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

            return True, None

        except TelegramAPIError as exc:
            logger.exception(
                "LIVE DELETE FAILED | "
                "chat=%s | message=%s",
                chat_id,
                message_id,
            )

            return False, str(exc)

        except Exception as exc:
            logger.exception(
                "Unexpected LIVE DELETE failure | "
                "chat=%s | message=%s",
                chat_id,
                message_id,
            )

            return False, str(exc)

    async def _notify_ticket_created(
        self,
        ticket,
    ) -> None:
        if (
            not self.notify_new_tickets
            or self.dashboard_service
            is None
        ):
            return

        if ticket.occurrence_count != 1:
            return

        who = (
            ticket.username
            or (
                f"user {ticket.target_user_id}"
                if ticket.target_user_id
                is not None
                else "unknown"
            )
        )

        confidence = (
            f"{ticket.confidence:.0%}"
            if ticket.confidence
            is not None
            else "—"
        )

        text = (
            "⚠️ <b>Review needed</b>\n"
            f"{who} · "
            f"{ticket.category} · "
            f"{confidence}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Open ticket",
                        callback_data=(
                            f"mg:ticket:{ticket.id}"
                        ),
                    )
                ]
            ]
        )

        for admin_id in (
            self.dashboard_service
            .admin_ids
        ):
            try:
                await self.dashboard_service.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            except TelegramAPIError:
                logger.warning(
                    "Could not send compact "
                    "ticket notification.",
                    exc_info=True,
                )

    async def _create_ticket(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
    ):
        if self.control_repository is None:
            return None

        current = (
            context.current_message
        )

        ticket = (
            await self.control_repository
            .create_or_update_ticket(
                chat_id=current.chat_id,
                telegram_message_id=(
                    current.telegram_message_id
                ),
                target_user_id=(
                    current.user_id
                ),
                username=current.username,
                category=(
                    decision.category
                ),
                severity=(
                    decision.severity
                ),
                confidence=(
                    decision.confidence
                ),
                reason=(
                    decision.reason
                ),
                message_text=(
                    current.raw_text
                ),
                context={
                    "policy_reason": (
                        policy.policy_reason
                    ),
                    "current_message_evidence": (
                        decision
                        .current_message_evidence
                    ),
                    "context_evidence": (
                        decision
                        .context_evidence
                    ),
                    "recent_user_messages": [
                        item.raw_text
                        for item in context.recent_user_messages[-5:]
                    ],
                    "recent_chat_messages": [
                        {
                            "user_id": item.user_id,
                            "username": item.username,
                            "name": item.full_name,
                            "text": item.raw_text,
                        }
                        for item in context.recent_chat_messages[-10:]
                        if item.raw_text.strip()
                    ],
                },
            )
        )

        await self._notify_ticket_created(
            ticket
        )

        return ticket

    async def _remember_if_safe(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        execution_success: bool | None,
    ) -> None:
        if self.control_repository is None:
            return

        # Exact malicious-message memory belongs to the stable core.
        # A custom community rule must never poison core memory and survive
        # after that custom rule is removed or rolled back.
        if policy.source == "community_policy":
            return

        if execution_success is not True:
            return

        if (
            not decision.current_message_violation
            or decision.confidence
            < 0.97
            or decision.category
            not in MEMORY_CATEGORIES
        ):
            return

        await (
            self.control_repository
            .remember_confirmed_pattern(
                chat_id=(
                    context
                    .current_message
                    .chat_id
                ),
                normalized_text=(
                    context
                    .current_message
                    .normalized_text
                ),
                category=(
                    decision.category
                ),
                confidence=(
                    decision.confidence
                ),
                decision_json=(
                    decision
                    .model_dump_json()
                ),
            )
        )

        logger.info(
            "KNOWN PATTERN LEARNED | "
            "chat=%s | category=%s",
            (
                context
                .current_message
                .chat_id
            ),
            decision.category,
        )

    async def record_manual_ticket_resolution(
        self,
        *,
        ticket,
        action: str,
        moderator_admin_id: int | None,
    ):
        """
        Bridge a REAL human Ticket resolution into effective moderation history.

        Progressive LIGHT/MEDIUM ladders read AuditRepository history. Without
        this bridge, a moderator-confirmed Warn/Mute/Ban would teach Feedback
        Memory but would not count as a confirmed prior offense on the next
        message. TEST-* tickets return before this method is called.
        """

        if action not in {
            "allow",
            "warn",
            "delete",
            "mute",
            "ban",
        }:
            return None

        event = await self.audit_repository.create_event(
            chat_id=ticket.chat_id,
            telegram_message_id=ticket.telegram_message_id,
            target_user_id=ticket.target_user_id,
            action=action,
            category=ticket.category,
            severity=ticket.severity,
            confidence=ticket.confidence,
            reason=(
                "Human moderator confirmed Ticket "
                f"{ticket.ticket_key}: {ticket.reason}"
            )[:800],
            autonomous=False,
            reversible=(
                is_reversible(action)
                if action in {
                    "allow",
                    "warn",
                    "delete",
                    "mute",
                    "ban",
                }
                else False
            ),
            metadata={
                "source": "ticket",
                "ticket_id": ticket.id,
                "moderator_admin_id": moderator_admin_id,
                "dry_run": False,
                "human_confirmed": True,
            },
        )

        logger.info(
            "MANUAL MODERATION HISTORY | chat=%s | user=%s | "
            "ticket=%s | action=%s | event=%s",
            ticket.chat_id,
            ticket.target_user_id,
            ticket.ticket_key,
            action,
            event.event_key,
        )

        if self.dashboard_service is not None:
            self.dashboard_service.request_refresh(
                ticket.chat_id
            )

        return event

    async def execute(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
    ) -> ExecutionResult:
        current = context.current_message

        shadow_mode = await self._shadow_mode(
            context
        )

        live_ban_enabled = await self._live_ban_enabled(current.chat_id)
        mute_duration_minutes = await self._mute_duration_minutes(current.chat_id)

        effective_dry_run = (
            self.dry_run
            or shadow_mode
        )

        # Pilot auto-stop: before the next abnormal autonomous destructive
        # action, force this community into persistent SHADOW. Raid Guard is a
        # separate explicit subsystem and does not pass through this per-message
        # circuit.
        if (
            not effective_dry_run
            and self.safety_circuit is not None
            and policy.autonomous
            and policy.final_action in {"delete", "mute", "ban"}
        ):
            allowed = await self.safety_circuit.preflight(
                chat_id=current.chat_id,
                planned_action=policy.final_action,
            )
            if not allowed:
                shadow_mode = True
                effective_dry_run = True

        ban_attempted = False
        ban_success = None
        ban_error = None

        mute_attempted = False
        mute_success = None
        mute_error = None

        delete_attempted = False
        delete_success = None
        delete_error = None

        warning_attempted = False
        warning_success = None
        warning_error = None

        if (
            not effective_dry_run
            and policy.final_action == "mute"
            and self.bot is not None
            and current.user_id is not None
        ):
            mute_attempted = True
            mute_success, mute_error = await self._mute_user(
                chat_id=current.chat_id, user_id=current.user_id,
                minutes=mute_duration_minutes,
            )
            if mute_success:
                logger.info(
                    "LIVE MUTE | RESTRICTED | chat=%s | user=%s | message=%s | minutes=%s | category=%s",
                    current.chat_id, current.user_id, current.telegram_message_id,
                    mute_duration_minutes, decision.category,
                )

        # AUTO-BAN is a per-chat kill switch and defaults OFF.
        # Human-reviewed ticket actions are handled elsewhere and are not
        # controlled by this autonomous switch.
        if (
            not effective_dry_run
            and policy.final_action == "ban"
            and live_ban_enabled
            and self.bot is not None
            and current.user_id is not None
        ):
            ban_attempted = True
            ban_success, ban_error = await self._ban_user(
                chat_id=current.chat_id,
                user_id=current.user_id,
            )

            if ban_success:
                logger.info(
                    "LIVE BAN | BANNED | chat=%s | user=%s | "
                    "message=%s | category=%s",
                    current.chat_id,
                    current.user_id,
                    current.telegram_message_id,
                    decision.category,
                )

                if self.control_repository is not None:
                    await self.control_repository.record_moderation_ban(
                        chat_id=current.chat_id,
                        user_id=current.user_id,
                        username=current.username,
                        source="autonomous",
                        category=decision.category,
                        reason=decision.reason,
                    )

        # When Auto-ban is OFF, a HEAVY BAN verdict still needs containment.
        # Use the configured temporary mute as the safe fallback, then delete
        # the trigger message below. The autonomous permanent ban kill switch
        # remains OFF.
        if (
            not effective_dry_run
            and policy.final_action == "ban"
            and not live_ban_enabled
            and self.bot is not None
            and current.user_id is not None
        ):
            mute_attempted = True
            mute_success, mute_error = await self._mute_user(
                chat_id=current.chat_id,
                user_id=current.user_id,
                minutes=mute_duration_minutes,
            )
            if mute_success:
                logger.info(
                    "LIVE BAN FALLBACK | MUTED | chat=%s | user=%s | "
                    "message=%s | minutes=%s | category=%s | reason=auto_ban_off",
                    current.chat_id,
                    current.user_id,
                    current.telegram_message_id,
                    mute_duration_minutes,
                    decision.category,
                )

        # Message deletion is independent from auto-ban. A HEAVY BAN verdict
        # therefore becomes MUTE + DELETE when Auto-ban is OFF.
        if self._can_live_delete(
            context=context,
            decision=decision,
            policy=policy,
            shadow_mode=shadow_mode,
        ):
            delete_attempted = True
            delete_success, delete_error = await self._delete_message(
                chat_id=current.chat_id,
                message_id=current.telegram_message_id,
            )

            if delete_success:
                logger.info(
                    "LIVE CLEANUP | DELETED | chat=%s | user=%s | "
                    "message=%s | category=%s | recommended=%s",
                    current.chat_id,
                    current.user_id,
                    current.telegram_message_id,
                    decision.category,
                    policy.final_action,
                )

        # WARN is now a real Telegram action instead of audit-only.
        if (
            not effective_dry_run
            and policy.final_action == "warn"
        ):
            warning_attempted = True
            (
                warning_success,
                warning_error,
            ) = await self._send_user_warning(
                context=context,
                reason=(
                    "flood" if decision.category == "flood"
                    else "spam" if decision.category == "spam"
                    else "harassment" if decision.category == "harassment"
                    else "generic"
                ),
            )

            if warning_success:
                logger.info(
                    "LIVE WARNING | SENT | chat=%s | user=%s | "
                    "message=%s | category=%s",
                    current.chat_id,
                    current.user_id,
                    current.telegram_message_id,
                    decision.category,
                )


        if policy.final_action == "escalate":
            await self._create_ticket(
                context=context,
                decision=decision,
                policy=policy,
            )

        destructive_success = bool(
            ban_success is True or mute_success is True or delete_success is True
        )

        await self._remember_if_safe(
            context=context,
            decision=decision,
            policy=policy,
            execution_success=destructive_success,
        )

        if ban_success is True:
            audit_action = "ban"
            effective_action = "ban"
            overall_success = True
            overall_error = delete_error
        elif mute_success is True:
            audit_action = "mute"
            effective_action = "mute"
            overall_success = True
            overall_error = delete_error
        elif delete_success is True:
            audit_action = "delete"
            effective_action = "delete"
            overall_success = True
            overall_error = ban_error
        elif warning_success is True:
            audit_action = "warn"
            effective_action = "warn"
            overall_success = True
            overall_error = None
        elif ban_attempted:
            audit_action = "ban_failed"
            effective_action = "ban"
            overall_success = False
            overall_error = ban_error
        elif mute_attempted:
            audit_action = "mute_failed"
            effective_action = "mute"
            overall_success = False
            overall_error = mute_error
        elif delete_attempted:
            audit_action = "delete_failed"
            effective_action = "delete"
            overall_success = False
            overall_error = delete_error
        elif warning_attempted:
            audit_action = "warn_failed"
            effective_action = "warn"
            overall_success = False
            overall_error = warning_error
        else:
            audit_action = policy.final_action
            effective_action = None
            overall_success = None
            overall_error = None

        destructive_failure = any(
            attempted and success is False
            for attempted, success in (
                (ban_attempted, ban_success),
                (mute_attempted, mute_success),
                (delete_attempted, delete_success),
            )
        )

        if (
            not effective_dry_run
            and self.safety_circuit is not None
            and policy.autonomous
        ):
            try:
                await self.safety_circuit.record_execution(
                    chat_id=current.chat_id,
                    action=effective_action,
                    success=overall_success,
                    had_failure=destructive_failure,
                )
            except Exception:
                # The safety layer must never crash the stable moderation path.
                logger.exception(
                    "Safety circuit outcome recording failed | chat=%s",
                    current.chat_id,
                )

        event = await self.audit_repository.create_event(
            chat_id=current.chat_id,
            telegram_message_id=current.telegram_message_id,
            target_user_id=current.user_id,
            action=audit_action,
            category=decision.category,
            severity=decision.severity,
            confidence=decision.confidence,
            reason=decision.reason,
            autonomous=policy.autonomous,
            reversible=(
                is_reversible(audit_action)
                if audit_action
                in {
                    "allow",
                    "warn",
                    "delete",
                    "mute",
                    "ban",
                    "escalate",
                }
                else False
            ),
            metadata={
                "dry_run": effective_dry_run,
                "global_dry_run": self.dry_run,
                "shadow_mode": shadow_mode,
                "live_ban_enabled": live_ban_enabled,
                "recommended_action": policy.final_action,
                "effective_action": effective_action,
                "execution_success": overall_success,
                "execution_error": overall_error,
                "ban_attempted": ban_attempted,
                "ban_execution_success": ban_success,
                "ban_execution_error": ban_error,
                "mute_duration_minutes": mute_duration_minutes,
                "mute_attempted": mute_attempted,
                "mute_execution_success": mute_success,
                "mute_execution_error": mute_error,
                "delete_attempted": delete_attempted,
                "delete_execution_success": delete_success,
                "delete_execution_error": delete_error,
                "warning_attempted": warning_attempted,
                "warning_execution_success": warning_success,
                "warning_execution_error": warning_error,
                "current_message_violation": (
                    decision.current_message_violation
                ),
                "policy_reason": policy.policy_reason,
                "policy_source": policy.source,
                "community_policy_version": (
                    policy.community_policy_version
                ),
                "matched_community_rules": (
                    policy.matched_community_rules
                ),
                "current_message_evidence": (
                    decision.current_message_evidence
                ),
                "context_evidence": decision.context_evidence,
                "report_target": decision.report_target,
            },
        )

        notified = False

        # Shadow mode is explicitly an observation product, not silent dry-run.
        # It always tells the moderator what enforcement WOULD have happened,
        # with the existing throttle preventing floods.
        if (
            shadow_mode
            and policy.final_action != "allow"
        ):
            notified = await self._notify_shadow_action(
                context=context,
                decision=decision,
                policy=policy,
                live_ban_enabled=live_ban_enabled,
            )

        elif (
            self.notify_autonomous_actions
            and (
                policy.final_action != "allow"
                or effective_action is not None
            )
        ):
            alert_key = (
                f"{current.chat_id}:"
                f"{current.user_id}:"
                f"{decision.category}"
            )

            should_notify, suppressed = await self.alert_throttle.reserve(
                alert_key
            )

            if should_notify:
                notified = await self.notifier.notify_decision(
                    context=context,
                    decision=decision,
                    policy=policy,
                    event_key=event.event_key,
                    dry_run=effective_dry_run,
                    effective_action=effective_action,
                    execution_success=overall_success,
                    execution_error=overall_error,
                    suppressed_since_previous=suppressed,
                )

        if self.dashboard_service is not None:
            self.dashboard_service.request_refresh(
                current.chat_id
            )

        if effective_dry_run:
            logger.info(
                "%s | WOULD %s | user=%s | message=%s | "
                "category=%s | confidence=%.2f | event=%s",
                (
                    "SHADOW"
                    if shadow_mode
                    else "DRY RUN"
                ),
                get_action_name(policy.final_action),
                current.user_id,
                current.telegram_message_id,
                decision.category,
                decision.confidence,
                event.event_key,
            )

            return ExecutionResult(
                final_action=policy.final_action,
                dry_run=True,
                executed=False,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=(
                    "Shadow mode: action blocked and reported."
                    if shadow_mode
                    else "Dry run."
                ),
            )

        if ban_success is True:
            details = "User banned."
            if delete_success is True:
                details = "User banned and message deleted."
            elif delete_attempted and delete_success is False:
                details = (
                    "User banned, but message deletion failed: "
                    f"{delete_error}"
                )

            return ExecutionResult(
                final_action="ban",
                dry_run=False,
                executed=True,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=details,
            )

        if mute_success is True:
            details = f"User muted for {mute_duration_minutes} minutes."
            if delete_success is True:
                details += " Message deleted."
            elif delete_attempted and delete_success is False:
                details += f" Message deletion failed: {delete_error}"
            return ExecutionResult(
                final_action="mute", dry_run=False, executed=True,
                audit_event_key=event.event_key, admin_notified=notified,
                details=details,
            )

        if delete_success is True:
            return ExecutionResult(
                final_action="delete",
                dry_run=False,
                executed=True,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=(
                    "Message deleted; Auto-ban is OFF, so temporary mute was used when possible."
                    if policy.final_action == "ban"
                    and not live_ban_enabled
                    else "Message deleted."
                ),
            )

        if (
            policy.final_action == "warn"
            and warning_success is True
        ):
            return ExecutionResult(
                final_action="warn",
                dry_run=False,
                executed=True,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details="User warning sent.",
            )

        if (
            policy.final_action == "warn"
            and warning_attempted
            and warning_success is False
        ):
            return ExecutionResult(
                final_action="warn",
                dry_run=False,
                executed=False,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=(
                    "Warning failed: "
                    f"{warning_error}"
                ),
            )

        if mute_attempted and mute_success is False and delete_success is not True:
            return ExecutionResult(
                final_action="mute", dry_run=False, executed=False,
                audit_event_key=event.event_key, admin_notified=notified,
                details=f"Mute failed: {mute_error}",
            )

        if ban_attempted and ban_success is False:
            return ExecutionResult(
                final_action="ban",
                dry_run=False,
                executed=False,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=f"Ban failed: {ban_error}",
            )

        if delete_attempted and delete_success is False:
            return ExecutionResult(
                final_action="delete",
                dry_run=False,
                executed=False,
                audit_event_key=event.event_key,
                admin_notified=notified,
                details=f"Message deletion failed: {delete_error}",
            )

        return ExecutionResult(
            final_action=policy.final_action,
            dry_run=False,
            executed=False,
            audit_event_key=event.event_key,
            admin_notified=notified,
            details=(
                "No live executor for this policy in current stage."
            ),
        )
