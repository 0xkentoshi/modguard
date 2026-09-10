import asyncio
import html
import json
import logging

__test__ = False

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)

from app.admin.dashboard import DashboardService


logger = logging.getLogger(__name__)


async def _optional_repo_value(repository, method_name: str, *args, default):
    method = getattr(repository, method_name, None)
    if method is None:
        return default
    try:
        return await method(*args)
    except AttributeError:
        return default


def _duration_label(minutes: int) -> str:
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    if minutes == 1440:
        return "1 day"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


REPORT_TARGET_TEXT = (
    "There is a private opportunity with strong returns. "
    "I will share the details only in DMs."
)

REPORT_REPLY_TEXT = (
    "Mods, this looks suspicious. "
    "Please review the message above."
)


def is_test_ticket(ticket) -> bool:
    if ticket is None:
        return False

    if not str(
        getattr(ticket, "ticket_key", "")
    ).startswith("TEST-"):
        return False

    try:
        payload = json.loads(
            getattr(ticket, "context_json", "{}") or "{}"
        )
    except Exception:
        return False

    return bool(payload.get("test_mode"))


async def _register_artifact(
    *,
    dashboard_service: DashboardService,
    managed_chat_id: int,
    telegram_chat_id: int,
    telegram_message_id: int,
    kind: str,
) -> None:
    try:
        await dashboard_service.repository.register_test_artifact(
            managed_chat_id=managed_chat_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            kind=kind,
        )
    except Exception:
        logger.exception(
            "Could not register Test Mode artifact | chat=%s | tg_chat=%s | message=%s",
            managed_chat_id,
            telegram_chat_id,
            telegram_message_id,
        )


async def build_test_mode_payload(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
    status: str | None = None,
):
    settings = await dashboard_service.repository.get_chat_settings(
        chat_id
    )
    live_ban_enabled = await dashboard_service.repository.get_live_ban_enabled(chat_id)
    raid_guard_enabled = await _optional_repo_value(
        dashboard_service.repository, "get_raid_guard_enabled", chat_id, default=False
    )
    mute_duration_minutes = await _optional_repo_value(
        dashboard_service.repository, "get_mute_duration_minutes", chat_id, default=60
    )
    title = html.escape(
        await dashboard_service._chat_title(chat_id)
    )

    shadow = "ON" if settings.shadow_mode else "OFF"
    autoban = "ON" if live_ban_enabled else "OFF"
    raid = "ON" if raid_guard_enabled else "OFF"
    mute_duration = _duration_label(mute_duration_minutes)

    live = (
        "ON"
        if (
            not dashboard_service.global_dry_run
            and dashboard_service.live_delete_enabled
        )
        else "OFF"
    )

    text = (
        "<b>🧪 TEST MODE</b>\n"
        f"{title}\n\n"
        f"Shadow        <b>{shadow}</b>\n"
        f"Live cleanup  <b>{live}</b>\n"
        f"Auto-ban      <b>{autoban}</b>\n"
        f"Mute duration <b>{html.escape(mute_duration)}</b>\n"
        f"Raid Guard    <b>{raid}</b>\n\n"
        "Critical functions have safe simulations. Test Mute never restricts "
        "a real member. Test Spam Ladder, Test Policy Tiers, and Test Raid show the policy path "
        "without needing extra accounts. Test Report keeps the visible "
        "message → reporter reply → Ticket flow."
    )

    if status:
        text += (
            "\n\n<b>Last test</b>\n"
            + status
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🛡 Shadow: {shadow}",
                    callback_data=f"mg:test_shadow:{chat_id}",
                ),
                InlineKeyboardButton(
                    text=f"🚫 Auto-ban: {autoban}",
                    callback_data=f"mg:test_ban_toggle:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Test Delete",
                    callback_data=f"mg:test_delete:{chat_id}",
                ),
                InlineKeyboardButton(
                    text="🚫 Test Ban+Delete",
                    callback_data=f"mg:test_ban:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="🔇 Test Mute", callback_data=f"mg:test_mute:{chat_id}"),
                InlineKeyboardButton(text="🔁 Test Spam Ladder", callback_data=f"mg:test_spam:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text="⚖️ Test Policy Tiers", callback_data=f"mg:test_tiers:{chat_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="📣 Test Report",
                    callback_data=f"mg:test_report:{chat_id}",
                ),
                InlineKeyboardButton(
                    text="🚨 Test Raid",
                    callback_data=f"mg:test_raid:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Clear tests",
                    callback_data=f"mg:test_clear:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀ Dashboard",
                    callback_data=f"mg:dash:{chat_id}",
                ),
            ],
        ]
    )

    return text, keyboard


async def send_test_ticket_alert(
    *,
    dashboard_service: DashboardService,
    ticket,
) -> None:
    text = (
        "🧪 <b>TEST · Review needed</b>\n"
        f"{ticket.username} · {ticket.category} · "
        f"{ticket.confidence:.0%}\n\n"
        "Simulation: report → ambiguous re-review → Ticket."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open ticket",
                    callback_data=f"mg:ticket:{ticket.id}",
                )
            ]
        ]
    )

    for admin_id in dashboard_service.admin_ids:
        try:
            sent = await dashboard_service.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            await _register_artifact(
                dashboard_service=dashboard_service,
                managed_chat_id=ticket.chat_id,
                telegram_chat_id=admin_id,
                telegram_message_id=sent.message_id,
                kind="test_ticket_alert",
            )
        except TelegramAPIError:
            logger.warning(
                "Could not send test-ticket alert.",
                exc_info=True,
            )


async def notify_test_shadow_action(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
    action: str,
    details: str,
) -> None:
    title = html.escape(
        await dashboard_service._chat_title(chat_id)
    )

    text = (
        f"🛡 <b>SHADOW · WOULD {html.escape(action)}</b>\n"
        f"{title}\n\n"
        f"{details}\n\n"
        "The real action was blocked by Shadow mode."
    )

    for admin_id in dashboard_service.admin_ids:
        try:
            sent = await dashboard_service.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
            )
            await _register_artifact(
                dashboard_service=dashboard_service,
                managed_chat_id=chat_id,
                telegram_chat_id=admin_id,
                telegram_message_id=sent.message_id,
                kind="shadow_test_alert",
            )
        except TelegramAPIError:
            logger.warning(
                "Could not send test shadow notification.",
                exc_info=True,
            )


async def run_delete_probe(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
) -> tuple[bool, str]:
    """
    Test the delete path using ModGuard's own message.

    LIVE: send -> wait -> actually delete.
    SHADOW: send -> DO NOT delete -> tell moderators WOULD DELETE.
    """
    bot = dashboard_service.bot
    settings = await dashboard_service.repository.get_chat_settings(
        chat_id
    )

    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=(
                "🧪 TEST DELETE\n"
                "Safe ModGuard test message."
            ),
        )

        await _register_artifact(
            dashboard_service=dashboard_service,
            managed_chat_id=chat_id,
            telegram_chat_id=chat_id,
            telegram_message_id=message.message_id,
            kind="delete_probe",
        )

        if settings.shadow_mode:
            await notify_test_shadow_action(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                action="DELETE",
                details=(
                    "Test message #"
                    + str(message.message_id)
                    + " would be deleted. It was intentionally left visible."
                ),
            )

            return (
                True,
                "🛡 SHADOW: WOULD DELETE. "
                "The test message was left visible and moderators were notified.",
            )

        await asyncio.sleep(1.0)

        await bot.delete_message(
            chat_id=chat_id,
            message_id=message.message_id,
        )

        return (
            True,
            "✅ LIVE DELETE: OK. The ModGuard test message was actually deleted.",
        )

    except Exception as exc:
        logger.exception("Test delete probe failed")
        return (
            False,
            "❌ Delete API failed: "
            + html.escape(str(exc)[:180]),
        )


async def run_ban_permission_probe(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
) -> tuple[bool, str]:
    """
    Safe BAN+DELETE path test.

    No real Telegram user is banned. The probe checks the live switch and
    permission, and Shadow mode produces a WOULD BAN+DELETE notification.
    """
    bot = dashboard_service.bot
    settings = await dashboard_service.repository.get_chat_settings(
        chat_id
    )
    live_ban_enabled = await dashboard_service.repository.get_live_ban_enabled(
        chat_id
    )

    if not live_ban_enabled:
        return (
            True,
            "🚫 Auto-ban OFF. In LIVE mode, a BAN verdict will not permanently ban the user; "
            "ModGuard falls back to the configured temporary mute + message deletion.",
        )

    if settings.shadow_mode:
        await notify_test_shadow_action(
            dashboard_service=dashboard_service,
            chat_id=chat_id,
            action="BAN + DELETE",
            details=(
                "Synthetic high-confidence scam case. "
                "Auto-ban is ON, but Shadow blocked both actions."
            ),
        )

        return (
            True,
            "🛡 SHADOW: WOULD BAN + DELETE. No user was banned and nothing was deleted.",
        )

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=me.id,
        )

        status = getattr(member, "status", "")
        status_value = getattr(
            status,
            "value",
            str(status),
        )

        can_restrict = bool(
            getattr(
                member,
                "can_restrict_members",
                False,
            )
        )

        allowed = (
            can_restrict
            or status_value in {"creator", "owner"}
        )

        if allowed:
            return (
                True,
                "✅ LIVE BAN path ARMED: Auto-ban ON + Telegram ban permission OK. "
                "Safe test does not ban a real account; a real high-confidence "
                "BAN verdict will ban and delete.",
            )

        return (
            False,
            "❌ Auto-ban is ON, but ModGuard does not have permission to restrict members.",
        )

    except Exception as exc:
        logger.exception("Test ban probe failed")
        return (
            False,
            "❌ Ban capability check failed: "
            + html.escape(str(exc)[:180]),
        )


async def run_mute_permission_probe(*, dashboard_service: DashboardService, chat_id: int) -> tuple[bool, str]:
    duration = await _optional_repo_value(
        dashboard_service.repository, "get_mute_duration_minutes", chat_id, default=60
    )
    settings = await dashboard_service.repository.get_chat_settings(chat_id)
    if settings.shadow_mode:
        await notify_test_shadow_action(
            dashboard_service=dashboard_service,
            chat_id=chat_id,
            action="MUTE + DELETE",
            details=(
                "Synthetic repeat-spam case. "
                f"Configured mute: {_duration_label(duration)}. "
                "No real member was restricted."
            ),
        )
        return True, (
            "🛡 SHADOW: WOULD MUTE + DELETE. "
            f"Configured duration: {_duration_label(duration)}."
        )

    try:
        me = await dashboard_service.bot.get_me()
        member = await dashboard_service.bot.get_chat_member(
            chat_id=chat_id, user_id=me.id
        )
        status = getattr(member, "status", "")
        status_value = getattr(status, "value", str(status))
        can_restrict = bool(getattr(member, "can_restrict_members", False))
        allowed = can_restrict or status_value in {"creator", "owner"}
        if allowed:
            return True, (
                "✅ MUTE path ARMED. "
                f"Configured duration: {_duration_label(duration)}. "
                "Safe test did not restrict a real member."
            )
        return False, (
            "❌ ModGuard does not have permission to restrict members. "
            "Mute cannot work until that admin permission is granted."
        )
    except Exception as exc:
        logger.exception("Test mute probe failed")
        return False, "❌ Mute capability check failed: " + html.escape(str(exc)[:180])


async def spam_ladder_simulation_status(*, dashboard_service: DashboardService, chat_id: int) -> str:
    duration = await _optional_repo_value(
        dashboard_service.repository, "get_mute_duration_minutes", chat_id, default=60
    )
    live_ban_enabled = await dashboard_service.repository.get_live_ban_enabled(chat_id)
    ban_line = (
        "BAN + DELETE (Auto-ban ON)"
        if live_ban_enabled
        else "WOULD MUTE + DELETE (Auto-ban OFF; Auto-ban ON → BAN + DELETE)"
    )
    return (
        "🔁 <b>SPAM LADDER · SIMULATION</b>\n"
        "1st clear spam → <b>WARN</b> (message stays)\n"
        "Repeat → <b>MUTE " + html.escape(_duration_label(duration)) + " + DELETE</b>\n"
        "Spam again after confirmed mute → <b>" + ban_line + "</b>\n\n"
        "No reputation/audit state was changed."
    )


async def enforcement_tiers_simulation_status(*, dashboard_service: DashboardService, chat_id: int) -> str:
    duration = await _optional_repo_value(
        dashboard_service.repository, "get_mute_duration_minutes", chat_id, default=60
    )
    live_ban_enabled = await dashboard_service.repository.get_live_ban_enabled(chat_id)
    ban = (
        "BAN + DELETE (Auto-ban ON)"
        if live_ban_enabled
        else "WOULD MUTE + DELETE (Auto-ban OFF; Auto-ban ON → BAN + DELETE)"
    )
    return (
        "⚖️ <b>ENFORCEMENT TIERS · SIMULATION</b>\n"
        "LIGHT · spam/flood\n"
        "→ <b>WARN</b> → repeat <b>MUTE " + html.escape(_duration_label(duration)) + " + DELETE</b> "
        "→ repeat after mute <b>" + ban + "</b>\n\n"
        "HUMAN CONFLICT · conservative Core\n"
        "→ first clear targeted harassment <b>WARN</b> (message stays)\n"
        "→ repeat / mutual fight / unclear instigator <b>TICKET</b>\n"
        "→ no default autonomous mute/ban for ordinary fights\n\n"
        "MEDIUM · clear intrusive DM/promo solicitation\n"
        "→ <b>MUTE " + html.escape(_duration_label(duration)) + " + DELETE</b> "
        "→ repeat after mute <b>" + ban + "</b>\n\n"
        "HEAVY · scam/phishing/malicious link/credible threat\n"
        "→ <b>" + ban + "</b> immediately when confidence is high.\n\n"
        "Protected Core cannot be relaxed by Community Policy; configurable community behavior can."
    )


async def run_report_simulation(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
):
    """
    Produce a visible report-flow simulation in the selected community:

    bot posts a synthetic member message
      -> bot posts a synthetic reporter reply to that message
      -> a TEST-* human-review ticket is created
      -> admins receive the normal test ticket alert

    The Bot API cannot impersonate two real users, so both messages are sent by
    ModGuard and clearly labelled as synthetic roles.
    """
    bot = dashboard_service.bot

    target = await bot.send_message(
        chat_id=chat_id,
        text=(
            "🧪 TEST · MEMBER MESSAGE\n"
            + REPORT_TARGET_TEXT
        ),
    )
    await _register_artifact(
        dashboard_service=dashboard_service,
        managed_chat_id=chat_id,
        telegram_chat_id=chat_id,
        telegram_message_id=target.message_id,
        kind="report_target",
    )

    report = await bot.send_message(
        chat_id=chat_id,
        text=(
            "🧪 TEST · REPORTER REPLY\n"
            + REPORT_REPLY_TEXT
        ),
        reply_parameters=ReplyParameters(
            message_id=target.message_id
        ),
    )
    await _register_artifact(
        dashboard_service=dashboard_service,
        managed_chat_id=chat_id,
        telegram_chat_id=chat_id,
        telegram_message_id=report.message_id,
        kind="report_reply",
    )

    ticket = await dashboard_service.repository.create_test_ticket(
        chat_id=chat_id,
        source="report",
        category="scam",
        confidence=0.68,
        message_text=REPORT_TARGET_TEXT,
        reason=(
            "TEST REPORT: a member reported the original message. "
            "Deep re-review kept the case ambiguous and sent it to a moderator."
        ),
        telegram_message_id=target.message_id,
    )

    await send_test_ticket_alert(
        dashboard_service=dashboard_service,
        ticket=ticket,
    )

    return ticket, (
        "📣 Report simulation created in community: "
        "member message → reply complaint → Ticket "
        f"{ticket.ticket_key}."
    )


async def clear_all_test_artifacts(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
) -> str:
    """
    Delete all Telegram messages registered by Test Mode and purge TEST-* tickets.

    The dashboard itself is not a test artifact and remains intact.
    """
    artifacts = await dashboard_service.repository.list_test_artifacts(
        managed_chat_id=chat_id
    )

    deleted = 0
    already_gone = 0
    failed = 0

    for artifact in artifacts:
        try:
            await dashboard_service.bot.delete_message(
                chat_id=artifact.telegram_chat_id,
                message_id=artifact.telegram_message_id,
            )
            deleted += 1
        except TelegramBadRequest:
            # Typical case: LIVE Test Delete already removed its own probe.
            already_gone += 1
        except TelegramAPIError:
            failed += 1
            logger.warning(
                "Could not clear test artifact | tg_chat=%s | message=%s | kind=%s",
                artifact.telegram_chat_id,
                artifact.telegram_message_id,
                artifact.kind,
                exc_info=True,
            )
        except Exception:
            failed += 1
            logger.exception(
                "Unexpected error clearing test artifact | tg_chat=%s | message=%s",
                artifact.telegram_chat_id,
                artifact.telegram_message_id,
            )

    ticket_count = await dashboard_service.repository.clear_test_tickets(
        chat_id=chat_id
    )
    registry_count = await dashboard_service.repository.purge_test_artifacts(
        managed_chat_id=chat_id
    )

    return (
        "🧹 Test cleanup complete. "
        f"Messages removed: {deleted}; already gone: {already_gone}; "
        f"failed: {failed}; TEST tickets purged: {ticket_count}; "
        f"artifact records cleared: {registry_count}."
    )


async def raid_preview_payload(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
):
    title = html.escape(
        await dashboard_service._chat_title(chat_id)
    )

    text = (
        "<b>🚨 TEST RAID · SIMULATION</b>\n"
        f"{title}\n\n"
        "90 sec window\n"
        "New users        <b>34</b>\n"
        "Suspicious       <b>21</b>\n"
        "Similar messages <b>19</b>\n"
        "Domains           <b>2</b>\n\n"
        "UI simulation. Real accounts "
        "are not affected."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛡 Run anti-raid",
                    callback_data=f"mg:test_raid_run:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀ Test mode",
                    callback_data=f"mg:test:{chat_id}",
                ),
            ],
        ]
    )

    return text, keyboard


async def raid_result_payload(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
):
    title = html.escape(await dashboard_service._chat_title(chat_id))
    settings = await dashboard_service.repository.get_chat_settings(chat_id)
    live_ban_enabled = await dashboard_service.repository.get_live_ban_enabled(chat_id)
    raid_guard_enabled = await _optional_repo_value(
        dashboard_service.repository, "get_raid_guard_enabled", chat_id, default=False
    )
    if not raid_guard_enabled:
        enforcement = "Detection only · Raid Guard OFF"
    elif settings.shadow_mode:
        enforcement = "WOULD DELETE + " + ("WOULD BAN" if live_ban_enabled else "Auto-ban OFF")
    else:
        enforcement = "LIVE plan: DELETE + " + ("BAN" if live_ban_enabled else "Auto-ban OFF")

    text = (
        "<b>🛡 TEST ANTI-RAID · SIMULATION</b>\n"
        f"{title}\n\n"
        "Synthetic campaign <b>SC-TEST</b>\n"
        "Window              <b>90 sec</b>\n"
        "Messages            <b>12</b>\n"
        "Unique users        <b>4</b>\n"
        "Semantic similarity <b>94%</b>\n"
        "Hard-risk ratio     <b>83%</b>\n\n"
        f"Result: <b>{html.escape(enforcement)}</b>\n\n"
        "✅ Full Raid Guard decision path simulated.\n"
        "No real user or message was affected. Extra accounts are not required."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Run again",
                    callback_data=f"mg:test_raid:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀ Test mode",
                    callback_data=f"mg:test:{chat_id}",
                ),
            ],
        ]
    )

    return text, keyboard


async def _delete_test_ticket_target(
    *,
    dashboard_service: DashboardService,
    ticket,
) -> tuple[bool, str]:
    """
    Delete the synthetic Test Report target from the selected community.

    This is safe because TEST-* tickets never point at a real participant.
    """
    if ticket.telegram_message_id is None:
        return (
            False,
            "❌ This test ticket has no linked community message.",
        )

    try:
        await dashboard_service.bot.delete_message(
            chat_id=ticket.chat_id,
            message_id=ticket.telegram_message_id,
        )

        return (
            True,
            "🗑 The original TEST message was actually deleted from the community.",
        )

    except TelegramBadRequest as exc:
        message = str(exc).lower()

        if (
            "message to delete not found" in message
            or "message can't be deleted" not in message
            and "not found" in message
        ):
            return (
                True,
                "🗑 The TEST message is already gone from the community.",
            )

        logger.warning(
            "Could not delete Test Report target | chat=%s | message=%s",
            ticket.chat_id,
            ticket.telegram_message_id,
            exc_info=True,
        )

        return (
            False,
            "❌ Telegram did not allow the TEST message to be deleted: "
            + html.escape(str(exc)[:180]),
        )

    except Exception as exc:
        logger.exception(
            "Test ticket target delete failed | chat=%s | message=%s",
            ticket.chat_id,
            ticket.telegram_message_id,
        )

        return (
            False,
            "❌ Error deleting TEST message: "
            + html.escape(str(exc)[:180]),
        )


async def execute_test_ticket_action(
    *,
    dashboard_service: DashboardService,
    ticket,
    action: str,
) -> tuple[bool, str]:
    """
    Execute a TEST-* ticket safely but visibly.

    DELETE:
      really deletes the synthetic target message.

    BAN:
      never bans a real Telegram account, because the synthetic Test Report
      has no real target user; it simulates the ban and really removes the
      offending synthetic message.

    WARN:
      really posts a synthetic warning into the community.

    MUTE:
      safely simulates the selected chat's configured mute duration.

    ALLOW:
      leaves the synthetic target visible.
    """
    if not is_test_ticket(ticket):
        return (
            False,
            "❌ This is not a TEST ticket.",
        )

    if action == "delete":
        return await _delete_test_ticket_target(
            dashboard_service=dashboard_service,
            ticket=ticket,
        )

    if action == "ban":
        deleted, delete_status = await _delete_test_ticket_target(
            dashboard_service=dashboard_service,
            ticket=ticket,
        )

        if not deleted:
            return (
                False,
                delete_status,
            )

        return (
            True,
            "🚫 TEST BAN completed safely: no real user was banned.\n"
            + delete_status
            + "\nIn a real ticket, Ban would target the real user ID.",
        )

    if action == "mute":
        duration = await _optional_repo_value(
            dashboard_service.repository, "get_mute_duration_minutes", ticket.chat_id, default=60
        )
        return True, (
            "🔇 TEST MUTE completed safely: no real user was restricted.\n"
            f"Configured duration: {_duration_label(duration)}."
        )

    if action == "warn":
        warning_text = (
            "🧪 TEST WARN\n"
            "@test_user, this is a ModGuard test warning. "
            "In a real ticket, the member would receive the warning here."
        )

        try:
            kwargs = {}

            if ticket.telegram_message_id is not None:
                kwargs["reply_parameters"] = ReplyParameters(
                    message_id=ticket.telegram_message_id
                )

            try:
                sent = await dashboard_service.bot.send_message(
                    chat_id=ticket.chat_id,
                    text=warning_text,
                    **kwargs,
                )
            except TelegramBadRequest:
                # The target could already have been removed manually.
                sent = await dashboard_service.bot.send_message(
                    chat_id=ticket.chat_id,
                    text=warning_text,
                )

            await _register_artifact(
                dashboard_service=dashboard_service,
                managed_chat_id=ticket.chat_id,
                telegram_chat_id=ticket.chat_id,
                telegram_message_id=sent.message_id,
                kind="test_ticket_warn",
            )

            return (
                True,
                "⚠️ TEST WARN was actually sent to the community.",
            )

        except Exception as exc:
            logger.exception(
                "Test ticket warn failed | chat=%s",
                ticket.chat_id,
            )

            return (
                False,
                "❌ Could not send TEST WARN: "
                + html.escape(str(exc)[:180]),
            )

    if action == "allow":
        return (
            True,
            "✅ ALLOW: the original TEST message was intentionally left in the community.",
        )

    return (
        False,
        "❌ Unsupported TEST action: "
        + html.escape(action),
    )


async def build_test_ticket_result_payload(
    *,
    dashboard_service: DashboardService,
    chat_id: int,
    action: str,
    result_details: str | None = None,
):
    title = html.escape(
        await dashboard_service._chat_title(chat_id)
    )

    label = {
        "ban": "BAN",
        "warn": "WARN",
        "mute": "MUTE",
        "delete": "DELETE",
        "allow": "ALLOW",
    }.get(
        action,
        action.upper(),
    )

    text = (
        "<b>🧪 TEST REPORT RESOLVED</b>\n"
        f"{title}\n\n"
        f"Moderator chose: <b>{label}</b>\n\n"
        "✅ Human-review flow completed end-to-end.\n"
        "No real Telegram user was affected."
    )

    if result_details:
        text += (
            "\n\n<b>Action result</b>\n"
            + result_details
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📣 Run another report",
                    callback_data=f"mg:test_report:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀ Test mode",
                    callback_data=f"mg:test:{chat_id}",
                ),
            ],
        ]
    )

    return text, keyboard
