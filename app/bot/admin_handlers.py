import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.admin.control_repository import ControlRepository
from app.admin.dashboard import DashboardService
from app.admin.test_mode import (
    build_test_mode_payload,
    build_test_ticket_result_payload,
    clear_all_test_artifacts,
    enforcement_tiers_simulation_status,
    execute_test_ticket_action,
    is_test_ticket,
    raid_preview_payload,
    raid_result_payload,
    run_ban_permission_probe,
    run_delete_probe,
    run_mute_permission_probe,
    run_report_simulation,
    spam_ladder_simulation_status,
)
from app.community_policy.service import CommunityPolicyService
from app.feedback.service import ModeratorFeedbackService
from app.raid_guard.service import RaidGuardService
from app.moderation.executor import ModerationExecutor
from app.config import Settings


router = Router(
    name="admin_private"
)

logger = logging.getLogger(
    __name__
)

PRIVATE_CHAT = (
    F.chat.type
    == ChatType.PRIVATE
)




async def safe_callback_answer(
    callback: CallbackQuery,
    text: str = "",
    *,
    show_alert: bool = False,
) -> bool:
    """Best-effort callback ACK; expired Telegram query IDs are harmless."""
    try:
        await callback.answer(text, show_alert=show_alert)
        return True
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if (
            "query is too old" in message
            or "query id is invalid" in message
            or "response timeout expired" in message
        ):
            logger.info(
                "CALLBACK ACK SKIPPED | reason=expired_query | data=%s",
                callback.data,
            )
            return False
        raise

async def allowed(
    user_id: int,
    settings: Settings,
    pilot_access_service=None,
) -> bool:
    if user_id in settings.admin_id_list:
        return True
    if pilot_access_service is None:
        return False
    try:
        return bool(await pilot_access_service.is_authorized(user_id))
    except Exception:
        logger.exception("Pilot authorization failed | user=%s", user_id)
        return False


async def can_access_chat(
    user_id: int,
    chat_id: int,
    pilot_access_service=None,
) -> bool:
    if pilot_access_service is None:
        return True
    try:
        return bool(await pilot_access_service.can_access_chat(user_id, chat_id))
    except Exception:
        logger.exception(
            "Pilot chat authorization failed | user=%s | chat=%s",
            user_id,
            chat_id,
        )
        return False


def shadow_feedback_case_text(
    case,
    *,
    stage: str = "pending",
    interpretation_summary: str | None = None,
    unsupported_assumptions: list[str] | None = None,
) -> str:
    who = (
        getattr(case, "username", None)
        or (
            f"user {case.target_user_id}"
            if getattr(case, "target_user_id", None) is not None
            else "unknown"
        )
    )
    confidence = (
        f"{case.ai_confidence:.0%}"
        if getattr(case, "ai_confidence", None) is not None
        else "—"
    )
    message_text = (getattr(case, "message_text", "") or "").strip()
    if len(message_text) > 320:
        message_text = message_text[:320] + "..."

    base = (
        f"🛡 <b>SHADOW FEEDBACK</b>\n"
        f"{html.escape(str(who))} · "
        f"{html.escape(str(getattr(case, 'ai_category', None) or 'other'))} · "
        f"{confidence}\n\n"
        f"<code>{html.escape(message_text or '[non-text message]')}</code>\n\n"
        f"<b>ModGuard:</b> {html.escape(str(getattr(case, 'ai_action', 'escalate')).upper())}\n"
        f"<b>Why:</b> {html.escape((getattr(case, 'ai_reason', '') or '')[:500])}"
    )

    if stage == "explain":
        return (
            base
            + "\n\n❌ <b>You disagree.</b>\n"
            + "Explain in one normal message <b>why this decision is wrong</b> "
              "and <b>what ModGuard should do instead</b>.\n\n"
            + "You can include context such as local chat culture, recurring jokes, "
              "or a known relationship between these participants. ModGuard will "
              "first show how it understood you; nothing is learned until you confirm it."
        )

    if stage == "clarify":
        previous = (getattr(case, "moderator_explanation", "") or "").strip()
        previous_block = ""
        if previous:
            previous_block = (
                "\n\n<b>Previous explanation:</b>\n"
                f"{html.escape(previous[:700])}"
            )
        return (
            base
            + previous_block
            + "\n\n✏️ <b>Send one more message with the correction/clarification.</b>\n"
              "I will reinterpret the combined feedback and show it again before saving."
        )

    if stage == "confirm":
        corrected = html.escape(
            str(getattr(case, "corrected_action", None) or "escalate").upper()
        )
        local_rule = html.escape(
            (getattr(case, "local_rule", "") or "")[:800]
        )
        relation = (getattr(case, "relationship_note", "") or "").strip()
        relation_block = ""
        if relation:
            pair_scope = (
                "same participant pair"
                if getattr(case, "apply_to_same_pair", False)
                else "community context only"
            )
            relation_block = (
                "\n\n<b>Relationship context:</b> "
                f"{html.escape(relation[:500])}\n"
                f"<b>Scope:</b> {html.escape(pair_scope)}"
            )
        unsupported_block = ""
        if unsupported_assumptions:
            unsupported_block = (
                "\n\n<b>Cannot verify automatically:</b>\n"
                + "\n".join(
                    f"• {html.escape(item[:240])}"
                    for item in unsupported_assumptions[:5]
                )
            )
        summary_block = ""
        if interpretation_summary:
            summary_block = (
                "\n\n<b>I understood:</b> "
                + html.escape(interpretation_summary[:600])
            )
        return (
            base
            + "\n\n🧠 <b>Proposed correction</b>\n"
            + f"Correct action: <b>{corrected}</b>\n"
            + f"Local rule: {local_rule}"
            + summary_block
            + relation_block
            + unsupported_block
            + "\n\nThis memory stays inside this community and remains soft guidance; "
              "Core safety still runs first."
        )

    if stage == "saved":
        action = html.escape(
            str(getattr(case, "moderator_action", "allow")).upper()
        )
        return (
            base
            + f"\n\n✅ <b>Feedback saved.</b> Correct action: <b>{action}</b>.\n"
              "It can now help with similar gray cases in this community."
        )

    return base


def shadow_feedback_keyboard(
    case_id: int,
    *,
    stage: str,
) -> InlineKeyboardMarkup:
    if stage == "pending":
        rows = [[
            InlineKeyboardButton(
                text="✅ Agree",
                callback_data=f"mg:sagree:{case_id}",
            ),
            InlineKeyboardButton(
                text="❌ Disagree",
                callback_data=f"mg:sdisagree:{case_id}",
            ),
        ]]
    elif stage == "confirm":
        rows = [
            [
                InlineKeyboardButton(
                    text="✅ Save",
                    callback_data=f"mg:sfsave:{case_id}",
                ),
                InlineKeyboardButton(
                    text="✏️ Clarify",
                    callback_data=f"mg:sfclarify:{case_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=f"mg:sfcancel:{case_id}",
                )
            ],
        ]
    else:
        rows = [[
            InlineKeyboardButton(
                text="Cancel",
                callback_data=f"mg:sfcancel:{case_id}",
            )
        ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def remove_stale_dashboard_copy(
    *,
    callback: CallbackQuery,
    repository: ControlRepository,
) -> bool:
    """
    Old dashboard messages from the pre-fix spam bug can still have live
    inline buttons. Clicking one must not make the UI look broken.

    If the clicked bot message is not the currently tracked dashboard,
    delete that stale copy best-effort and continue rendering into the
    current dashboard.
    """

    if callback.message is None:
        return False

    state = await repository.get_dashboard_state(
        callback.from_user.id
    )

    if (
        state is None
        or state.dashboard_message_id is None
    ):
        return False

    clicked_message_id = getattr(
        callback.message,
        "message_id",
        None,
    )

    if (
        clicked_message_id is None
        or clicked_message_id
        == state.dashboard_message_id
    ):
        return False

    try:
        await callback.message.delete()
    except Exception:
        logger.debug(
            "Could not delete stale dashboard copy.",
            exc_info=True,
        )

    return True


@router.message(
    CommandStart(),
    PRIVATE_CHAT,
)
async def start_private(
    message: Message,
    dashboard_service: DashboardService,
    app_settings: Settings,
    pilot_access_service=None,
) -> None:
    if message.from_user and pilot_access_service is not None:
        try:
            await pilot_access_service.record_user(
                user_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
        except Exception:
            logger.exception("Could not record Pilot user")

    if (
        not message.from_user
        or not await allowed(
            message.from_user.id,
            app_settings,
            pilot_access_service,
        )
    ):
        await message.answer(
            "ModGuard admin console."
        )
        return

    await dashboard_service.open_dashboard(
        admin_id=message.from_user.id
    )


@router.message(
    Command("dashboard"),
    PRIVATE_CHAT,
)
async def dashboard_command(
    message: Message,
    dashboard_service: DashboardService,
    app_settings: Settings,
    pilot_access_service=None,
) -> None:
    if (
        not message.from_user
        or not await allowed(
            message.from_user.id,
            app_settings,
            pilot_access_service,
        )
    ):
        return

    # Intentionally updates the ONE tracked dashboard instead of creating
    # another message. This is the anti-spam behavior.
    await dashboard_service.open_dashboard(
        admin_id=message.from_user.id
    )

    # Keep admin chat clean; best effort only.
    try:
        await message.delete()
    except Exception:
        logger.debug(
            "Could not delete /dashboard command message.",
            exc_info=True,
        )


@router.message(
    Command("myid"),
    PRIVATE_CHAT,
)
async def show_my_id(
    message: Message,
) -> None:
    if message.from_user:
        await message.answer(
            f"Your Telegram ID:\n"
            f"{message.from_user.id}"
        )


@router.callback_query(
    F.data.startswith("mg:")
)
async def admin_callback(
    callback: CallbackQuery,
    dashboard_service: DashboardService,
    control_repository: ControlRepository,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    raid_guard_service: RaidGuardService,
    moderation_executor: ModerationExecutor,
    app_settings: Settings,
    safety_circuit=None,
    pilot_access_service=None,
) -> None:
    if (
        not callback.from_user
        or not await allowed(
            callback.from_user.id,
            app_settings,
            pilot_access_service,
        )
    ):
        await callback.answer(
            "Not authorized.",
            show_alert=True,
        )
        return

    parts = (
        callback.data
        or ""
    ).split(":")

    if len(parts) < 2:
        await callback.answer()
        return

    action = parts[1]
    admin_id = callback.from_user.id

    # Most chat-scoped callbacks carry the Telegram chat id in parts[2].
    # Negative ids are Telegram group/supergroup ids; protect them centrally
    # for dynamically granted Pilot renters. Resource-id callbacks are checked
    # again when their record is loaded below.
    if pilot_access_service is not None and len(parts) > 2:
        try:
            candidate_chat_id = int(parts[2])
        except (TypeError, ValueError):
            candidate_chat_id = None
        if (
            candidate_chat_id is not None
            and candidate_chat_id < 0
            and not await can_access_chat(
                admin_id,
                candidate_chat_id,
                pilot_access_service,
            )
        ):
            await safe_callback_answer(
                callback,
                "Not authorized for this community.",
                show_alert=True,
            )
            return

    # First admin interaction after restart also repairs any Telegram
    # basic-group -> supergroup aliases left by older ModGuard versions.
    await dashboard_service.reconcile_managed_chats()

    if action in {
        "sagree",
        "sdisagree",
        "sfsave",
        "sfclarify",
        "sfcancel",
    }:
        # Shadow feedback lives in separate alert messages, not in the pinned
        # dashboard. Never treat these messages as stale dashboard copies.
        stale = False
    else:
        stale = await remove_stale_dashboard_copy(
            callback=callback,
            repository=control_repository,
        )

    toast = (
        "Stale dashboard removed"
        if stale
        else None
    )

    try:
        if action in {
            "sagree",
            "sdisagree",
            "sfsave",
            "sfclarify",
            "sfcancel",
        }:
            if len(parts) < 3:
                await safe_callback_answer(
                    callback,
                    "Feedback case is missing.",
                    show_alert=True,
                )
                return

            case_id = int(parts[2])
            case = await control_repository.get_shadow_feedback_case(
                case_id
            )
            if case is None:
                await safe_callback_answer(
                    callback,
                    "Shadow feedback case not found.",
                    show_alert=True,
                )
                return

            if not await can_access_chat(
                admin_id,
                case.chat_id,
                pilot_access_service,
            ):
                await safe_callback_answer(
                    callback,
                    "Not authorized for this community.",
                    show_alert=True,
                )
                return

            async def edit_feedback_alert(
                record,
                *,
                stage: str,
                keyboard_stage: str | None = None,
            ) -> None:
                text = shadow_feedback_case_text(
                    record,
                    stage=stage,
                )
                keyboard = (
                    shadow_feedback_keyboard(
                        record.id,
                        stage=keyboard_stage,
                    )
                    if keyboard_stage is not None
                    else None
                )
                if callback.message is not None:
                    try:
                        await callback.message.edit_text(
                            text=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                        return
                    except Exception:
                        logger.debug(
                            "Could not edit Shadow feedback alert.",
                            exc_info=True,
                        )
                await dashboard_service.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            if action == "sagree":
                saved = await control_repository.agree_shadow_feedback(
                    case_id=case_id,
                    moderator_admin_id=admin_id,
                )
                if saved is None:
                    await safe_callback_answer(
                        callback,
                        "This case is already being reviewed or was resolved.",
                        show_alert=True,
                    )
                    return
                await edit_feedback_alert(
                    saved,
                    stage="saved",
                )
                await safe_callback_answer(
                    callback,
                    "Feedback saved",
                )
                return

            if action == "sdisagree":
                review_message_id = (
                    callback.message.message_id
                    if callback.message is not None
                    else None
                )
                claimed = (
                    await control_repository.begin_shadow_feedback_disagreement(
                        case_id=case_id,
                        moderator_admin_id=admin_id,
                        review_message_id=review_message_id,
                    )
                )
                if claimed is None:
                    await safe_callback_answer(
                        callback,
                        "This case was already resolved.",
                        show_alert=True,
                    )
                    return
                await edit_feedback_alert(
                    claimed,
                    stage="explain",
                    keyboard_stage="input",
                )
                await safe_callback_answer(
                    callback,
                    "Send your explanation as a normal message",
                )
                return

            if action == "sfsave":
                saved = await control_repository.confirm_shadow_feedback(
                    case_id=case_id,
                    moderator_admin_id=admin_id,
                )
                if saved is None:
                    await safe_callback_answer(
                        callback,
                        "No correction is waiting for confirmation.",
                        show_alert=True,
                    )
                    return
                await edit_feedback_alert(
                    saved,
                    stage="saved",
                )
                await safe_callback_answer(
                    callback,
                    "Correction learned for this community",
                )
                return

            if action == "sfclarify":
                pending = await control_repository.clarify_shadow_feedback(
                    case_id=case_id,
                    moderator_admin_id=admin_id,
                )
                if pending is None:
                    await safe_callback_answer(
                        callback,
                        "This correction is no longer editable.",
                        show_alert=True,
                    )
                    return
                await edit_feedback_alert(
                    pending,
                    stage="clarify",
                    keyboard_stage="input",
                )
                await safe_callback_answer(
                    callback,
                    "Send the clarification as a normal message",
                )
                return

            if action == "sfcancel":
                cancelled = await control_repository.cancel_shadow_feedback(
                    case_id=case_id,
                    moderator_admin_id=admin_id,
                )
                if cancelled is None:
                    await safe_callback_answer(
                        callback,
                        "Nothing to cancel.",
                        show_alert=True,
                    )
                    return
                await edit_feedback_alert(
                    cancelled,
                    stage="pending",
                    keyboard_stage="pending",
                )
                await safe_callback_answer(
                    callback,
                    "Feedback draft cancelled",
                )
                return

        if action == "test":
            chat_id = int(parts[2])

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer(
                toast or "Test mode"
            )
            return

        elif action == "test_shadow":
            chat_id = int(parts[2])

            enabled = await control_repository.toggle_shadow(
                chat_id
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=(
                    "🛡 Shadow mode: "
                    + ("ON" if enabled else "OFF")
                ),
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer("Shadow updated")
            return

        elif action == "test_ban_toggle":
            chat_id = int(parts[2])

            enabled = await control_repository.toggle_live_ban(
                chat_id
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=(
                    "🚫 Auto-ban: "
                    + ("ON" if enabled else "OFF")
                ),
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer("Auto-ban updated")
            return

        elif action in {"test_ticket", "test_report"}:
            # Legacy "test_ticket" callback is kept only so an old cached
            # button cannot break. New UI exposes Test Report only.
            chat_id = int(parts[2])

            ticket, status = await run_report_simulation(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=status,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer(
                f"Report simulation → {ticket.ticket_key}"
            )
            return

        elif action == "test_delete":
            chat_id = int(parts[2])

            await callback.answer(
                "Delete test started"
            )

            _, status = await run_delete_probe(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=status,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )
            return

        elif action == "test_ban":
            chat_id = int(parts[2])

            _, status = await run_ban_permission_probe(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=status,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer("Ban test complete")
            return

        elif action == "test_mute":
            chat_id = int(parts[2])
            _, status = await run_mute_permission_probe(
                dashboard_service=dashboard_service, chat_id=chat_id
            )
            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service, chat_id=chat_id, status=status
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="test",
            )
            await callback.answer("Mute test complete")
            return

        elif action == "test_spam":
            chat_id = int(parts[2])
            status = await spam_ladder_simulation_status(
                dashboard_service=dashboard_service, chat_id=chat_id
            )
            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service, chat_id=chat_id, status=status
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="test",
            )
            await callback.answer("Spam ladder simulated")
            return

        elif action == "test_tiers":
            chat_id = int(parts[2])
            status = await enforcement_tiers_simulation_status(
                dashboard_service=dashboard_service, chat_id=chat_id
            )
            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service, chat_id=chat_id, status=status
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="test",
            )
            await callback.answer("Policy tiers simulated")
            return

        elif action == "test_raid":
            chat_id = int(parts[2])

            text, keyboard = await raid_preview_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test_raid",
            )

            await callback.answer("Raid simulation")
            return

        elif action == "test_raid_run":
            chat_id = int(parts[2])

            text, keyboard = await raid_result_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test_raid",
            )

            await callback.answer("Anti-raid simulated")
            return

        elif action == "test_clear":
            chat_id = int(parts[2])

            status = await clear_all_test_artifacts(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
            )

            text, keyboard = await build_test_mode_payload(
                dashboard_service=dashboard_service,
                chat_id=chat_id,
                status=status,
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="test",
            )

            await callback.answer("All test artifacts cleared")
            return

        elif action == "dash":
            chat_id = int(
                parts[2]
            )

            await dashboard_service.open_dashboard(
                admin_id=admin_id,
                chat_id=chat_id,
            )

            await callback.answer(
                toast
                or "Dashboard updated"
            )
            return

        elif action == "chats":
            text, keyboard = (
                await dashboard_service
                .chats_payload(admin_id=admin_id)
            )

            state = (
                await control_repository
                .get_dashboard_state(
                    admin_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=(
                    state.selected_chat_id
                    if state
                    else None
                ),
                view="chats",
            )

        elif action == "policy":
            chat_id = int(parts[2])

            text, keyboard = await dashboard_service.policy_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )

            await callback.answer("Community policy")
            return

        elif action == "policy_edit":
            chat_id = int(parts[2])

            # Fresh edit starts from the currently active policy.
            await control_repository.clear_community_policy_draft(
                admin_id
            )

            text, keyboard = await dashboard_service.policy_input_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy_input",
            )

            await callback.answer("Send rules as a message")
            return

        elif action == "policy_change":
            chat_id = int(parts[2])

            # Keep the pending draft so the next instruction can refine it.
            text, keyboard = await dashboard_service.policy_input_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy_input",
            )

            await callback.answer("Send the correction")
            return

        elif action == "policy_apply":
            chat_id = int(parts[2])

            version = await community_policy_service.apply_draft(
                admin_id=admin_id,
                chat_id=chat_id,
            )

            if version is None:
                await callback.answer(
                    "Policy draft not found.",
                    show_alert=True,
                )
                return

            text, keyboard = await dashboard_service.policy_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )

            await callback.answer(
                f"Custom policy v{version.version} active"
            )
            return

        elif action == "policy_cancel":
            chat_id = int(parts[2])

            await control_repository.clear_community_policy_draft(
                admin_id
            )

            text, keyboard = await dashboard_service.policy_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )

            await callback.answer("Cancelled")
            return

        elif action == "policy_prev":
            chat_id = int(parts[2])

            version = await community_policy_service.rollback_previous(
                chat_id=chat_id,
                admin_id=admin_id,
            )

            if version is None:
                await callback.answer(
                    "Previous policy version not found.",
                    show_alert=True,
                )
                return

            text, keyboard = await dashboard_service.policy_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )

            await callback.answer(
                f"Rolled back → v{version.version}"
            )
            return

        elif action == "policy_clear":
            chat_id = int(parts[2])

            text, keyboard = (
                await dashboard_service.policy_clear_confirm_payload(
                    chat_id=chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy_clear",
            )

            await callback.answer()
            return

        elif action == "policy_clear_apply":
            chat_id = int(parts[2])

            version = await community_policy_service.clear_custom_rules(
                chat_id=chat_id,
                admin_id=admin_id,
            )

            text, keyboard = await dashboard_service.policy_payload(
                chat_id=chat_id
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )

            await callback.answer(
                f"Custom rules cleared · v{version.version}"
            )
            return

        elif action == "settings":
            chat_id = int(
                parts[2]
            )

            text, keyboard = (
                await dashboard_service
                .settings_payload(
                    chat_id=chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="settings",
            )

        elif action == "tools":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.tools_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="tools",
            )
            await callback.answer("Safety & tools")
            return

        elif action == "light_memory":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.light_memory_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="light_memory",
            )
            await callback.answer("Light offense memory")
            return

        elif action == "light_memory_set":
            chat_id = int(parts[2])
            hours = int(parts[3])
            applied = await control_repository.set_light_offense_decay_hours(
                chat_id, hours
            )
            text, keyboard = await dashboard_service.light_memory_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="light_memory",
            )
            label = "never" if applied <= 0 else f"{applied}h"
            await callback.answer(f"Light memory: {label}")
            return

        elif action == "immune":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.immunity_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="immunity",
            )
            await callback.answer("Immunity list")
            return

        elif action == "immune_add":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.immunity_input_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="immunity_input",
            )
            await callback.answer("Send @username or ID")
            return

        elif action == "immune_del":
            chat_id = int(parts[2])
            record_id = int(parts[3])
            removed = await control_repository.remove_moderation_immunity(
                chat_id=chat_id,
                record_id=record_id,
            )
            text, keyboard = await dashboard_service.immunity_payload(
                chat_id=chat_id,
                status=("Immunity removed" if removed else "Entry already removed"),
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="immunity",
            )
            await callback.answer("Updated")
            return

        elif action == "shadow":
            chat_id = int(
                parts[2]
            )

            shadow_enabled = await control_repository.toggle_shadow(
                chat_id
            )
            if not shadow_enabled and safety_circuit is not None:
                # Human explicitly re-enabled LIVE/DRY behavior after review.
                safety_circuit.reset_runtime_counters(chat_id)

            text, keyboard = (
                await dashboard_service
                .settings_payload(
                    chat_id=chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="settings",
            )

        elif action == "ban_toggle":
            chat_id = int(parts[2])

            enabled = await control_repository.toggle_live_ban(
                chat_id
            )

            text, keyboard = (
                await dashboard_service
                .settings_payload(
                    chat_id=chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="settings",
            )

            await callback.answer(
                "Auto-ban ON"
                if enabled
                else "Auto-ban OFF"
            )
            return

        elif action == "mute_duration":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.mute_duration_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="mute_duration",
            )
            await callback.answer("Mute duration")
            return

        elif action == "mute_set":
            chat_id = int(parts[2])
            minutes = int(parts[3])
            applied = await control_repository.set_mute_duration_minutes(chat_id, minutes)
            text, keyboard = await dashboard_service.mute_duration_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="mute_duration",
            )
            await callback.answer(f"Mute duration: {applied} min")
            return

        elif action == "raid_toggle":
            chat_id = int(parts[2])

            if not raid_guard_service.semantic_service.enabled:
                await callback.answer(
                    "Raid Guard unavailable: install/start the embedding model.",
                    show_alert=True,
                )
                return

            enabled = await control_repository.toggle_raid_guard(
                chat_id
            )

            text, keyboard = await dashboard_service.settings_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="settings",
            )
            await callback.answer(
                "Raid Guard ON" if enabled else "Raid Guard OFF"
            )
            return

        elif action == "safety":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.safety_policy_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="safety",
            )
            await callback.answer("Safety policy")
            return

        elif action == "raid_incident":
            incident_id = int(parts[2])
            payload = await dashboard_service.raid_incident_payload(
                incident_id=incident_id
            )
            if payload is None:
                await callback.answer(
                    "Raid incident not found.",
                    show_alert=True,
                )
                return
            text, keyboard, chat_id = payload
            if not await can_access_chat(admin_id, chat_id, pilot_access_service):
                await safe_callback_answer(callback, "Not authorized for this community.", show_alert=True)
                return
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="raid_incident",
            )
            await callback.answer("Raid incident")
            return

        elif action == "raid_unban":
            incident_id = int(parts[2])
            incident = await control_repository.get_raid_incident(incident_id)
            if incident is None:
                await safe_callback_answer(callback, "Raid incident not found.", show_alert=True)
                return
            if not await can_access_chat(admin_id, incident.chat_id, pilot_access_service):
                await safe_callback_answer(callback, "Not authorized for this community.", show_alert=True)
                return
            unbanned, failed = await raid_guard_service.unban_incident_users(
                incident_id=incident_id
            )
            payload = await dashboard_service.raid_incident_payload(
                incident_id=incident_id
            )
            if payload is not None:
                text, keyboard, chat_id = payload
                await dashboard_service.render(
                    admin_id=admin_id,
                    text=text,
                    keyboard=keyboard,
                    selected_chat_id=chat_id,
                    view="raid_incident",
                )
            await callback.answer(
                f"Unbanned {unbanned}" + (f" · failed {failed}" if failed else ""),
                show_alert=bool(failed),
            )
            return

        elif action == "shadow_clear":
            chat_id = int(parts[2])
            artifacts = await control_repository.list_admin_alert_artifacts(
                managed_chat_id=chat_id,
                kind="shadow_alert",
            )
            deleted = 0
            for artifact in artifacts:
                try:
                    await dashboard_service.bot.delete_message(
                        chat_id=artifact.admin_chat_id,
                        message_id=artifact.telegram_message_id,
                    )
                    deleted += 1
                except Exception:
                    logger.debug(
                        "Could not delete old shadow alert | id=%s",
                        artifact.id,
                        exc_info=True,
                    )
            await control_repository.purge_admin_alert_artifacts(
                managed_chat_id=chat_id,
                kind="shadow_alert",
            )
            text, keyboard = await dashboard_service.tools_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="tools",
            )
            await callback.answer(f"Cleared {deleted} shadow alert(s)")
            return

        elif action == "bans":
            chat_id = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
            text, keyboard = await dashboard_service.bans_payload(
                chat_id=chat_id,
                page=page,
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="bans",
            )
            await callback.answer("Banned users")
            return

        elif action == "bans_search":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.bans_search_prompt_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="bans_search",
            )
            await callback.answer("Send @username or user ID")
            return

        elif action == "unban":
            ban_id = int(parts[2])
            record = await control_repository.get_moderation_ban(ban_id)
            if record is None or not record.active:
                await callback.answer("Ban record is no longer active.", show_alert=True)
                return
            if not await can_access_chat(admin_id, record.chat_id, pilot_access_service):
                await safe_callback_answer(callback, "Not authorized for this community.", show_alert=True)
                return

            try:
                await dashboard_service.bot.unban_chat_member(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    only_if_banned=True,
                )
            except TelegramMigrateToChat as exc:
                new_chat_id = await control_repository.register_chat_migration(
                    old_chat_id=record.chat_id,
                    new_chat_id=int(exc.migrate_to_chat_id),
                )
                logger.info(
                    "UNBAN CHAT MIGRATION RETRY | old=%s | new=%s | ban_id=%s",
                    record.chat_id, new_chat_id, ban_id,
                )
                await dashboard_service.bot.unban_chat_member(
                    chat_id=new_chat_id,
                    user_id=record.user_id,
                    only_if_banned=True,
                )
                record = await control_repository.get_moderation_ban(ban_id)
            except Exception as exc:
                logger.exception("Manual unban failed | ban_id=%s", ban_id)
                await safe_callback_answer(
                    callback,
                    "Telegram unban failed: " + str(exc)[:120],
                    show_alert=True,
                )
                return

            await control_repository.mark_moderation_unbanned(ban_id=ban_id)
            text, keyboard = await dashboard_service.bans_payload(chat_id=record.chat_id, page=0)
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=record.chat_id, view="bans",
            )
            await callback.answer("User unbanned")
            return

        elif action == "diag":
            chat_id = int(parts[2])
            await safe_callback_answer(callback, "Running diagnostics…")
            text, keyboard, chat_id = await dashboard_service.diagnostics_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="diagnostics",
            )
            return

        elif action == "activity":
            chat_id = int(
                parts[2]
            )

            hours = int(
                parts[3]
            )

            text, keyboard = (
                await dashboard_service
                .activity_payload(
                    chat_id=chat_id,
                    hours=hours,
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="activity",
            )

        elif action == "tickets":
            chat_id = int(
                parts[2]
            )

            text, keyboard = (
                await dashboard_service
                .tickets_payload(
                    chat_id=chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="tickets",
            )

        elif action == "ticket":
            ticket_id = int(
                parts[2]
            )

            payload = (
                await dashboard_service
                .ticket_payload(
                    ticket_id=ticket_id
                )
            )

            if payload is None:
                await callback.answer(
                    "Ticket not found.",
                    show_alert=True,
                )
                return

            (
                text,
                keyboard,
                chat_id,
            ) = payload

            if not await can_access_chat(admin_id, chat_id, pilot_access_service):
                await safe_callback_answer(callback, "Not authorized for this community.", show_alert=True)
                return

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="ticket",
            )

        elif action == "tact":
            # Telegram callback queries expire quickly. Acknowledge the click
            # before Telegram/API/LLM/dashboard work can take several seconds.
            await safe_callback_answer(callback, "Processing…")

            ticket_id = int(
                parts[2]
            )

            ticket_action = (
                parts[3]
            )

            ticket = (
                await control_repository
                .get_ticket(
                    ticket_id
                )
            )

            if ticket is None:
                await safe_callback_answer(callback,
                    "Ticket not found.",
                    show_alert=True,
                )
                return

            if not await can_access_chat(admin_id, ticket.chat_id, pilot_access_service):
                await safe_callback_answer(callback, "Not authorized for this community.", show_alert=True)
                return

            if is_test_ticket(ticket):
                success, action_status = await execute_test_ticket_action(
                    dashboard_service=dashboard_service,
                    ticket=ticket,
                    action=ticket_action,
                )

                if not success:
                    await safe_callback_answer(callback,
                        action_status[:180],
                        show_alert=True,
                    )
                    return

                await control_repository.resolve_ticket(
                    ticket_id=ticket_id,
                    action=ticket_action,
                )

                dashboard_service.request_refresh(
                    ticket.chat_id
                )

                text, keyboard = await build_test_ticket_result_payload(
                    dashboard_service=dashboard_service,
                    chat_id=ticket.chat_id,
                    action=ticket_action,
                    result_details=action_status,
                )

                await dashboard_service.render(
                    admin_id=admin_id,
                    text=text,
                    keyboard=keyboard,
                    selected_chat_id=ticket.chat_id,
                    view="test",
                )

                await safe_callback_answer(callback,
                    "Test action executed"
                )
                return

            bot = dashboard_service.bot

            if (
                ticket_action
                == "delete"
                and ticket.telegram_message_id
                is not None
            ):
                try:
                    await bot.delete_message(
                        chat_id=ticket.chat_id,
                        message_id=(
                            ticket.telegram_message_id
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "Ticket delete failed",
                        exc_info=True,
                    )
                    await safe_callback_answer(callback,
                        "Telegram delete failed: " + str(exc)[:120],
                        show_alert=True,
                    )
                    return

            elif (
                ticket_action
                == "ban"
                and ticket.target_user_id
                is not None
            ):
                await bot.ban_chat_member(
                    chat_id=ticket.chat_id,
                    user_id=ticket.target_user_id,
                )

                # A BAN always includes cleanup of the trigger message.
                # The ban itself remains successful even if Telegram can no
                # longer delete the original message (already removed/too old).
                if ticket.telegram_message_id is not None:
                    try:
                        await bot.delete_message(
                            chat_id=ticket.chat_id,
                            message_id=ticket.telegram_message_id,
                        )
                    except Exception:
                        logger.warning(
                            "Ticket ban: message delete failed",
                            exc_info=True,
                        )

                await control_repository.record_moderation_ban(
                    chat_id=ticket.chat_id,
                    user_id=ticket.target_user_id,
                    username=ticket.username,
                    source="ticket",
                    category=ticket.category,
                    reason=ticket.reason,
                )

            elif (
                ticket_action == "mute"
                and ticket.target_user_id is not None
            ):
                duration = await control_repository.get_mute_duration_minutes(ticket.chat_id)
                await bot.restrict_chat_member(
                    chat_id=ticket.chat_id,
                    user_id=ticket.target_user_id,
                    permissions=ChatPermissions(
                        can_send_messages=False, can_send_audios=False,
                        can_send_documents=False, can_send_photos=False,
                        can_send_videos=False, can_send_video_notes=False,
                        can_send_voice_notes=False, can_send_polls=False,
                        can_send_other_messages=False, can_add_web_page_previews=False,
                    ),
                    until_date=datetime.now(timezone.utc) + timedelta(minutes=duration),
                )
                if ticket.telegram_message_id is not None:
                    try:
                        await bot.delete_message(
                            chat_id=ticket.chat_id, message_id=ticket.telegram_message_id
                        )
                    except Exception:
                        logger.warning("Ticket mute: message delete failed", exc_info=True)

            elif ticket_action == "warn":
                who = (
                    ticket.username
                    or (
                        f"user "
                        f"{ticket.target_user_id}"
                    )
                )

                await bot.send_message(
                    chat_id=ticket.chat_id,
                    text=(
                        f"⚠️ {who}, "
                        "this message violates the community rules."
                    ),
                )

            elif ticket_action != "allow":
                await safe_callback_answer(callback,
                    "Unsupported action.",
                    show_alert=True,
                )
                return

            # REAL human decision becomes effective moderation history so
            # progressive Warn -> Mute -> Ban ladders also respect human
            # confirmations. TEST-* tickets returned above and never reach
            # this branch.
            await moderation_executor.record_manual_ticket_resolution(
                ticket=ticket,
                action=ticket_action,
                moderator_admin_id=admin_id,
            )

            # The same REAL human decision also becomes chat-scoped semantic
            # Feedback Memory for future gray cases.
            await feedback_service.learn_from_ticket(
                ticket=ticket,
                moderator_action=ticket_action,
                moderator_admin_id=admin_id,
            )

            await control_repository.resolve_ticket(
                ticket_id=ticket_id,
                action=ticket_action,
            )

            dashboard_service.request_refresh(
                ticket.chat_id
            )

            text, keyboard = (
                await dashboard_service
                .tickets_payload(
                    chat_id=ticket.chat_id
                )
            )

            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=(
                    ticket.chat_id
                ),
                view="tickets",
            )

            await safe_callback_answer(
                callback,
                "Done.",
            )
            return

        await safe_callback_answer(
            callback,
            toast or "",
        )

    except Exception as exc:
        logger.exception(
            "Admin control callback failed"
        )

        await safe_callback_answer(
            callback,
            f"Error: {str(exc)[:120]}",
            show_alert=True,
        )


@router.message(
    PRIVATE_CHAT,
    F.text,
)
async def community_policy_text_input(
    message: Message,
    dashboard_service: DashboardService,
    control_repository: ControlRepository,
    community_policy_service: CommunityPolicyService,
    feedback_service: ModeratorFeedbackService,
    app_settings: Settings,
    pilot_access_service=None,
) -> None:
    if (
        not message.from_user
        or not await allowed(
            message.from_user.id,
            app_settings,
            pilot_access_service,
        )
    ):
        return

    # Optional private Pilot Ops input flow (grant/search/etc.). The public
    # repository only exposes this hook; the implementation lives in the
    # git-ignored private_ops package.
    if pilot_access_service is not None and message.from_user is not None:
        try:
            if await pilot_access_service.handle_private_text(message):
                return
        except Exception:
            logger.exception("Pilot Ops text handler failed")

    # Command handlers above own slash commands.
    if (
        not message.text
        or message.text.startswith("/")
    ):
        return

    admin_id = message.from_user.id

    # Shadow feedback input is independent from the pinned dashboard. An
    # authorized moderator can teach ModGuard directly from a Shadow alert even
    # if the dashboard was not opened in this private chat.
    pending_shadow = (
        await control_repository.pending_shadow_feedback_for_admin(
            moderator_admin_id=admin_id
        )
    )
    if pending_shadow is not None:
        if not await can_access_chat(
            admin_id,
            pending_shadow.chat_id,
            pilot_access_service,
        ):
            await control_repository.cancel_shadow_feedback(
                case_id=pending_shadow.id,
                moderator_admin_id=admin_id,
            )
            return

        new_text = (message.text or "").strip()
        previous_text = (
            pending_shadow.moderator_explanation or ""
        ).strip()
        combined_text = (
            new_text
            if not previous_text
            else previous_text + "\nClarification: " + new_text
        )

        try:
            interpretation = (
                await feedback_service.interpret_shadow_feedback(
                    case=pending_shadow,
                    moderator_explanation=combined_text,
                )
            )

            saved_case = (
                await control_repository.save_shadow_feedback_interpretation(
                    case_id=pending_shadow.id,
                    moderator_admin_id=admin_id,
                    moderator_explanation=combined_text,
                    corrected_action=interpretation.corrected_action,
                    corrected_category=interpretation.category,
                    corrected_severity=interpretation.severity,
                    local_rule=interpretation.local_rule,
                    relationship_note=(
                        interpretation.relationship_note
                        if interpretation.relationship_relevant
                        else ""
                    ),
                    apply_to_same_pair=(
                        interpretation.relationship_relevant
                        and interpretation.apply_to_same_pair
                    ),
                    interpretation=interpretation.model_dump(),
                )
            )

            if saved_case is None:
                raise RuntimeError(
                    "Shadow feedback draft expired before it could be saved."
                )

            preview_text = shadow_feedback_case_text(
                saved_case,
                stage="confirm",
                interpretation_summary=interpretation.summary,
                unsupported_assumptions=(
                    interpretation.unsupported_assumptions
                ),
            )
            preview_keyboard = shadow_feedback_keyboard(
                saved_case.id,
                stage="confirm",
            )

            edited = False
            if saved_case.review_message_id is not None:
                try:
                    await dashboard_service.bot.edit_message_text(
                        chat_id=admin_id,
                        message_id=saved_case.review_message_id,
                        text=preview_text,
                        parse_mode="HTML",
                        reply_markup=preview_keyboard,
                    )
                    edited = True
                except Exception:
                    logger.debug(
                        "Could not edit Shadow feedback review message.",
                        exc_info=True,
                    )

            if not edited:
                await dashboard_service.bot.send_message(
                    chat_id=admin_id,
                    text=preview_text,
                    parse_mode="HTML",
                    reply_markup=preview_keyboard,
                )

        except Exception as exc:
            logger.exception(
                "Shadow feedback interpretation failed | case=%s",
                pending_shadow.id,
            )
            error_text = (
                shadow_feedback_case_text(
                    pending_shadow,
                    stage="explain",
                )
                + "\n\n⚠️ <b>I could not interpret that feedback.</b> "
                  "Please try again with a little more detail.\n"
                + html.escape(str(exc)[:220])
            )
            try:
                if pending_shadow.review_message_id is not None:
                    await dashboard_service.bot.edit_message_text(
                        chat_id=admin_id,
                        message_id=pending_shadow.review_message_id,
                        text=error_text,
                        parse_mode="HTML",
                        reply_markup=shadow_feedback_keyboard(
                            pending_shadow.id,
                            stage="input",
                        ),
                    )
                else:
                    await dashboard_service.bot.send_message(
                        chat_id=admin_id,
                        text=error_text,
                        parse_mode="HTML",
                        reply_markup=shadow_feedback_keyboard(
                            pending_shadow.id,
                            stage="input",
                        ),
                    )
            except Exception:
                logger.debug(
                    "Could not render Shadow feedback error.",
                    exc_info=True,
                )

        finally:
            try:
                await message.delete()
            except Exception:
                logger.debug(
                    "Could not delete Shadow feedback input message.",
                    exc_info=True,
                )
        return

    state = await control_repository.get_dashboard_state(
        admin_id
    )

    if state is None or state.selected_chat_id is None:
        return

    chat_id = int(state.selected_chat_id)

    if not await can_access_chat(admin_id, chat_id, pilot_access_service):
        await control_repository.clear_dashboard_state(admin_id)
        return

    if state.view == "bans_search":
        text, keyboard = await dashboard_service.bans_search_results_payload(
            chat_id=chat_id,
            query=message.text,
        )
        await dashboard_service.render(
            admin_id=admin_id,
            text=text,
            keyboard=keyboard,
            selected_chat_id=chat_id,
            view="bans_search_results",
        )
        try:
            await message.delete()
        except Exception:
            logger.debug("Could not delete ban search input.", exc_info=True)
        return

    if state.view == "immunity_input":
        raw = (message.text or "").strip()
        try:
            if raw.lstrip("-").isdigit() and not raw.startswith("-"):
                user_id = int(raw)
                if user_id <= 0:
                    raise ValueError("Telegram user ID must be a positive integer.")
                await control_repository.add_moderation_immunity(
                    chat_id=chat_id,
                    user_id=user_id,
                    created_by_admin_id=admin_id,
                )
                status = f"Added ID {user_id}"
            else:
                username = raw.lstrip("@").strip()
                if not username or any(ch.isspace() for ch in username):
                    raise ValueError("Send one @username or numeric Telegram user ID.")
                await control_repository.add_moderation_immunity(
                    chat_id=chat_id,
                    username=username,
                    created_by_admin_id=admin_id,
                )
                status = f"Added @{username}"

            text, keyboard = await dashboard_service.immunity_payload(
                chat_id=chat_id,
                status=status,
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="immunity",
            )
        except Exception as exc:
            text, keyboard = await dashboard_service.immunity_input_payload(
                chat_id=chat_id,
                error=str(exc),
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="immunity_input",
            )
        finally:
            try:
                await message.delete()
            except Exception:
                logger.debug("Could not delete immunity input message.", exc_info=True)
        return

    if state.view != "policy_input":
        return

    compiling_text, compiling_keyboard = (
        await dashboard_service.policy_compiling_payload(
            chat_id=chat_id
        )
    )

    await dashboard_service.render(
        admin_id=admin_id,
        text=compiling_text,
        keyboard=compiling_keyboard,
        selected_chat_id=chat_id,
        view="policy_compiling",
    )

    try:
        await community_policy_service.compile_update(
            admin_id=admin_id,
            chat_id=chat_id,
            admin_instruction=message.text,
        )

        payload = await dashboard_service.policy_preview_payload(
            admin_id=admin_id
        )

        if payload is None:
            raise RuntimeError(
                "Policy compiler did not create a draft."
            )

        text, keyboard, selected_chat_id = payload

        await dashboard_service.render(
            admin_id=admin_id,
            text=text,
            keyboard=keyboard,
            selected_chat_id=selected_chat_id,
            view="policy_preview",
        )

    except Exception as exc:
        logger.exception(
            "Community policy compilation failed"
        )

        text, keyboard = await dashboard_service.policy_input_payload(
            chat_id=chat_id,
            error=str(exc),
        )

        await dashboard_service.render(
            admin_id=admin_id,
            text=text,
            keyboard=keyboard,
            selected_chat_id=chat_id,
            view="policy_input",
        )

    finally:
        try:
            await message.delete()
        except Exception:
            logger.debug(
                "Could not delete policy input message.",
                exc_info=True,
            )
