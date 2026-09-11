import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.agent.schemas import (
    MessageContext,
    ModerationDecision,
    PolicyEvaluation,
)
from app.moderation.actions import get_action_name


logger = logging.getLogger(__name__)


def escape(
    value: object,
) -> str:
    return html.escape(
        str(value)
    )


def shorten(
    value: str,
    limit: int = 700,
) -> str:
    if len(value) <= limit:
        return value

    return value[:limit] + "..."


class AdminNotifier:
    def __init__(
        self,
        *,
        bot: Bot,
        admin_ids: list[int],
        recipient_provider=None,
    ):
        self.bot = bot
        self.admin_ids = admin_ids
        self.recipient_provider = recipient_provider

    async def _recipients_for_chat(self, chat_id: int) -> set[int]:
        recipients = set(self.admin_ids)
        if self.recipient_provider is not None:
            try:
                dynamic = await self.recipient_provider.notification_recipients(chat_id)
                recipients.update(int(item) for item in dynamic)
            except Exception:
                logger.exception(
                    "Could not resolve dynamic admin recipients | chat=%s",
                    chat_id,
                )
        return recipients

    async def notify_system_alert(
        self,
        *,
        chat_id: int,
        title: str,
        body: str,
    ) -> bool:
        """Compact operational/safety alert for owners and platform superadmins."""
        recipients = await self._recipients_for_chat(chat_id)
        if not recipients:
            return False

        text = (
            f"<b>{escape(title)}</b>\n\n"
            f"{escape(body)}\n\n"
            f"Chat: <code>{escape(chat_id)}</code>"
        )
        sent = False
        for admin_id in sorted(recipients):
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                )
                sent = True
            except TelegramAPIError:
                logger.exception(
                    "Could not send system alert to admin %s",
                    admin_id,
                )
        return sent

    async def notify_decision(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        event_key: str,
        dry_run: bool,
        effective_action: str | None = None,
        execution_success: bool | None = None,
        execution_error: str | None = None,
        suppressed_since_previous: int = 0,
    ) -> bool:
        if (
            policy.final_action == "allow"
            and effective_action is None
        ):
            return False

        current = context.current_message

        recipients = await self._recipients_for_chat(current.chat_id)

        if not recipients:
            return False

        displayed_action = (
            effective_action
            or policy.final_action
        )

        action_name = get_action_name(
            displayed_action
        )

        if dry_run:
            mode = "DRY RUN"

        elif (
            effective_action == "delete"
        ):
            mode = "LIVE CLEANUP"

        else:
            mode = "LIVE"

        current_evidence = (
            "\n".join(
                f"• {escape(item)}"
                for item
                in decision.current_message_evidence
            )
            or "—"
        )

        context_evidence = (
            "\n".join(
                f"• {escape(item)}"
                for item
                in decision.context_evidence
            )
            or "—"
        )

        username = (
            current.username
            or current.full_name
            or current.user_id
            or "unknown"
        )

        execution_block = ""

        if not dry_run:
            if execution_success is True:
                execution_block = (
                    "\n<b>Execution:</b> "
                    "message deleted from chat\n"
                )

            elif execution_success is False:
                execution_block = (
                    "\n<b>Execution:</b> FAILED\n"
                    f"<b>Error:</b> "
                    f"{escape(execution_error or 'unknown')}\n"
                )

        recommendation_block = ""

        if (
            effective_action is not None
            and effective_action
            != policy.final_action
        ):
            recommendation_block = (
                f"\n<b>AI/Policy recommendation:</b> "
                f"{escape(get_action_name(policy.final_action))}\n"
            )

        suppressed_block = ""

        if suppressed_since_previous > 0:
            suppressed_block = (
                f"\n<b>Previous duplicate alerts suppressed:</b> "
                f"{suppressed_since_previous}\n"
            )

        message_text = (
            f"<b>MODGUARD · {escape(mode)}</b>\n\n"

            f"<b>Action:</b> "
            f"{escape(action_name)}\n"

            f"<b>Category:</b> "
            f"{escape(decision.category)}\n"

            f"<b>Severity:</b> "
            f"{escape(decision.severity)}\n"

            f"<b>Confidence:</b> "
            f"{decision.confidence:.0%}\n"

            f"<b>Current violation:</b> "
            f"{'YES' if decision.current_message_violation else 'NO'}\n"

            f"<b>User:</b> "
            f"{escape(username)}\n"

            f"<b>User ID:</b> "
            f"{escape(current.user_id)}\n"

            f"<b>Event:</b> "
            f"{escape(event_key)}\n"

            f"{recommendation_block}"
            f"{execution_block}"
            f"{suppressed_block}\n"

            f"<b>Reason:</b>\n"
            f"{escape(decision.reason)}\n\n"

            f"<b>Current-message evidence:</b>\n"
            f"{current_evidence}\n\n"

            f"<b>Context evidence:</b>\n"
            f"{context_evidence}\n\n"

            f"<b>Message:</b>\n"
            f"<code>"
            f"{escape(shorten(current.raw_text))}"
            f"</code>\n\n"

            f"<b>Signals:</b>\n"
            f"URLs: "
            f"{context.behavior_signals.url_count}\n"

            f"Messages/60s: "
            f"{context.behavior_signals.messages_last_60s}\n"

            f"Repeated: "
            f"{context.behavior_signals.repeated_recent_messages}\n"

            f"Mixed-script tokens: "
            f"{escape(context.text_signals.mixed_script_tokens)}\n\n"

            f"<b>Policy:</b>\n"
            f"{escape(policy.policy_reason)}"
        )

        sent = False

        for admin_id in sorted(recipients):
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=message_text,
                    parse_mode="HTML",
                )

                sent = True

            except TelegramAPIError:
                logger.exception(
                    "Could not send moderation "
                    "notification to admin %s",
                    admin_id,
                )

        return sent
