import asyncio
import html
import json
import logging
import os

import aiohttp

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.admin.control_repository import ControlRepository
from app.community_policy.core import (
    CORE_DEFAULT_ALLOWED_ITEMS,
    CORE_POLICY_ITEMS,
    CORE_POLICY_NOTE,
    DEFAULT_ENFORCEMENT_TIER_ITEMS,
)
from app.community_policy.service import parse_rules_json


logger = logging.getLogger(__name__)


def esc(value: object) -> str:
    return html.escape(str(value))


def compact(text: str, limit: int = 500) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def format_duration(minutes: int) -> str:
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


def format_hours_window(hours: int) -> str:
    hours = int(hours)
    if hours <= 0:
        return "never"
    if hours == 1:
        return "1 hour"
    if hours < 24:
        return f"{hours} hours"
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
    return f"{hours} hours"


def is_not_modified_error(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


def is_definitively_unavailable_chat_error(exc: Exception) -> bool:
    """Classify only definitive Telegram chat-removal responses as stale."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    if not isinstance(exc, TelegramBadRequest):
        return False
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "chat not found",
            "bot was kicked",
            "bot is not a member",
            "group chat was deleted",
            "supergroup chat was deleted",
        )
    )


def is_missing_edit_target(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "message to edit not found" in text
        or "message can't be edited" in text
        or "message_id_invalid" in text
    )


class DashboardService:
    def __init__(
        self,
        *,
        bot: Bot,
        repository: ControlRepository,
        admin_ids: list[int],
        global_dry_run: bool,
        live_delete_enabled: bool,
        raid_guard_available: bool = True,
        access_service=None,
    ):
        self.bot = bot
        self.repository = repository
        self.admin_ids = set(admin_ids)
        self.global_dry_run = global_dry_run
        self.live_delete_enabled = live_delete_enabled
        self.raid_guard_available = raid_guard_available
        self.access_service = access_service
        self._refresh_tasks: dict[int, asyncio.Task] = {}
        self._registry_reconciled = False
        self._registry_reconcile_lock = asyncio.Lock()

    async def reconcile_managed_chats(self, *, force: bool = False) -> int:
        """
        Repair basic-group migrations and hide definitively unavailable chats.
        Historical moderation data is retained.
        """
        if self._registry_reconciled and not force:
            return 0

        async with self._registry_reconcile_lock:
            if self._registry_reconciled and not force:
                return 0

            repaired = 0
            chats = await self.repository.list_managed_chats()

            async def repair_alias(chat, exc: TelegramMigrateToChat) -> None:
                nonlocal repaired
                new_id = int(exc.migrate_to_chat_id)
                title = chat.title
                try:
                    current = await self.bot.get_chat(new_id)
                    title = getattr(current, "title", None) or title
                except Exception:
                    logger.debug(
                        "Could not refresh migrated chat title | old=%s | new=%s",
                        chat.chat_id,
                        new_id,
                        exc_info=True,
                    )

                await self.repository.register_chat_migration(
                    old_chat_id=chat.chat_id,
                    new_chat_id=new_id,
                    chat_title=title,
                )
                repaired += 1
                logger.info(
                    "CHAT MIGRATION REPAIRED | old=%s | new=%s | title=%s",
                    chat.chat_id,
                    new_id,
                    title,
                )

            async def mark_unavailable(chat, reason: str) -> None:
                await self.repository.mark_chat_unavailable(
                    chat_id=chat.chat_id,
                    reason=reason,
                )
                logger.info(
                    "STALE MANAGED CHAT HIDDEN | chat=%s | title=%s | reason=%s",
                    chat.chat_id,
                    chat.title,
                    compact(reason, 160),
                )

            for chat in chats:
                if not str(chat.chat_id).startswith("-100"):
                    try:
                        await self.bot.get_chat_member_count(chat.chat_id)
                    except TelegramMigrateToChat as exc:
                        await repair_alias(chat, exc)
                        continue
                    except (TelegramForbiddenError, TelegramBadRequest) as exc:
                        if is_definitively_unavailable_chat_error(exc):
                            await mark_unavailable(chat, str(exc))
                            continue
                        logger.info(
                            "CHAT MIGRATION PROBE SKIPPED | chat=%s | reason=%s",
                            chat.chat_id,
                            compact(str(exc), 160),
                        )
                    except TelegramAPIError as exc:
                        logger.warning(
                            "CHAT MIGRATION PROBE FAILED | chat=%s | reason=%s",
                            chat.chat_id,
                            compact(str(exc), 160),
                        )

                try:
                    telegram_chat = await self.bot.get_chat(chat.chat_id)
                    title = getattr(telegram_chat, "title", None)

                    get_member = getattr(self.bot, "get_chat_member", None)
                    if callable(get_member):
                        member = await get_member(chat.chat_id, self.bot.id)
                        status = getattr(member, "status", "")
                        status = str(getattr(status, "value", status)).casefold()
                        if status in {"left", "kicked"}:
                            await mark_unavailable(
                                chat,
                                f"bot membership status={status}",
                            )
                            continue

                    await self.repository.mark_chat_available(
                        chat_id=chat.chat_id,
                    )
                    await self.repository.ensure_chat_settings(
                        chat_id=chat.chat_id,
                        chat_title=title or chat.title,
                    )
                except TelegramMigrateToChat as exc:
                    await repair_alias(chat, exc)
                except (TelegramForbiddenError, TelegramBadRequest) as exc:
                    if is_definitively_unavailable_chat_error(exc):
                        await mark_unavailable(chat, str(exc))
                    else:
                        logger.info(
                            "CHAT REGISTRY PROBE SKIPPED | chat=%s | reason=%s",
                            chat.chat_id,
                            compact(str(exc), 160),
                        )
                except TelegramAPIError as exc:
                    logger.warning(
                        "CHAT REGISTRY PROBE FAILED | chat=%s | reason=%s",
                        chat.chat_id,
                        compact(str(exc), 160),
                    )

            self._registry_reconciled = True
            return repaired

    async def _visible_chats(self, admin_id: int):
        chats = await self.repository.list_managed_chats()
        if self.access_service is None:
            return chats
        try:
            if await self.access_service.is_superadmin(admin_id):
                return chats
            allowed_ids = set(await self.access_service.list_allowed_chat_ids(admin_id))
            return [chat for chat in chats if chat.chat_id in allowed_ids]
        except Exception:
            logger.exception("Pilot chat visibility failed | admin=%s", admin_id)
            return []

    async def admin_can_access_chat(self, admin_id: int, chat_id: int) -> bool:
        if self.access_service is None:
            return True
        try:
            return bool(await self.access_service.can_access_chat(admin_id, chat_id))
        except Exception:
            logger.exception(
                "Pilot dashboard access failed | admin=%s | chat=%s",
                admin_id,
                chat_id,
            )
            return False

    async def _choose_chat(self, admin_id: int) -> int | None:
        await self.reconcile_managed_chats()
        state = await self.repository.get_dashboard_state(admin_id)
        chats = await self._visible_chats(admin_id)
        if not chats:
            return None
        chat_ids = {chat.chat_id for chat in chats}
        if state and state.selected_chat_id in chat_ids:
            return state.selected_chat_id
        return chats[0].chat_id

    async def _chat_title(self, chat_id: int) -> str:
        chats = await self.repository.list_managed_chats()
        for chat in chats:
            if chat.chat_id == chat_id:
                return chat.title
        return str(chat_id)

    async def dashboard_payload(self, *, chat_id: int):
        settings = await self.repository.get_chat_settings(chat_id)
        summary = await self.repository.get_activity_summary(chat_id=chat_id, hours=24)
        tickets = await self.repository.count_open_tickets(chat_id)
        title = await self._chat_title(chat_id)

        if settings.shadow_mode:
            mode = "SHADOW"
        elif not self.global_dry_run and self.live_delete_enabled:
            mode = "LIVE"
        else:
            mode = "DRY RUN"

        text = (
            "<b>🛡 MODGUARD</b>\n"
            f"{esc(title)} · <b>{mode}</b>\n\n"
            "<b>24h</b>\n"
            f"Deleted {summary.deleted} · Banned {summary.banned} · Muted {summary.muted} · Warned {summary.warned}\n"
            f"Tickets {tickets} · Reviews {summary.reviews}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"⚠️ Tickets · {tickets}", callback_data=f"mg:tickets:{chat_id}")],
                [
                    InlineKeyboardButton(text="📊 Activity", callback_data=f"mg:activity:{chat_id}:24"),
                    InlineKeyboardButton(text="⚙️ Settings", callback_data=f"mg:settings:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="🧪 Test mode", callback_data=f"mg:test:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Refresh", callback_data=f"mg:dash:{chat_id}"),
                    InlineKeyboardButton(text="💬 Change chat", callback_data="mg:chats"),
                ],
            ]
        )
        return text, keyboard

    async def settings_payload(self, *, chat_id: int):
        settings = await self.repository.get_chat_settings(chat_id)
        live_ban_enabled = await self.repository.get_live_ban_enabled(chat_id)
        mute_duration_minutes = await self.repository.get_mute_duration_minutes(chat_id)
        light_decay_hours = await self.repository.get_light_offense_decay_hours(chat_id)
        raid_guard_enabled = await self.repository.get_raid_guard_enabled(chat_id)
        title = await self._chat_title(chat_id)

        shadow = "ON" if settings.shadow_mode else "OFF"
        cleanup = "ON" if (not self.global_dry_run and self.live_delete_enabled) else "OFF"
        autoban = "ON" if live_ban_enabled else "OFF"
        mute_duration = format_duration(mute_duration_minutes)
        light_memory = format_hours_window(light_decay_hours)
        raid = (
            "UNAVAILABLE"
            if not self.raid_guard_available
            else ("ON" if raid_guard_enabled else "OFF")
        )

        text = (
            "<b>⚙️ SETTINGS</b>\n"
            f"{esc(title)}\n"
            "<i>Only everyday controls live here. Safety/diagnostics/history moved to Tools.</i>\n\n"
            f"Shadow         <b>{shadow}</b>\n"
            f"Live cleanup   <b>{cleanup}</b>\n"
            f"Auto-ban       <b>{autoban}</b>\n"
            f"Mute duration  <b>{esc(mute_duration)}</b>\n"
            f"Light memory   <b>{esc(light_memory)}</b>\n"
            f"Raid Guard     <b>{raid}</b>\n\n"
            "Light memory affects only minor spam/flood/harassment escalation. "
            "After the window expires, a new LIGHT offense starts from warning again. "
            "MEDIUM/HEAVY safety history does not decay."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🛡 Shadow: {shadow}",
                        callback_data=f"mg:shadow:{chat_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"🚫 Auto-ban: {autoban}",
                        callback_data=f"mg:ban_toggle:{chat_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"🔇 Mute: {mute_duration}",
                        callback_data=f"mg:mute_duration:{chat_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"🕒 Memory: {light_memory}",
                        callback_data=f"mg:light_memory:{chat_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"🚨 Raid: {raid}",
                        callback_data=f"mg:raid_toggle:{chat_id}",
                    ),
                    InlineKeyboardButton(
                        text="📜 Policy",
                        callback_data=f"mg:policy:{chat_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🧰 Safety & tools",
                        callback_data=f"mg:tools:{chat_id}",
                    )
                ],
                [InlineKeyboardButton(text="◀ Dashboard", callback_data=f"mg:dash:{chat_id}")],
            ]
        )
        return text, keyboard

    async def light_memory_payload(self, *, chat_id: int):
        title = await self._chat_title(chat_id)
        current = await self.repository.get_light_offense_decay_hours(chat_id)
        text = (
            "<b>🕒 LIGHT OFFENSE MEMORY</b>\n"
            f"{esc(title)}\n\n"
            f"Current window: <b>{esc(format_hours_window(current))}</b>\n\n"
            "Applies only to LIGHT reputation: spam, flood and targeted-harassment "
            "warning ladders. When this time passes, old minor offenses stop escalating "
            "a new incident. MEDIUM/HEAVY safety history stays intact.\n\n"
            "Recommended default: <b>6 hours</b>."
        )
        options = [1, 3, 6, 12, 24, 72, 168, 0]
        labels = {0: "Never expire"}
        rows = []
        for index in range(0, len(options), 2):
            row = []
            for hours in options[index:index + 2]:
                label = labels.get(hours, format_hours_window(hours))
                selected = " ✓" if hours == current else ""
                row.append(InlineKeyboardButton(
                    text=label + selected,
                    callback_data=f"mg:light_memory_set:{chat_id}:{hours}",
                ))
            rows.append(row)
        rows.append([InlineKeyboardButton(
            text="◀ Settings", callback_data=f"mg:settings:{chat_id}"
        )])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def tools_payload(self, *, chat_id: int):
        title = await self._chat_title(chat_id)
        immunity_count = len(await self.repository.list_moderation_immunity(chat_id=chat_id))
        latest_raid = await self.repository.latest_raid_incident(chat_id=chat_id)

        text = (
            "<b>🧰 SAFETY & TOOLS</b>\n"
            f"{esc(title)}\n\n"
            "Operational tools, safety controls and investigation views are kept "
            "separate from everyday settings.\n\n"
            f"Immunity entries: <b>{immunity_count}</b>\n"
            "Auto-stop: <b>ARMED</b> · abnormal action bursts or repeated failures force SHADOW."
        )

        rows = [
            [
                InlineKeyboardButton(text="⚖️ Safety policy", callback_data=f"mg:safety:{chat_id}"),
                InlineKeyboardButton(text="🩺 Diagnostics", callback_data=f"mg:diag:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text="🚫 Banned users", callback_data=f"mg:bans:{chat_id}"),
                InlineKeyboardButton(text="🧿 Immunity", callback_data=f"mg:immune:{chat_id}"),
            ],
            [InlineKeyboardButton(text="🧹 Clear shadow alerts", callback_data=f"mg:shadow_clear:{chat_id}")],
        ]
        if latest_raid is not None:
            rows.append([InlineKeyboardButton(
                text=f"🚨 Last raid · {latest_raid.incident_key}",
                callback_data=f"mg:raid_incident:{latest_raid.id}",
            )])
        rows.append([InlineKeyboardButton(text="◀ Settings", callback_data=f"mg:settings:{chat_id}")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def immunity_payload(self, *, chat_id: int, status: str | None = None):
        chat_id = await self.repository.resolve_chat_id(chat_id)
        title = await self._chat_title(chat_id)
        records = await self.repository.list_moderation_immunity(chat_id=chat_id)

        text = (
            "<b>🧿 IMMUNITY LIST</b>\n"
            f"{esc(title)}\n\n"
            "Users/bots in this list bypass ModGuard completely: no AI analysis, "
            "spam/flood checks, tickets, delete, mute or ban.\n"
            "Use this mainly for trusted service bots and integrations."
        )
        if status:
            text += f"\n\n<b>{esc(status)}</b>"

        rows = []
        if records:
            text += "\n\n<b>Protected accounts</b>"
            for record in records[:30]:
                if record.user_id is not None:
                    label = f"ID {record.user_id}"
                    if record.username:
                        label += f" · @{record.username}"
                else:
                    label = f"@{record.username}"
                rows.append([
                    InlineKeyboardButton(
                        text=f"❌ {label[:48]}",
                        callback_data=f"mg:immune_del:{chat_id}:{record.id}",
                    )
                ])
        else:
            text += "\n\n<i>No immunity entries.</i>"

        rows.append([
            InlineKeyboardButton(
                text="➕ Add by @username / ID",
                callback_data=f"mg:immune_add:{chat_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="◀ Tools",
                callback_data=f"mg:tools:{chat_id}",
            )
        ])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def immunity_input_payload(self, *, chat_id: int, error: str | None = None):
        title = await self._chat_title(chat_id)
        text = (
            "<b>➕ ADD IMMUNITY</b>\n"
            f"{esc(title)}\n\n"
            "Send one Telegram user/bot as either:\n"
            "• numeric user ID, for example <code>123456789</code>\n"
            "• @username, for example <code>@service_bot</code>\n\n"
            "The account will become fully immune from ModGuard in this chat."
        )
        if error:
            text += f"\n\n<b>Error</b>\n{esc(compact(error, 300))}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=f"mg:immune:{chat_id}",
                )
            ]]
        )
        return text, keyboard

    async def diagnostics_payload(self, *, chat_id: int):
        """Run lightweight live probes so an admin can see which dependency is broken."""
        repaired = await self.reconcile_managed_chats(force=True)
        chat_id = await self.repository.resolve_chat_id(chat_id)
        title = await self._chat_title(chat_id)

        telegram_status = "❌ unavailable"
        chat_status = "❌ unavailable"
        permissions_status = "⚠️ unknown"
        db_status = "❌ unavailable"
        ollama_status = "❌ unavailable"
        fast_status = "⚠️ unknown"
        deep_status = "⚠️ unknown"
        embed_status = "⚠️ unknown"

        try:
            me = await self.bot.get_me()
            telegram_status = f"✅ API reachable · @{getattr(me, 'username', '') or me.id}"
            tg_chat = await self.bot.get_chat(chat_id)
            tg_type = str(getattr(getattr(tg_chat, 'type', ''), 'value', getattr(tg_chat, 'type', '')))
            chat_status = f"✅ {tg_type or 'chat'} · {chat_id}"
            member = await self.bot.get_chat_member(chat_id, me.id)
            status = str(getattr(getattr(member, 'status', ''), 'value', getattr(member, 'status', '')))
            if status in {"administrator", "creator"}:
                can_delete = bool(getattr(member, 'can_delete_messages', False))
                can_restrict = bool(getattr(member, 'can_restrict_members', False))
                permissions_status = (
                    "✅ admin · delete=" + ("yes" if can_delete else "no")
                    + " · restrict/ban=" + ("yes" if can_restrict else "no")
                )
            else:
                permissions_status = f"❌ bot status={status or 'unknown'}"
        except TelegramMigrateToChat as exc:
            new_id = int(exc.migrate_to_chat_id)
            await self.repository.register_chat_migration(
                old_chat_id=chat_id, new_chat_id=new_id, chat_title=title
            )
            chat_status = f"⚠️ migrated while checking · {chat_id} → {new_id}"
        except Exception as exc:
            telegram_status = f"❌ {type(exc).__name__}: {compact(str(exc), 120)}"

        try:
            await self.repository.healthcheck()
            db_status = "✅ reachable"
        except Exception as exc:
            db_status = f"❌ {type(exc).__name__}: {compact(str(exc), 120)}"

        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        fast_model = os.getenv("OLLAMA_FAST_MODEL", "qwen3:1.7b")
        deep_model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
        embed_model = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{base_url}/api/tags") as response:
                    data = await response.json()
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    names = {str(item.get('name', '')) for item in data.get('models', [])}
                    ollama_status = "✅ API reachable"

                async def probe_structured(model: str) -> str:
                    if model not in names:
                        return f"❌ missing · {model}"
                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": "Return {\"ok\":true} only."}],
                        "stream": False,
                        "think": False,
                        "format": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                        },
                        "options": {"temperature": 0, "num_ctx": 128, "num_predict": 24},
                    }
                    try:
                        async with session.post(f"{base_url}/api/chat", json=payload) as response:
                            data = await response.json()
                            if response.status != 200:
                                return f"❌ HTTP {response.status}"
                            content = data.get("message", {}).get("content", "")
                            parsed = json.loads(content)
                            if parsed.get("ok") is True:
                                return f"✅ structured JSON · {model}"
                            return f"❌ invalid structured JSON · {model}"
                    except Exception as exc:
                        return f"❌ {type(exc).__name__}: {compact(str(exc), 90)}"

                async def probe_embedding(model: str) -> str:
                    if model not in names:
                        return f"❌ missing · {model}"
                    try:
                        async with session.post(
                            f"{base_url}/api/embed",
                            json={"model": model, "input": "health check"},
                        ) as response:
                            data = await response.json()
                            vectors = data.get("embeddings") or []
                            if response.status == 200 and vectors and vectors[0]:
                                return f"✅ vector OK · {model}"
                            return f"❌ invalid embedding response · {model}"
                    except Exception as exc:
                        return f"❌ {type(exc).__name__}: {compact(str(exc), 90)}"

                fast_status, deep_status, embed_status = await asyncio.gather(
                    probe_structured(fast_model),
                    probe_structured(deep_model),
                    probe_embedding(embed_model),
                )
        except Exception as exc:
            ollama_status = f"❌ {type(exc).__name__}: {compact(str(exc), 120)}"

        aliases = await self.repository.count_chat_migrations()
        registry_status = f"✅ canonical · aliases repaired={aliases}"
        if repaired:
            registry_status += f" · fixed now={repaired}"

        text = (
            "<b>🩺 SYSTEM DIAGNOSTICS</b>\n"
            f"{esc(title)}\n\n"
            f"<b>Telegram</b>  {esc(telegram_status)}\n"
            f"<b>Chat</b>      {esc(chat_status)}\n"
            f"<b>Bot rights</b> {esc(permissions_status)}\n"
            f"<b>Database</b>  {esc(db_status)}\n"
            f"<b>Ollama</b>    {esc(ollama_status)}\n"
            f"<b>Fast LLM</b>  {esc(fast_status)}\n"
            f"<b>Deep LLM</b>  {esc(deep_status)}\n"
            f"<b>Embeddings</b> {esc(embed_status)}\n"
            f"<b>Chat registry</b> {esc(registry_status)}\n\n"
            "<i>Operation-specific conditions such as an already deleted message "
            "or a user who already left are not global outages; ModGuard handles "
            "them at action time.</i>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Run again", callback_data=f"mg:diag:{chat_id}")],
            [InlineKeyboardButton(text="◀ Tools", callback_data=f"mg:tools:{chat_id}")],
        ])
        return text, keyboard, chat_id

    async def mute_duration_payload(self, *, chat_id: int):
        title = await self._chat_title(chat_id)
        current = await self.repository.get_mute_duration_minutes(chat_id)
        text = (
            "<b>🔇 MUTE DURATION</b>\n"
            f"{esc(title)}\n"
            "<i>Applies only to this selected chat.</i>\n\n"
            f"Current duration: <b>{esc(format_duration(current))}</b>\n\n"
            "All automatic and manual mutes use this single duration.\n"
            "Default: <b>1 hour</b>."
        )
        options = [15, 30, 60, 180, 360, 720, 1440]
        rows = []
        for index in range(0, len(options), 3):
            row = []
            for minutes in options[index:index + 3]:
                selected = " ✓" if minutes == current else ""
                row.append(InlineKeyboardButton(
                    text=format_duration(minutes) + selected,
                    callback_data=f"mg:mute_set:{chat_id}:{minutes}",
                ))
            rows.append(row)
        rows.append([InlineKeyboardButton(
            text="◀ Settings", callback_data=f"mg:settings:{chat_id}"
        )])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def policy_payload(
        self,
        *,
        chat_id: int,
    ):
        title = await self._chat_title(chat_id)
        active = await self.repository.get_active_community_policy(
            chat_id
        )

        core_lines = "\n".join(
            f"✓ {esc(item)}"
            for item in CORE_POLICY_ITEMS
        )

        allowed_lines = "\n".join(
            f"• {esc(item)}"
            for item in CORE_DEFAULT_ALLOWED_ITEMS
        )

        tier_lines = "\n".join(
            f"• {esc(item)}"
            for item in DEFAULT_ENFORCEMENT_TIER_ITEMS
        )

        rules = parse_rules_json(
            active.rules_json
            if active is not None
            else "[]"
        )

        if active is None or not rules:
            custom = (
                "<b>Custom overlay</b> · none\n"
                "No custom rules yet."
            )
        else:
            custom_lines = []
            action_labels = {
                "allow": "ALLOW",
                "warn": "WARN",
                "delete": "DELETE",
                "mute": "MUTE",
                "ban": "BAN",
                "escalate": "REVIEW",
            }

            for rule in rules[:6]:
                suffix = (f" · {rule.enforcement_tier.upper()}" if rule.enforcement_tier else "")

                custom_lines.append(
                    f"<b>{esc(rule.rule_id)} · "
                    f"{action_labels.get(rule.action, rule.action.upper())}"
                    f"{suffix}</b>\n"
                    f"{esc(compact(rule.condition, 180))}"
                )

            if len(rules) > 6:
                custom_lines.append(
                    f"… {len(rules) - 6} more rules"
                )

            custom = (
                f"<b>Custom overlay · v{active.version}</b>\n"
                + "\n\n".join(custom_lines)
            )

        text = (
            "<b>📜 COMMUNITY POLICY</b>\n"
            f"{esc(title)}\n"
            "<i>Custom rules apply only to this selected chat.</i>\n\n"
            "<b>PROTECTED CORE · CANNOT BE WEAKENED</b>\n"
            f"{core_lines}\n\n"
            "<b>DEFAULT ENFORCEMENT · CUSTOMIZABLE</b>\n"
            f"{tier_lines}\n\n"
            "<b>DEFAULT · ALLOWED</b>\n"
            f"{allowed_lines}\n\n"
            f"{custom}\n\n"
            f"<i>{esc(CORE_POLICY_NOTE)}</i>"
        )

        rows = [
            [
                InlineKeyboardButton(
                    text="✏️ Add / change rules",
                    callback_data=f"mg:policy_edit:{chat_id}",
                )
            ]
        ]

        if active is not None:
            previous = await self.repository.get_previous_community_policy(
                chat_id,
                before_version=active.version,
            )

            if previous is not None:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="↩ Previous version",
                            callback_data=f"mg:policy_prev:{chat_id}",
                        )
                    ]
                )

            if rules:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="🧹 Clear custom rules",
                            callback_data=f"mg:policy_clear:{chat_id}",
                        )
                    ]
                )

        rows.append(
            [
                InlineKeyboardButton(
                    text="◀ Settings",
                    callback_data=f"mg:settings:{chat_id}",
                )
            ]
        )

        return text, InlineKeyboardMarkup(
            inline_keyboard=rows
        )

    async def policy_input_payload(
        self,
        *,
        chat_id: int,
        error: str | None = None,
    ):
        title = await self._chat_title(chat_id)

        text = (
            "<b>✏️ CHANGE COMMUNITY POLICY</b>\n"
            f"{esc(title)}\n\n"
            "Send the rules as a normal message. ModGuard will interpret the meaning "
            "and build a preview. Nothing is applied until you press Apply.\n\n"
            "<b>Examples</b>\n"
            "• Allow ordinary spam in this crypto chat.\n"
            "• Treat profanity as a light offense.\n"
            "• Treat unsolicited DM advertising as medium.\n"
            "• Treat casino investment scams as heavy.\n"
            "• Delete external links, but allow GitHub.\n\n"
            "Protected scam/phishing/malicious-link/threat safety cannot be disabled. "
            "Mute duration always comes from this chat's Settings."
        )

        if error:
            text += (
                "\n\n<b>Error</b>\n"
                + esc(compact(error, 500))
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data=f"mg:policy_cancel:{chat_id}",
                    )
                ]
            ]
        )

        return text, keyboard

    async def policy_compiling_payload(
        self,
        *,
        chat_id: int,
    ):
        title = await self._chat_title(chat_id)
        return (
            "<b>🧠 POLICY COMPILER</b>\n"
            f"{esc(title)}\n\n"
            "Deep AI is compiling semantic rules and checking them against "
            "the Core policy…",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )

    async def policy_preview_payload(
        self,
        *,
        admin_id: int,
    ):
        draft = await self.repository.get_community_policy_draft(
            admin_id
        )
        if draft is None:
            return None

        title = await self._chat_title(draft.chat_id)
        rules = parse_rules_json(draft.rules_json)

        try:
            changes = json.loads(draft.changes_json or "[]")
        except Exception:
            changes = []

        try:
            ignored = json.loads(draft.ignored_json or "[]")
        except Exception:
            ignored = []

        action_labels = {
            "allow": "ALLOW",
            "warn": "WARN",
            "delete": "DELETE",
            "mute": "MUTE",
            "ban": "BAN",
            "escalate": "REVIEW",
        }

        rule_lines = []
        for rule in rules[:6]:
            suffix = (f" · {rule.enforcement_tier.upper()}" if rule.enforcement_tier else "")

            rule_lines.append(
                f"<b>{esc(rule.rule_id)} · "
                f"{action_labels.get(rule.action, rule.action.upper())}"
                f"{suffix}</b>\n"
                f"{esc(compact(rule.condition, 180))}"
            )

        if not rule_lines:
            rule_lines.append(
                "The custom overlay will be empty; only the Core policy will remain."
            )

        text = (
            "<b>📜 POLICY PREVIEW</b>\n"
            f"{esc(title)}\n\n"
            f"{esc(compact(draft.summary, 600))}\n\n"
            + "\n\n".join(rule_lines)
        )

        if changes:
            text += (
                "\n\n<b>Changes</b>\n"
                + "\n".join(
                    f"• {esc(compact(item, 220))}"
                    for item in changes[:5]
                )
            )

        if ignored:
            text += (
                "\n\n<b>Core protection kept</b>\n"
                + "\n".join(
                    f"• {esc(compact(item, 220))}"
                    for item in ignored[:4]
                )
            )

        text += (
            "\n\n<i>Apply activates only the custom overlay. "
            "ModGuard Core remains unchanged.</i>"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Apply",
                        callback_data=f"mg:policy_apply:{draft.chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Change",
                        callback_data=f"mg:policy_change:{draft.chat_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data=f"mg:policy_cancel:{draft.chat_id}",
                    ),
                ],
            ]
        )

        return text, keyboard, draft.chat_id

    async def policy_clear_confirm_payload(
        self,
        *,
        chat_id: int,
    ):
        title = await self._chat_title(chat_id)
        text = (
            "<b>🧹 CLEAR CUSTOM RULES?</b>\n"
            f"{esc(title)}\n\n"
            "Only the custom overlay will be cleared.\n"
            "Core scam/phishing/spam protection will remain active."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Clear custom",
                        callback_data=f"mg:policy_clear_apply:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀ Back",
                        callback_data=f"mg:policy:{chat_id}",
                    )
                ],
            ]
        )
        return text, keyboard

    async def safety_policy_payload(
        self,
        *,
        chat_id: int,
    ):
        title = await self._chat_title(chat_id)

        text = (
            "<b>⚖️ SAFETY / ENFORCEMENT POLICY</b>\n"
            f"{esc(title)}\n\n"
            "<b>LIGHT · configurable</b>\n"
            "Ordinary spam/flood.\n"
            "→ WARN (message stays)\n"
            "→ repeat inside Light-memory window: MUTE + DELETE\n"
            "→ repeat after mute inside the window: BAN + DELETE\n"
            "→ after Light memory expires: ladder starts from WARN again\n\n"
            "<b>HUMAN CONFLICT · conservative Core</b>\n"
            "Clear first targeted harassment → WARN (message stays).\n"
            "Repeat, mutual fight, unclear instigator, or uncertainty → moderator Ticket.\n"
            "Default Core does not auto-mute/ban ordinary fights.\n\n"
            "<b>MEDIUM · configurable</b>\n"
            "Clear intrusive unsolicited DM/commercial solicitation and similar behavior.\n"
            "→ MUTE + DELETE\n"
            "→ repeat after mute: BAN + DELETE\n\n"
            "<b>HEAVY · Protected Core</b>\n"
            "✓ Scam / fraud\n"
            "✓ Phishing / credential theft\n"
            "✓ Malicious links\n"
            "✓ Credible direct threats\n"
            "→ high confidence: BAN + DELETE immediately\n\n"
            "If a fight/harassment case is genuinely ambiguous, ModGuard creates a "
            "review Ticket with conversation context instead of guessing.\n\n"
            "<b>Community Policy</b> may relax or strengthen LIGHT/MEDIUM community "
            "behavior (for example, allow ordinary spam in a crypto chat), but it "
            "cannot weaken Protected Core threats.\n\n"
            "<b>Automatic safety circuit</b>\n"
            "Abnormal destructive-action bursts or repeated execution/pipeline failures "
            "automatically force this chat into SHADOW until a human reviews it.\n\n"
            "<i>Auto-ban remains the per-chat kill switch for real autonomous bans. "
            "Raid Guard separately handles confirmed multi-user hostile campaigns.</i>"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="◀ Tools", callback_data=f"mg:tools:{chat_id}"
            )]]
        )
        return text, keyboard

    async def raid_incident_payload(
        self,
        *,
        incident_id: int,
    ):
        incident = await self.repository.get_raid_incident(
            incident_id
        )
        if incident is None:
            return None

        try:
            affected_users = json.loads(
                incident.affected_user_ids_json or "[]"
            )
        except Exception:
            affected_users = []

        try:
            unbanned_users = json.loads(
                incident.unbanned_user_ids_json or "[]"
            )
        except Exception:
            unbanned_users = []

        mode = "SHADOW" if incident.shadow_mode else "LIVE"
        text = (
            "<b>🚨 RAID INCIDENT</b>\n"
            f"{esc(incident.incident_key)} · <b>{mode}</b>\n\n"
            f"Campaign <code>{esc(incident.cluster_key)}</code>\n"
            f"Similarity   <b>{incident.similarity:.0%}</b>\n"
            f"Messages     <b>{incident.message_count}</b>\n"
            f"Unique users <b>{incident.unique_users}</b>\n"
            f"Deleted      <b>{incident.deleted_messages}</b>\n"
            f"Banned       <b>{incident.banned_users}</b>\n"
            f"Unbanned     <b>{len(unbanned_users)}</b>\n\n"
            "Deleted Telegram messages cannot be restored. Bans can be rolled back."
        )

        rows = []
        if incident.banned_users > len(unbanned_users):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="↩ Unban users",
                        callback_data=f"mg:raid_unban:{incident.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="◀ Tools",
                    callback_data=f"mg:tools:{incident.chat_id}",
                )
            ]
        )
        return (
            text,
            InlineKeyboardMarkup(inline_keyboard=rows),
            incident.chat_id,
        )

    async def bans_payload(
        self,
        *,
        chat_id: int,
        page: int = 0,
    ):
        title = await self._chat_title(chat_id)
        page_size = 10
        max_pages = 10
        total = await self.repository.count_active_moderation_bans(chat_id=chat_id)
        visible_total = min(total, page_size * max_pages)
        page_count = max(1, (visible_total + page_size - 1) // page_size)
        page = max(0, min(int(page), page_count - 1))

        bans = await self.repository.list_active_moderation_bans(
            chat_id=chat_id,
            limit=page_size,
            offset=page * page_size,
        )

        text = (
            "<b>🚫 BANNED USERS</b>\n"
            f"{esc(title)}\n\n"
            + (
                f"Active ModGuard bans: <b>{total}</b>\n"
                f"Page <b>{page + 1}/{page_count}</b> · newest first.\n"
                "Choose a user to unban or search by @username / user ID."
                if total
                else "No active ModGuard bans."
            )
        )

        rows = []
        for record in bans:
            who = record.username or f"user {record.user_id}"
            label = f"↩ {who} · {record.category or record.source}"[:60]
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"mg:unban:{record.id}",
                )
            ])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀ Newer",
                callback_data=f"mg:bans:{chat_id}:{page - 1}",
            ))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton(
                text="Older ▶",
                callback_data=f"mg:bans:{chat_id}:{page + 1}",
            ))
        if nav:
            rows.append(nav)

        rows.append([
            InlineKeyboardButton(
                text="🔎 Search ban",
                callback_data=f"mg:bans_search:{chat_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="◀ Tools",
                callback_data=f"mg:tools:{chat_id}",
            )
        ])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def bans_search_prompt_payload(self, *, chat_id: int):
        title = await self._chat_title(chat_id)
        text = (
            "<b>🔎 SEARCH BANNED USERS</b>\n"
            f"{esc(title)}\n\n"
            "Send an <b>@username</b>, username fragment, or numeric Telegram user ID.\n"
            "The search is limited to active ModGuard bans in this chat."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="◀ Banned users",
                callback_data=f"mg:bans:{chat_id}:0",
            )]
        ])
        return text, keyboard

    async def bans_search_results_payload(
        self,
        *,
        chat_id: int,
        query: str,
    ):
        title = await self._chat_title(chat_id)
        results = await self.repository.search_active_moderation_bans(
            chat_id=chat_id,
            query=query,
            limit=10,
        )
        text = (
            "<b>🔎 BAN SEARCH</b>\n"
            f"{esc(title)}\n"
            f"Query: <code>{esc(query.strip())}</code>\n\n"
            + (
                f"Found <b>{len(results)}</b> active ban(s)."
                if results
                else "No active bans matched this query."
            )
        )
        rows = []
        for record in results:
            who = record.username or f"user {record.user_id}"
            rows.append([InlineKeyboardButton(
                text=f"↩ {who} · {record.category or record.source}"[:60],
                callback_data=f"mg:unban:{record.id}",
            )])
        rows.append([InlineKeyboardButton(
            text="🔎 Search again",
            callback_data=f"mg:bans_search:{chat_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="◀ Banned users",
            callback_data=f"mg:bans:{chat_id}:0",
        )])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def activity_payload(self, *, chat_id: int, hours: int):
        summary = await self.repository.get_activity_summary(chat_id=chat_id, hours=hours)
        title = await self._chat_title(chat_id)
        label = f"{hours}h" if hours < 168 else "7d"

        text = (
            "<b>📊 ACTIVITY</b>\n"
            f"{esc(title)} · {label}\n\n"
            f"Processed   <b>{summary.total}</b>\n"
            f"Deleted     <b>{summary.deleted}</b>\n"
            f"Banned      <b>{summary.banned}</b>\n"
            f"Muted       <b>{summary.muted}</b>\n"
            f"Warned      <b>{summary.warned}</b>\n"
            f"Reviews     <b>{summary.reviews}</b>\n"
            f"Delete fail <b>{summary.failed_deletes}</b>"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="6h", callback_data=f"mg:activity:{chat_id}:6"),
                    InlineKeyboardButton(text="24h", callback_data=f"mg:activity:{chat_id}:24"),
                    InlineKeyboardButton(text="7d", callback_data=f"mg:activity:{chat_id}:168"),
                ],
                [InlineKeyboardButton(text="◀ Dashboard", callback_data=f"mg:dash:{chat_id}")],
            ]
        )
        return text, keyboard

    async def tickets_payload(self, *, chat_id: int):
        tickets = await self.repository.list_open_tickets(chat_id=chat_id, limit=20)
        title = await self._chat_title(chat_id)
        text = (
            "<b>⚠️ TICKETS</b>\n"
            f"{esc(title)}\n\n"
            + (f"Open: <b>{len(tickets)}</b>\nChoose a case to review." if tickets else "No open tickets.")
        )

        rows = []
        for ticket in tickets:
            who = ticket.username or (f"user {ticket.target_user_id}" if ticket.target_user_id else "unknown")
            suffix = f" ×{ticket.occurrence_count}" if ticket.occurrence_count > 1 else ""
            rows.append([
                InlineKeyboardButton(
                    text=f"{who} · {ticket.category}{suffix}"[:60],
                    callback_data=f"mg:ticket:{ticket.id}",
                )
            ])
        rows.append([InlineKeyboardButton(text="◀ Dashboard", callback_data=f"mg:dash:{chat_id}")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def ticket_payload(self, *, ticket_id: int):
        ticket = await self.repository.get_ticket(ticket_id)
        if ticket is None:
            return None

        who = ticket.username or (f"user {ticket.target_user_id}" if ticket.target_user_id else "unknown")
        confidence = f"{ticket.confidence:.0%}" if ticket.confidence is not None else "—"

        text = (
            f"<b>⚠️ {esc(ticket.ticket_key)}</b>\n"
            f"{esc(who)} · {esc(ticket.category)} · {confidence}\n\n"
            f"<b>Message</b>\n<code>{esc(compact(ticket.message_text))}</code>\n\n"
            f"<b>Why review</b>\n{esc(compact(ticket.reason, 350))}"
        )
        try:
            context_payload = json.loads(ticket.context_json or "{}")
        except Exception:
            context_payload = {}

        chat_context = context_payload.get("recent_chat_messages") or []
        if chat_context:
            lines = []
            for item in chat_context[-8:]:
                who = item.get("username") or item.get("name") or (
                    f"user {item.get('user_id')}" if item.get("user_id") else "member"
                )
                msg = compact(str(item.get("text") or ""), 120)
                if msg:
                    lines.append(f"{esc(who)}: {esc(msg)}")
            if lines:
                text += "\n\n<b>Conversation context</b>\n" + "\n".join(lines)

        if ticket.occurrence_count > 1:
            text += f"\n\nSeen in this ticket: <b>{ticket.occurrence_count}</b>"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚫 Ban", callback_data=f"mg:tact:{ticket.id}:ban"),
                    InlineKeyboardButton(text="🔇 Mute", callback_data=f"mg:tact:{ticket.id}:mute"),
                ],
                [
                    InlineKeyboardButton(text="⚠️ Warn", callback_data=f"mg:tact:{ticket.id}:warn"),
                    InlineKeyboardButton(text="🗑 Delete", callback_data=f"mg:tact:{ticket.id}:delete"),
                ],
                [
                    InlineKeyboardButton(text="✅ Allow", callback_data=f"mg:tact:{ticket.id}:allow"),
                ],
                [InlineKeyboardButton(text="◀ Tickets", callback_data=f"mg:tickets:{ticket.chat_id}")],
            ]
        )
        return text, keyboard, ticket.chat_id

    async def chats_payload(self, *, admin_id: int):
        await self.reconcile_managed_chats(force=True)
        chats = await self._visible_chats(admin_id)
        rows = [
            [InlineKeyboardButton(text=chat.title[:60], callback_data=f"mg:dash:{chat.chat_id}")]
            for chat in chats
        ]
        return "<b>💬 CHATS</b>\n\nChoose a community.", InlineKeyboardMarkup(inline_keyboard=rows)

    async def render(
        self,
        *,
        admin_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup,
        selected_chat_id: int | None,
        view: str,
    ) -> None:
        state = await self.repository.get_dashboard_state(admin_id)

        if state and state.dashboard_message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=state.dashboard_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                await self.repository.save_dashboard_state(
                    admin_id=admin_id,
                    selected_chat_id=(
                        selected_chat_id
                        if selected_chat_id is not None
                        else state.selected_chat_id
                    ),
                    view=view,
                )
                return

            except TelegramBadRequest as exc:
                # Refreshing unchanged dashboard is normal, not a reason to
                # create and pin a new dashboard.
                if is_not_modified_error(exc):
                    await self.repository.save_dashboard_state(
                        admin_id=admin_id,
                        selected_chat_id=(
                            selected_chat_id
                            if selected_chat_id is not None
                            else state.selected_chat_id
                        ),
                        view=view,
                    )
                    return

                # Only a genuinely missing/uneditable target justifies a new one.
                if not is_missing_edit_target(exc):
                    logger.warning("Dashboard edit rejected; keeping existing dashboard: %s", exc)
                    return

            except TelegramAPIError:
                # Transient Telegram error: never turn it into dashboard spam.
                logger.warning("Dashboard edit failed transiently; keeping existing dashboard.", exc_info=True)
                return

        # Fresh dashboard only when there is no state or old target is genuinely gone.
        message = await self.bot.send_message(
            chat_id=admin_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        await self.repository.save_dashboard_state(
            admin_id=admin_id,
            dashboard_message_id=message.message_id,
            selected_chat_id=selected_chat_id,
            view=view,
        )

        try:
            await self.bot.pin_chat_message(
                chat_id=admin_id,
                message_id=message.message_id,
                disable_notification=True,
            )
        except TelegramAPIError:
            logger.debug("Could not pin private dashboard.", exc_info=True)

    async def open_dashboard(self, *, admin_id: int, chat_id: int | None = None) -> None:
        if chat_id is None:
            chat_id = await self._choose_chat(admin_id)
        elif not await self.admin_can_access_chat(admin_id, chat_id):
            chat_id = await self._choose_chat(admin_id)

        if chat_id is None:
            await self.render(
                admin_id=admin_id,
                text="<b>🛡 MODGUARD</b>\n\nNo managed groups found yet.",
                keyboard=InlineKeyboardMarkup(inline_keyboard=[]),
                selected_chat_id=None,
                view="dashboard",
            )
            return

        text, keyboard = await self.dashboard_payload(chat_id=chat_id)
        await self.render(
            admin_id=admin_id,
            text=text,
            keyboard=keyboard,
            selected_chat_id=chat_id,
            view="dashboard",
        )

    def request_refresh(self, chat_id: int) -> None:
        task = self._refresh_tasks.get(chat_id)
        if task is not None and not task.done():
            return
        self._refresh_tasks[chat_id] = asyncio.create_task(
            self._debounced_refresh(chat_id)
        )

    async def _debounced_refresh(self, chat_id: int) -> None:
        await asyncio.sleep(1.5)
        states = await self.repository.dashboard_states_for_chat(
            chat_id=chat_id, view="dashboard"
        )
        if not states:
            return

        text, keyboard = await self.dashboard_payload(chat_id=chat_id)

        for state in states:
            if state.dashboard_message_id is None:
                continue
            if not await self.admin_can_access_chat(state.admin_id, chat_id):
                continue
            try:
                await self.bot.edit_message_text(
                    chat_id=state.admin_id,
                    message_id=state.dashboard_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as exc:
                if is_not_modified_error(exc):
                    continue
                logger.debug("Dashboard background edit rejected: %s", exc)
            except TelegramAPIError:
                logger.debug("Dashboard background refresh skipped.", exc_info=True)
