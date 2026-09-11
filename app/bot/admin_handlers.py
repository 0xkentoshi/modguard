import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatPermissions, Message

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
