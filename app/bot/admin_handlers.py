import html
import json
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
    # Static public ADMIN_IDS always retain access. An optional external
    # access provider can grant additional scoped access, but must never
    # revoke the repository owner's/admin's explicit ADMIN_IDS access.
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


async def delete_transient_admin_card(
    *,
    callback: CallbackQuery,
    repository: ControlRepository,
    admin_id: int,
) -> bool:
    """Delete a ticket/review card without ever deleting the persistent dashboard."""
    if callback.message is None:
        return False

    try:
        state = await repository.get_dashboard_state(int(admin_id))
    except Exception:
        logger.debug("Could not resolve dashboard state before card cleanup.", exc_info=True)
        state = None

    if (
        state is not None
        and state.dashboard_message_id is not None
        and int(state.dashboard_message_id) == int(callback.message.message_id)
    ):
        logger.warning(
            "TRANSIENT CARD DELETE SKIPPED | reason=persistent_dashboard | admin=%s | message=%s",
            admin_id,
            callback.message.message_id,
        )
        return False

    try:
        await callback.message.delete()
        return True
    except Exception:
        logger.debug("Could not delete transient admin card.", exc_info=True)
        return False


async def sync_shadow_feedback_mirrors(
    *,
    dashboard_service: DashboardService,
    repository: ControlRepository,
    record,
    save_note: str,
    save_outcome: str,
) -> None:
    """Best-effort update of every DM mirror for one resolved Shadow case."""
    reader = getattr(repository, "list_admin_alert_artifacts", None)
    if not callable(reader):
        return
    try:
        artifacts = await reader(
            managed_chat_id=int(record.chat_id),
            kind=f"shadow_alert:{int(record.id)}",
        )
        try:
            community_title = await dashboard_service._chat_title(int(record.chat_id))
        except Exception:
            community_title = None
        text = shadow_feedback_case_text(
            record,
            stage="saved",
            save_note=save_note,
            save_outcome=save_outcome,
            community_title_override=community_title,
        )
        resolver = getattr(record, "moderator_admin_id", None)
        if resolver is not None:
            text += f"\n\nHandled by moderator <code>{int(resolver)}</code>."
        keyboard = shadow_feedback_keyboard(
            int(record.id),
            stage="saved",
            chat_id=int(record.chat_id),
        )
        for artifact in artifacts:
            try:
                await dashboard_service.bot.edit_message_text(
                    chat_id=int(artifact.admin_chat_id),
                    message_id=int(artifact.telegram_message_id),
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.debug(
                    "Could not sync resolved Shadow mirror | case=%s | artifact=%s",
                    record.id,
                    getattr(artifact, "id", None),
                    exc_info=True,
                )
    except Exception:
        logger.debug(
            "Could not enumerate resolved Shadow mirrors | case=%s",
            getattr(record, "id", None),
            exc_info=True,
        )


def moderation_action_label(action: str | None) -> str:
    value = str(action or "escalate").strip().casefold()
    if value == "mute":
        return "MUTE + DELETE"
    if value == "ban":
        return "BAN + DELETE"
    if value == "delete":
        return "DELETE"
    if value == "warn":
        return "WARN"
    if value == "allow":
        return "ALLOW"
    return value.upper()


def shadow_feedback_case_text(
    case,
    *,
    stage: str = "pending",
    interpretation_summary: str | None = None,
    unsupported_assumptions: list[str] | None = None,
    save_note: str | None = None,
    save_outcome: str | None = None,
    community_title_override: str | None = None,
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

    try:
        case_context = json.loads(
            getattr(case, "context_json", "{}") or "{}"
        )
    except Exception:
        case_context = {}

    core_action = str(
        case_context.get("core_recommendation")
        or moderation_action_label(getattr(case, "ai_action", "escalate"))
    ).upper()
    policy_action = str(
        case_context.get("community_policy_result")
        or moderation_action_label(getattr(case, "ai_action", "escalate"))
    ).upper()
    effective_action = str(
        case_context.get("effective_shadow_action")
        or policy_action
    ).upper()

    community_title = str(
        community_title_override
        or case_context.get("chat_title")
        or f"chat {getattr(case, 'chat_id', 'unknown')}"
    )

    base = (
        f"🛡 <b>SHADOW FEEDBACK</b>\n"
        f"🏘 <b>Community:</b> {html.escape(community_title)}\n"
        f"{html.escape(str(who))} · "
        f"{html.escape(str(getattr(case, 'ai_category', None) or 'other'))} · "
        f"{confidence}\n\n"
        f"<code>{html.escape(message_text or '[non-text message]')}</code>\n\n"
        f"<b>AI/Core recommendation:</b> {html.escape(core_action)}\n"
        + (
            f"<b>Community policy result:</b> {html.escape(policy_action)}\n"
            if case_context.get("policy_source") == "community_policy"
            else ""
        )
        + f"<b>With current safety settings:</b> {html.escape(effective_action)}\n"
        + f"<b>Why:</b> {html.escape((getattr(case, 'ai_reason', '') or '')[:500])}"
    )

    if stage == "explain":
        return (
            base
            + "\n\n❌ <b>You disagree.</b>\n"
            + "Explain in one normal message <b>why this decision is wrong</b> "
              "and <b>what ModGuard should do instead</b>.\n\n"
            + "<b>Best feedback format</b>\n"
              "1. What ModGuard misunderstood\n"
              "2. When this rule should apply\n"
              "3. Correct action\n\n"
              "<i>Example: Friendly insults are allowed while both users are clearly "
              "joking. If one asks to stop and the other continues, treat it as "
              "harassment. Correct action: WARN.</i>\n\n"
            + "Natural language and slang are fine. ModGuard will first show how it "
              "understood you; nothing is learned until you confirm it."
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
            moderation_action_label(getattr(case, "corrected_action", None))
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
            moderation_action_label(getattr(case, "moderator_action", "allow"))
        )
        note = (
            save_note
            or "Saved as chat-local feedback for future gray cases."
        )

        # A rejected correction must never be presented as the "correct"
        # action. In particular, Protected Core may accept the feedback for
        # audit while refusing to promote phishing/scam -> ALLOW into policy.
        if save_outcome == "protected_audit_only":
            return (
                base
                + f"\n\n✅ <b>Feedback received.</b> "
                  f"Requested correction: <b>{action}</b>.\n"
                + "🛡 <b>Protected Core blocked this policy change.</b>\n"
                + html.escape(note)
            )

        if save_outcome == "safety_review_failed":
            return (
                base
                + f"\n\n✅ <b>Feedback received.</b> "
                  f"Requested correction: <b>{action}</b>.\n"
                + "🛡 <b>Policy was left unchanged because the safety "
                  "re-check did not complete safely.</b>\n"
                + html.escape(note)
            )

        if save_outcome == "confirmed":
            return (
                base
                + f"\n\n✅ <b>Feedback saved.</b> "
                  f"AI decision confirmed: <b>{action}</b>.\n"
                + html.escape(note)
            )

        return (
            base
            + f"\n\n✅ <b>Feedback saved.</b> "
              f"Corrected action: <b>{action}</b>.\n"
            + html.escape(note)
        )

    return base


def shadow_feedback_keyboard(
    case_id: int,
    *,
    stage: str,
    chat_id: int | None = None,
) -> InlineKeyboardMarkup:
    if stage == "pending":
        rows = [[
            InlineKeyboardButton(
                text="✅ AI decision correct",
                callback_data=f"mg:sagree:{case_id}",
            ),
            InlineKeyboardButton(
                text="❌ AI decision wrong",
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
    elif stage in {"input", "clarify"}:
        rows = [[
            InlineKeyboardButton(
                text="Cancel",
                callback_data=f"mg:sfcancel:{case_id}",
            )
        ]]
    else:
        rows = []

    if chat_id is not None:
        rows.append([
            InlineKeyboardButton(
                text="🧹 Clear this community alerts",
                callback_data=f"mg:shadow_clear:{chat_id}",
            )
        ])
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

    if (
        pilot_access_service is not None
        and await pilot_access_service.is_superadmin(message.from_user.id)
    ):
        await pilot_access_service.close_ops_message(message.from_user.id)

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
    # another message. Platform owners also use one panel at a time: opening
    # Dashboard removes OPS first.
    if (
        pilot_access_service is not None
        and await pilot_access_service.is_superadmin(message.from_user.id)
    ):
        await pilot_access_service.close_ops_message(message.from_user.id)
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
    Command("myid", "id"),
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

    if action in {
        "sagree",
        "sdisagree",
        "sfsave",
        "sfclarify",
        "sfcancel",
        "ticket",
        "tact",
        "tclose",
    }:
        # Shadow feedback and review cards live in separate transient messages,
        # not in the persistent dashboard. Never treat them as stale copies.
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

    claimed_ticket_id: int | None = None
    claimed_ticket_action: str | None = None
    claimed_action_committed = False

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
                save_note: str | None = None,
                save_outcome: str | None = None,
            ) -> None:
                try:
                    resolved_community_title = await dashboard_service._chat_title(
                        int(record.chat_id)
                    )
                except Exception:
                    logger.debug(
                        "Could not resolve Shadow feedback community title.",
                        exc_info=True,
                    )
                    resolved_community_title = None
                text = shadow_feedback_case_text(
                    record,
                    stage=stage,
                    save_note=save_note,
                    save_outcome=save_outcome,
                    community_title_override=resolved_community_title,
                )
                keyboard = shadow_feedback_keyboard(
                    record.id,
                    stage=(keyboard_stage or "saved"),
                    chat_id=record.chat_id,
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
                agree_note = (
                    "AI recommendation confirmed. Stored as positive "
                    "chat-local calibration; Community Policy was not changed."
                )
                await edit_feedback_alert(
                    saved,
                    stage="saved",
                    save_note=agree_note,
                    save_outcome="confirmed",
                )
                await sync_shadow_feedback_mirrors(
                    dashboard_service=dashboard_service,
                    repository=control_repository,
                    record=saved,
                    save_note=agree_note,
                    save_outcome="confirmed",
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
                policy_version, promotion = (
                    await community_policy_service.promote_shadow_feedback(
                        case=saved,
                        admin_id=admin_id,
                    )
                )

                if promotion == "policy" and policy_version is not None:
                    save_note = (
                        f"Added to Community Policy v{policy_version.version}. "
                        "You can inspect or remove the learned rule from Rules."
                    )
                elif promotion == "policy_replaced" and policy_version is not None:
                    save_note = (
                        f"Community Policy v{policy_version.version} updated. "
                        "This correction replaced older learned rule(s) for the "
                        "same moderation topic instead of creating a conflict."
                    )
                elif promotion == "protected_audit_only":
                    save_note = (
                        "Saved for audit only. An independent Protected Core "
                        "re-check found scam/phishing/malicious-link/threat safety "
                        "that this correction would weaken below its safe floor. "
                        "Community Policy was not changed."
                    )
                elif promotion == "safety_review_failed":
                    save_note = (
                        "Saved for audit only. The independent Protected Core "
                        "re-check could not complete safely, so policy learning "
                        "was skipped rather than weakening protection."
                    )
                elif promotion == "manual_conflict":
                    save_note = (
                        "Saved as feedback, but Community Policy was not changed "
                        "because a manual administrator rule already owns this "
                        "policy topic. Edit that manual rule explicitly if needed."
                    )
                elif promotion == "reconcile_uncertain":
                    save_note = (
                        "Saved as feedback, but no learned rule was created because "
                        "ModGuard could not safely determine whether this was a new "
                        "policy topic or a revision of an existing one."
                    )
                elif promotion == "pair_memory":
                    save_note = (
                        "Saved as pair-scoped relationship feedback. It was not "
                        "promoted into chat-wide Community Policy."
                    )
                elif promotion == "policy_full":
                    save_note = (
                        "Saved as soft feedback. Community Policy already has 20 "
                        "manual rules, so no rule was auto-added."
                    )
                else:
                    save_note = (
                        "Saved as chat-local feedback; no Community Policy rule "
                        "was created for this correction."
                    )

                await edit_feedback_alert(
                    saved,
                    stage="saved",
                    save_note=save_note,
                    save_outcome=promotion,
                )
                await sync_shadow_feedback_mirrors(
                    dashboard_service=dashboard_service,
                    repository=control_repository,
                    record=saved,
                    save_note=save_note,
                    save_outcome=promotion,
                )
                await safe_callback_answer(
                    callback,
                    (
                        f"Policy v{policy_version.version} updated"
                        if promotion in {"policy", "policy_replaced"}
                        and policy_version is not None
                        else (
                            "Protected Core blocked policy change"
                            if promotion == "protected_audit_only"
                            else (
                                "Safety review failed; policy unchanged"
                                if promotion == "safety_review_failed"
                                else "Correction saved"
                            )
                        )
                    ),
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
            current_settings = await control_repository.get_chat_settings(chat_id)
            target_shadow = not bool(current_settings.shadow_mode)
            if not target_shadow:
                ready, reason = await dashboard_service.live_punitive_capability(chat_id=chat_id)
                if not ready:
                    await control_repository.set_shadow(chat_id, True)
                    await safe_callback_answer(callback, reason, show_alert=True)
                    return
            enabled = await control_repository.set_shadow(chat_id, target_shadow)

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
            chat_id = int(parts[2])
            await safe_callback_answer(callback, toast or "Opening dashboard…")
            await dashboard_service.reconcile_managed_chats()
            await dashboard_service.open_dashboard(
                admin_id=admin_id,
                chat_id=chat_id,
            )
            return

        elif action == "chats":
            await safe_callback_answer(callback, "Communities")
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
            return

        elif action == "faq":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.faq_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="faq",
            )
            await safe_callback_answer(callback, "FAQ")
            return

        elif action == "ops":
            if (
                pilot_access_service is None
                or not await pilot_access_service.is_superadmin(admin_id)
            ):
                await safe_callback_answer(callback, "Superadmin only.", show_alert=True)
                return
            await safe_callback_answer(callback, "OPS")
            await dashboard_service.close_dashboard(admin_id=admin_id)
            text, keyboard = await pilot_access_service.panel_payload()
            await pilot_access_service.render_ops_message(
                superadmin_id=admin_id,
                text=text,
                keyboard=keyboard,
            )
            return

        elif action == "close":
            # Legacy v1.5.0 callback. v1.5.1 uses Dashboard <-> OPS replacement
            # instead of separate close controls.
            await safe_callback_answer(callback, "Use OPS to switch panels.")
            return

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

        elif action == "policy_security":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.policy_security_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy_security",
            )
            await safe_callback_answer(callback, "Security preset")
            return

        elif action == "policy_security_apply":
            chat_id = int(parts[2])
            preset = str(parts[3]).casefold() if len(parts) > 3 else ""
            try:
                version = await community_policy_service.apply_security_preset(
                    chat_id=chat_id,
                    admin_id=admin_id,
                    preset=preset,
                )
            except ValueError as exc:
                await safe_callback_answer(callback, str(exc), show_alert=True)
                return

            forced_shadow_reason = None
            if preset == "progressive":
                settings_row = await control_repository.get_chat_settings(chat_id)
                if not settings_row.shadow_mode:
                    ready, reason = await dashboard_service.live_punitive_capability(chat_id=chat_id)
                    if not ready:
                        await control_repository.set_shadow(chat_id, True)
                        forced_shadow_reason = reason

            text, keyboard = await dashboard_service.policy_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="policy",
            )
            label = "Progressive" if preset == "progressive" else "Strict Core"
            await safe_callback_answer(
                callback,
                (
                    f"{label} active · policy v{version.version}"
                    if forced_shadow_reason is None
                    else f"{label} active · SHADOW forced: {forced_shadow_reason[:120]}"
                ),
                show_alert=forced_shadow_reason is not None,
            )
            return

        elif action == "policy_details":
            chat_id = int(parts[2])
            text, keyboard = await dashboard_service.policy_details_payload(
                chat_id=chat_id
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="policy_details",
            )
            await callback.answer("Core policy")
            return

        elif action == "policy_rules":
            chat_id = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
            text, keyboard = await dashboard_service.policy_rules_payload(
                chat_id=chat_id, page=page
            )
            await dashboard_service.render(
                admin_id=admin_id, text=text, keyboard=keyboard,
                selected_chat_id=chat_id, view="policy_rules",
            )
            await callback.answer("Community rules")
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

        elif action == "policy_memory_reset":
            chat_id = int(parts[2])

            version, revoked = (
                await community_policy_service.reset_adaptive_memory(
                    chat_id=chat_id,
                    admin_id=admin_id,
                )
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
            suffix = (
                f" · policy v{version.version}"
                if version is not None
                else ""
            )
            await callback.answer(
                f"Learned memory reset: {revoked}{suffix}"
            )
            return

        elif action == "policy_shadow_reset":
            chat_id = int(parts[2])

            cleared = await control_repository.reset_shadow_simulation(
                chat_id=chat_id
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
                f"Shadow simulation reset: {cleared}"
            )
            return

        elif action == "policy_rule_del":
            chat_id = int(parts[2])
            rule_id = parts[3]
            return_page = int(parts[4]) if len(parts) > 4 else None

            version = await community_policy_service.remove_rule(
                chat_id=chat_id,
                admin_id=admin_id,
                rule_id=rule_id,
            )

            if version is None:
                await callback.answer(
                    "Rule not found.",
                    show_alert=True,
                )
                return

            if return_page is None:
                text, keyboard = await dashboard_service.policy_payload(
                    chat_id=chat_id
                )
                view = "policy"
            else:
                text, keyboard = await dashboard_service.policy_rules_payload(
                    chat_id=chat_id, page=return_page
                )
                view = "policy_rules"
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view=view,
            )
            await callback.answer(
                f"Rule removed · policy v{version.version}"
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
            chat_id = int(parts[2])
            current_settings = await control_repository.get_chat_settings(chat_id)
            target_shadow = not bool(current_settings.shadow_mode)

            if not target_shadow:
                ready, reason = await dashboard_service.live_punitive_capability(chat_id=chat_id)
                if not ready:
                    await control_repository.set_shadow(chat_id, True)
                    text, keyboard = await dashboard_service.settings_payload(chat_id=chat_id)
                    await dashboard_service.render(
                        admin_id=admin_id,
                        text=text,
                        keyboard=keyboard,
                        selected_chat_id=chat_id,
                        view="settings",
                    )
                    await safe_callback_answer(callback, reason, show_alert=True)
                    return

            shadow_enabled = await control_repository.set_shadow(chat_id, target_shadow)
            if not shadow_enabled and safety_circuit is not None:
                # Human explicitly re-enabled LIVE/DRY behavior after review.
                safety_circuit.reset_runtime_counters(chat_id)

            text, keyboard = await dashboard_service.settings_payload(chat_id=chat_id)
            await dashboard_service.render(
                admin_id=admin_id,
                text=text,
                keyboard=keyboard,
                selected_chat_id=chat_id,
                view="settings",
            )
            await safe_callback_answer(callback, "Shadow ON" if shadow_enabled else "LIVE enabled")
            return

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
            # Clearing transient Shadow cards is intentionally side-effect free for
            # persistent navigation.  The operator may currently be looking at the
            # renter dashboard, Platform OPS, or no persistent panel at all; none of
            # those views should be replaced just because old alert cards were
            # dismissed.
            await safe_callback_answer(callback, "Clearing shadow alerts...")
            artifacts = await control_repository.list_admin_alert_artifacts(
                managed_chat_id=chat_id,
                kind_prefix="shadow_alert",
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
                kind_prefix="shadow_alert",
            )
            logger.info(
                "SHADOW ALERTS CLEARED | chat=%s | by=%s | deleted=%s",
                chat_id,
                admin_id,
                deleted,
            )
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
            ticket_id = int(parts[2])

            ticket = await control_repository.get_ticket(ticket_id)
            if ticket is None:
                await safe_callback_answer(
                    callback,
                    "Review not found.",
                    show_alert=True,
                )
                return

            if not await can_access_chat(
                admin_id,
                ticket.chat_id,
                pilot_access_service,
            ):
                await safe_callback_answer(
                    callback,
                    "Not authorized for this community.",
                    show_alert=True,
                )
                return

            message_id = await dashboard_service.send_ticket_message(
                admin_id=admin_id,
                ticket_id=ticket_id,
            )
            if message_id is None:
                await safe_callback_answer(
                    callback,
                    "Could not open review.",
                    show_alert=True,
                )
                return

            await safe_callback_answer(callback, "Review opened")
            return

        elif action == "tclose":
            ticket_id = int(parts[2])
            ticket = await control_repository.get_ticket(ticket_id)
            if (
                ticket is not None
                and not await can_access_chat(
                    admin_id,
                    ticket.chat_id,
                    pilot_access_service,
                )
            ):
                await safe_callback_answer(
                    callback,
                    "Not authorized for this community.",
                    show_alert=True,
                )
                return

            await safe_callback_answer(callback, "Closed")
            await delete_transient_admin_card(
                callback=callback,
                repository=control_repository,
                admin_id=admin_id,
            )
            return

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

            if not is_test_ticket(ticket):
                claimed = await control_repository.claim_ticket_resolution(
                    ticket_id=ticket_id,
                    action=ticket_action,
                )
                if not claimed:
                    fresh_ticket = await control_repository.get_ticket(ticket_id)
                    if fresh_ticket is not None and callback.message is not None:
                        payload = await dashboard_service.ticket_payload(ticket_id=ticket_id)
                        if payload is not None:
                            resolved_text, resolved_keyboard, _ = payload
                            try:
                                await callback.message.edit_text(
                                    text=resolved_text,
                                    parse_mode="HTML",
                                    reply_markup=resolved_keyboard,
                                )
                            except Exception:
                                logger.debug(
                                    "Could not refresh already claimed ticket card.",
                                    exc_info=True,
                                )
                    return
                claimed_ticket_id = ticket_id
                claimed_ticket_action = ticket_action

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
                    claimed_action_committed = True
                except Exception as exc:
                    logger.warning(
                        "Ticket delete failed",
                        exc_info=True,
                    )
                    if claimed_ticket_id is not None and claimed_ticket_action is not None:
                        await control_repository.release_ticket_resolution_claim(
                            ticket_id=claimed_ticket_id,
                            action=claimed_ticket_action,
                        )
                        claimed_ticket_id = None
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
                claimed_action_committed = True

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
                ready, reason = await dashboard_service.live_punitive_capability(
                    chat_id=ticket.chat_id
                )
                if not ready:
                    if claimed_ticket_id is not None and claimed_ticket_action is not None:
                        await control_repository.release_ticket_resolution_claim(
                            ticket_id=claimed_ticket_id,
                            action=claimed_ticket_action,
                        )
                        claimed_ticket_id = None
                    await safe_callback_answer(callback, reason, show_alert=True)
                    return
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
                claimed_action_committed = True
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
                claimed_action_committed = True

            elif ticket_action == "allow":
                claimed_action_committed = True

            elif ticket_action != "allow":
                if claimed_ticket_id is not None and claimed_ticket_action is not None:
                    await control_repository.release_ticket_resolution_claim(
                        ticket_id=claimed_ticket_id,
                        action=claimed_ticket_action,
                    )
                    claimed_ticket_id = None
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
            claimed_ticket_id = None

            dashboard_service.request_refresh(ticket.chat_id)

            # One ticket can have renter + explicitly monitored platform mirrors.
            # Resolution is single-winner and removes every mirror together.
            await dashboard_service.cleanup_ticket_cards(
                ticket_id=ticket_id,
                managed_chat_id=ticket.chat_id,
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

        if claimed_ticket_id is not None and claimed_ticket_action is not None:
            try:
                if claimed_action_committed:
                    # Telegram action already happened. Finalize instead of
                    # reopening and risking a duplicate punishment.
                    recovered_ticket = await control_repository.get_ticket(claimed_ticket_id)
                    await control_repository.resolve_ticket(
                        ticket_id=claimed_ticket_id,
                        action=claimed_ticket_action,
                    )
                    if recovered_ticket is not None:
                        try:
                            await dashboard_service.cleanup_ticket_cards(
                                ticket_id=claimed_ticket_id,
                                managed_chat_id=recovered_ticket.chat_id,
                            )
                        except Exception:
                            logger.debug(
                                "Could not cleanup mirrored ticket cards during recovery | ticket=%s",
                                claimed_ticket_id,
                                exc_info=True,
                            )
                else:
                    await control_repository.release_ticket_resolution_claim(
                        ticket_id=claimed_ticket_id,
                        action=claimed_ticket_action,
                    )
            except Exception:
                logger.exception(
                    "Ticket claim recovery failed | ticket=%s",
                    claimed_ticket_id,
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

    # Optional external access-provider input hook.
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

    # Shadow feedback input is independent from the pinned dashboard. An admin
    # can receive a Shadow alert and teach ModGuard without opening the dashboard.
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
            try:
                resolved_shadow_title = await dashboard_service._chat_title(
                    int(pending_shadow.chat_id)
                )
            except Exception:
                logger.debug(
                    "Could not resolve pending Shadow feedback community title.",
                    exc_info=True,
                )
                resolved_shadow_title = None

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
                community_title_override=resolved_shadow_title,
            )
            preview_keyboard = shadow_feedback_keyboard(
                saved_case.id,
                stage="confirm",
                chat_id=saved_case.chat_id,
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
                    community_title_override=locals().get("resolved_shadow_title"),
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
                            chat_id=pending_shadow.chat_id,
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
                            chat_id=pending_shadow.chat_id,
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
