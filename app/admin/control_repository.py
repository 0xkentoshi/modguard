import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.control_models import (
    AdminAlertArtifactRecord,
    AdminDashboardStateRecord,
    ChatControlSettingsRecord,
    ChatEnforcementSettingsRecord,
    ChatMuteSettingsRecord,
    ChatMigrationRecord,
    ChatRaidSettingsRecord,
    CommunityPolicyDraftRecord,
    CommunityPolicyVersionRecord,
    KnownModerationPatternRecord,
    ModerationTicketRecord,
    ModeratorFeedbackRecord,
    ModerationBanRecord,
    RaidIncidentRecord,
    SemanticObservationRecord,
    TestArtifactRecord,
)
from app.database.models import MessageRecord, ModerationEventRecord


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def message_fingerprint(normalized_text: str) -> str:
    canonical = normalized_text.strip().casefold()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ManagedChat:
    chat_id: int
    title: str


@dataclass
class ActivitySummary:
    hours: int
    total: int
    deleted: int
    banned: int
    muted: int
    warned: int
    reviews: int
    failed_deletes: int


class ControlRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def healthcheck(self) -> bool:
        async with self.session_factory() as session:
            await session.execute(select(1))
        return True

    async def resolve_chat_id(self, chat_id: int) -> int:
        current = int(chat_id)
        seen: set[int] = set()
        async with self.session_factory() as session:
            for _ in range(12):
                if current in seen:
                    break
                seen.add(current)
                record = await session.get(ChatMigrationRecord, current)
                if record is None:
                    break
                current = int(record.new_chat_id)
        return current

    async def count_chat_migrations(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(select(func.count(ChatMigrationRecord.old_chat_id)))
            return int(result.scalar_one() or 0)

    async def register_chat_migration(
        self,
        *,
        old_chat_id: int,
        new_chat_id: int,
        chat_title: str | None = None,
    ) -> int:
        """Merge an obsolete Telegram basic-group id into its supergroup id.

        This is idempotent. Operational records that must keep working (settings,
        tickets, bans, policy, feedback, raid/test/admin artifacts) follow the new
        canonical id. Raw stored message/audit history is intentionally left on
        the historical id; Change-chat hides aliases so it never becomes a second
        community.
        """
        old_chat_id = int(old_chat_id)
        new_chat_id = int(new_chat_id)
        if old_chat_id == new_chat_id:
            return new_chat_id

        # Flatten a possible migration chain first.
        new_chat_id = await self.resolve_chat_id(new_chat_id)

        async with self.session_factory() as session:
            async with session.begin():
                alias = await session.get(ChatMigrationRecord, old_chat_id)
                if alias is None:
                    alias = ChatMigrationRecord(
                        old_chat_id=old_chat_id,
                        new_chat_id=new_chat_id,
                        chat_title=chat_title,
                    )
                    session.add(alias)
                else:
                    alias.new_chat_id = new_chat_id
                    if chat_title:
                        alias.chat_title = chat_title
                    alias.updated_at = utcnow()

                # Any older aliases in a chain should point directly at canonical.
                await session.execute(
                    update(ChatMigrationRecord)
                    .where(ChatMigrationRecord.new_chat_id == old_chat_id)
                    .values(new_chat_id=new_chat_id, updated_at=utcnow())
                )

                async def merge_pk(
                    model,
                    fields: tuple[str, ...],
                    defaults: dict[str, object],
                ):
                    old = await session.get(model, old_chat_id)
                    current = await session.get(model, new_chat_id)
                    if old is None:
                        return current
                    old_stamp = getattr(old, "updated_at", getattr(old, "created_at", utcnow()))
                    if current is None:
                        values = {field: getattr(old, field) for field in fields}
                        current = model(chat_id=new_chat_id, **values)
                        session.add(current)
                    else:
                        cur_stamp = getattr(current, "updated_at", getattr(current, "created_at", utcnow()))
                        old_custom = any(getattr(old, f) != defaults.get(f) for f in fields)
                        new_custom = any(getattr(current, f) != defaults.get(f) for f in fields)
                        # A freshly registered supergroup row contains defaults. Do
                        # not let those defaults erase real settings from the old
                        # basic-group id. If both sides were customized, the newer
                        # edit wins.
                        take_old = old_custom and not new_custom
                        if old_custom == new_custom:
                            take_old = bool(old_stamp and cur_stamp and old_stamp > cur_stamp)
                        if take_old:
                            for field in fields:
                                setattr(current, field, getattr(old, field))
                    await session.delete(old)
                    return current

                chat_settings = await merge_pk(
                    ChatControlSettingsRecord, ("shadow_mode",), {"shadow_mode": False}
                )
                await merge_pk(
                    ChatEnforcementSettingsRecord,
                    ("live_ban_enabled",),
                    {"live_ban_enabled": False},
                )
                await merge_pk(
                    ChatMuteSettingsRecord,
                    ("mute_duration_minutes",),
                    {"mute_duration_minutes": 60},
                )
                await merge_pk(
                    ChatRaidSettingsRecord,
                    ("raid_guard_enabled",),
                    {"raid_guard_enabled": False},
                )
                if chat_settings is None:
                    chat_settings = ChatControlSettingsRecord(
                        chat_id=new_chat_id,
                        chat_title=chat_title,
                        shadow_mode=False,
                    )
                    session.add(chat_settings)
                elif chat_title:
                    chat_settings.chat_title = chat_title
                    chat_settings.updated_at = utcnow()

                # Records whose identity is not the chat id can be moved directly.
                for model, column in (
                    (ModerationTicketRecord, ModerationTicketRecord.chat_id),
                    (CommunityPolicyVersionRecord, CommunityPolicyVersionRecord.chat_id),
                    (CommunityPolicyDraftRecord, CommunityPolicyDraftRecord.chat_id),
                    (KnownModerationPatternRecord, KnownModerationPatternRecord.chat_id),
                    (ModeratorFeedbackRecord, ModeratorFeedbackRecord.chat_id),
                    (SemanticObservationRecord, SemanticObservationRecord.chat_id),
                    (RaidIncidentRecord, RaidIncidentRecord.chat_id),
                    (ModerationBanRecord, ModerationBanRecord.chat_id),
                ):
                    await session.execute(
                        update(model).where(column == old_chat_id).values({column.key: new_chat_id})
                    )

                await session.execute(
                    update(AdminDashboardStateRecord)
                    .where(AdminDashboardStateRecord.selected_chat_id == old_chat_id)
                    .values(selected_chat_id=new_chat_id, updated_at=utcnow())
                )
                await session.execute(
                    update(TestArtifactRecord)
                    .where(TestArtifactRecord.managed_chat_id == old_chat_id)
                    .values(managed_chat_id=new_chat_id)
                )
                await session.execute(
                    update(TestArtifactRecord)
                    .where(TestArtifactRecord.telegram_chat_id == old_chat_id)
                    .values(telegram_chat_id=new_chat_id)
                )
                await session.execute(
                    update(AdminAlertArtifactRecord)
                    .where(AdminAlertArtifactRecord.managed_chat_id == old_chat_id)
                    .values(managed_chat_id=new_chat_id)
                )

                # Keep exactly one active policy after merging two chat ids and
                # make version ordering deterministic again.
                policy_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(CommunityPolicyVersionRecord.chat_id == new_chat_id)
                    .order_by(CommunityPolicyVersionRecord.created_at.asc(), CommunityPolicyVersionRecord.id.asc())
                )
                policies = list(policy_result.scalars().all())
                if policies:
                    active_candidates = [r for r in policies if r.active]
                    winner = max(
                        active_candidates or policies,
                        key=lambda r: (r.created_at, r.id),
                    )
                    for index, record in enumerate(policies, 1):
                        record.version = index
                        record.active = record.id == winner.id

                # If the same user was recorded as actively banned on both ids,
                # expose only the newest active record.
                bans_result = await session.execute(
                    select(ModerationBanRecord)
                    .where(
                        ModerationBanRecord.chat_id == new_chat_id,
                        ModerationBanRecord.active.is_(True),
                    )
                    .order_by(ModerationBanRecord.created_at.desc(), ModerationBanRecord.id.desc())
                )
                seen_users: set[int] = set()
                for record in bans_result.scalars().all():
                    if record.user_id in seen_users:
                        record.active = False
                        record.unbanned_at = utcnow()
                    else:
                        seen_users.add(record.user_id)

        return new_chat_id

    async def list_managed_chats(self) -> list[ManagedChat]:
        """Return canonical communities only; migrated basic-group aliases are hidden."""
        async with self.session_factory() as session:
            migration_result = await session.execute(
                select(ChatMigrationRecord.old_chat_id, ChatMigrationRecord.new_chat_id)
            )
            aliases = {int(old): int(new) for old, new in migration_result.all()}

            def canonical(value: int) -> int:
                current = int(value)
                seen: set[int] = set()
                while current in aliases and current not in seen:
                    seen.add(current)
                    current = aliases[current]
                return current

            settings_result = await session.execute(
                select(ChatControlSettingsRecord.chat_id, ChatControlSettingsRecord.chat_title)
            )
            message_result = await session.execute(
                select(MessageRecord.chat_id, func.max(MessageRecord.chat_title)).group_by(MessageRecord.chat_id)
            )

            merged: dict[int, str] = {}
            for chat_id, title in message_result.all():
                target = canonical(int(chat_id))
                merged[target] = title or merged.get(target) or str(target)
            for chat_id, title in settings_result.all():
                target = canonical(int(chat_id))
                merged[target] = title or merged.get(target) or str(target)

            return [
                ManagedChat(chat_id=chat_id, title=title)
                for chat_id, title in sorted(
                    merged.items(), key=lambda item: (item[1].casefold(), item[0])
                )
            ]

    async def ensure_chat_settings(
        self, *, chat_id: int, chat_title: str | None = None
    ) -> ChatControlSettingsRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(ChatControlSettingsRecord, chat_id)
                if record is None:
                    record = ChatControlSettingsRecord(
                        chat_id=chat_id,
                        chat_title=chat_title,
                        shadow_mode=False,
                    )
                    session.add(record)
                elif chat_title and record.chat_title != chat_title:
                    record.chat_title = chat_title
            return record

    async def get_chat_settings(self, chat_id: int) -> ChatControlSettingsRecord:
        return await self.ensure_chat_settings(chat_id=chat_id)

    async def toggle_shadow(self, chat_id: int) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(ChatControlSettingsRecord, chat_id)
                if record is None:
                    record = ChatControlSettingsRecord(
                        chat_id=chat_id, shadow_mode=True
                    )
                    session.add(record)
                else:
                    record.shadow_mode = not record.shadow_mode
            return bool(record.shadow_mode)

    async def ensure_enforcement_settings(
        self,
        *,
        chat_id: int,
    ) -> ChatEnforcementSettingsRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    ChatEnforcementSettingsRecord,
                    chat_id,
                )

                if record is None:
                    record = ChatEnforcementSettingsRecord(
                        chat_id=chat_id,
                        live_ban_enabled=False,
                    )
                    session.add(record)

            return record

    async def get_live_ban_enabled(
        self,
        chat_id: int,
    ) -> bool:
        record = await self.ensure_enforcement_settings(
            chat_id=chat_id
        )
        return bool(record.live_ban_enabled)

    async def toggle_live_ban(
        self,
        chat_id: int,
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    ChatEnforcementSettingsRecord,
                    chat_id,
                )

                if record is None:
                    record = ChatEnforcementSettingsRecord(
                        chat_id=chat_id,
                        live_ban_enabled=True,
                    )
                    session.add(record)
                else:
                    record.live_ban_enabled = (
                        not record.live_ban_enabled
                    )
                    record.updated_at = utcnow()

            return bool(record.live_ban_enabled)

    async def ensure_mute_settings(
        self,
        *,
        chat_id: int,
    ) -> ChatMuteSettingsRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    ChatMuteSettingsRecord,
                    chat_id,
                )
                if record is None:
                    record = ChatMuteSettingsRecord(
                        chat_id=chat_id,
                        mute_duration_minutes=60,
                    )
                    session.add(record)
            return record

    async def get_mute_duration_minutes(
        self,
        chat_id: int,
    ) -> int:
        record = await self.ensure_mute_settings(chat_id=chat_id)
        return int(record.mute_duration_minutes or 60)

    async def set_mute_duration_minutes(
        self,
        chat_id: int,
        minutes: int,
    ) -> int:
        allowed = {15, 30, 60, 180, 360, 720, 1440}
        minutes = int(minutes)
        if minutes not in allowed:
            raise ValueError("Unsupported mute duration")

        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(ChatMuteSettingsRecord, chat_id)
                if record is None:
                    record = ChatMuteSettingsRecord(
                        chat_id=chat_id,
                        mute_duration_minutes=minutes,
                    )
                    session.add(record)
                else:
                    record.mute_duration_minutes = minutes
                    record.updated_at = utcnow()
            return int(record.mute_duration_minutes)

    async def get_active_community_policy(
        self,
        chat_id: int,
    ) -> CommunityPolicyVersionRecord | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CommunityPolicyVersionRecord)
                .where(
                    CommunityPolicyVersionRecord.chat_id == chat_id,
                    CommunityPolicyVersionRecord.active.is_(True),
                )
                .order_by(CommunityPolicyVersionRecord.version.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_previous_community_policy(
        self,
        chat_id: int,
        *,
        before_version: int | None = None,
    ) -> CommunityPolicyVersionRecord | None:
        async with self.session_factory() as session:
            if before_version is None:
                active_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(
                        CommunityPolicyVersionRecord.chat_id == chat_id,
                        CommunityPolicyVersionRecord.active.is_(True),
                    )
                    .order_by(CommunityPolicyVersionRecord.version.desc())
                    .limit(1)
                )
                active = active_result.scalar_one_or_none()
                if active is None:
                    return None
                before_version = active.version

            result = await session.execute(
                select(CommunityPolicyVersionRecord)
                .where(
                    CommunityPolicyVersionRecord.chat_id == chat_id,
                    CommunityPolicyVersionRecord.version < before_version,
                )
                .order_by(CommunityPolicyVersionRecord.version.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def save_community_policy_draft(
        self,
        *,
        admin_id: int,
        chat_id: int,
        base_version: int | None,
        source_text: str,
        rules_json: str,
        summary: str,
        changes_json: str,
        ignored_json: str,
    ) -> CommunityPolicyDraftRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(CommunityPolicyDraftRecord, admin_id)

                if record is None:
                    record = CommunityPolicyDraftRecord(
                        admin_id=admin_id,
                        chat_id=chat_id,
                        base_version=base_version,
                        source_text=source_text,
                        rules_json=rules_json,
                        summary=summary,
                        changes_json=changes_json,
                        ignored_json=ignored_json,
                    )
                    session.add(record)
                else:
                    record.chat_id = chat_id
                    record.base_version = base_version
                    record.source_text = source_text
                    record.rules_json = rules_json
                    record.summary = summary
                    record.changes_json = changes_json
                    record.ignored_json = ignored_json
                    record.updated_at = utcnow()

            return record

    async def get_community_policy_draft(
        self,
        admin_id: int,
    ) -> CommunityPolicyDraftRecord | None:
        async with self.session_factory() as session:
            return await session.get(CommunityPolicyDraftRecord, admin_id)

    async def clear_community_policy_draft(
        self,
        admin_id: int,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(CommunityPolicyDraftRecord, admin_id)
                if record is not None:
                    await session.delete(record)

    async def apply_community_policy_draft(
        self,
        *,
        admin_id: int,
        expected_chat_id: int | None = None,
    ) -> CommunityPolicyVersionRecord | None:
        async with self.session_factory() as session:
            async with session.begin():
                draft = await session.get(CommunityPolicyDraftRecord, admin_id)
                if draft is None:
                    return None

                # Per-chat isolation: an old/stale Apply button from another
                # selected chat must never activate this draft elsewhere.
                if (
                    expected_chat_id is not None
                    and draft.chat_id != expected_chat_id
                ):
                    return None

                max_result = await session.execute(
                    select(func.max(CommunityPolicyVersionRecord.version))
                    .where(CommunityPolicyVersionRecord.chat_id == draft.chat_id)
                )
                next_version = int(max_result.scalar_one() or 0) + 1

                active_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(
                        CommunityPolicyVersionRecord.chat_id == draft.chat_id,
                        CommunityPolicyVersionRecord.active.is_(True),
                    )
                )
                for current in active_result.scalars().all():
                    current.active = False

                version = CommunityPolicyVersionRecord(
                    chat_id=draft.chat_id,
                    version=next_version,
                    source_text=draft.source_text,
                    rules_json=draft.rules_json,
                    summary=draft.summary,
                    created_by_admin_id=admin_id,
                    active=True,
                )
                session.add(version)
                await session.delete(draft)

            return version

    async def rollback_community_policy(
        self,
        *,
        chat_id: int,
        admin_id: int,
    ) -> CommunityPolicyVersionRecord | None:
        async with self.session_factory() as session:
            async with session.begin():
                active_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(
                        CommunityPolicyVersionRecord.chat_id == chat_id,
                        CommunityPolicyVersionRecord.active.is_(True),
                    )
                    .order_by(CommunityPolicyVersionRecord.version.desc())
                    .limit(1)
                )
                active = active_result.scalar_one_or_none()
                if active is None:
                    return None

                previous_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(
                        CommunityPolicyVersionRecord.chat_id == chat_id,
                        CommunityPolicyVersionRecord.version < active.version,
                    )
                    .order_by(CommunityPolicyVersionRecord.version.desc())
                    .limit(1)
                )
                previous = previous_result.scalar_one_or_none()
                if previous is None:
                    return None

                max_result = await session.execute(
                    select(func.max(CommunityPolicyVersionRecord.version))
                    .where(CommunityPolicyVersionRecord.chat_id == chat_id)
                )
                next_version = int(max_result.scalar_one() or 0) + 1

                active.active = False

                version = CommunityPolicyVersionRecord(
                    chat_id=chat_id,
                    version=next_version,
                    source_text=previous.source_text,
                    rules_json=previous.rules_json,
                    summary=(
                        f"Rollback to policy v{previous.version}. "
                        + previous.summary
                    ).strip(),
                    created_by_admin_id=admin_id,
                    active=True,
                )
                session.add(version)

            return version

    async def clear_custom_community_policy(
        self,
        *,
        chat_id: int,
        admin_id: int,
    ) -> CommunityPolicyVersionRecord:
        async with self.session_factory() as session:
            async with session.begin():
                max_result = await session.execute(
                    select(func.max(CommunityPolicyVersionRecord.version))
                    .where(CommunityPolicyVersionRecord.chat_id == chat_id)
                )
                next_version = int(max_result.scalar_one() or 0) + 1

                active_result = await session.execute(
                    select(CommunityPolicyVersionRecord)
                    .where(
                        CommunityPolicyVersionRecord.chat_id == chat_id,
                        CommunityPolicyVersionRecord.active.is_(True),
                    )
                )
                for current in active_result.scalars().all():
                    current.active = False

                version = CommunityPolicyVersionRecord(
                    chat_id=chat_id,
                    version=next_version,
                    source_text="",
                    rules_json="[]",
                    summary="Custom community rules cleared; core ModGuard policy remains active.",
                    created_by_admin_id=admin_id,
                    active=True,
                )
                session.add(version)

            return version

    async def create_or_update_ticket(
        self,
        *,
        chat_id: int,
        telegram_message_id: int | None,
        target_user_id: int | None,
        username: str | None,
        category: str,
        severity: str | None,
        confidence: float | None,
        reason: str,
        message_text: str,
        context: dict,
    ) -> ModerationTicketRecord:
        async with self.session_factory() as session:
            async with session.begin():
                statement = (
                    select(ModerationTicketRecord)
                    .where(
                        ModerationTicketRecord.chat_id == chat_id,
                        ModerationTicketRecord.category == category,
                        ModerationTicketRecord.status == "open",
                    )
                    .order_by(ModerationTicketRecord.updated_at.desc())
                    .limit(1)
                )

                if target_user_id is None:
                    statement = statement.where(
                        ModerationTicketRecord.target_user_id.is_(None)
                    )
                else:
                    statement = statement.where(
                        ModerationTicketRecord.target_user_id == target_user_id
                    )

                result = await session.execute(statement)
                ticket = result.scalar_one_or_none()

                if ticket is None:
                    ticket = ModerationTicketRecord(
                        ticket_key="T-" + uuid4().hex[:8].upper(),
                        chat_id=chat_id,
                        telegram_message_id=telegram_message_id,
                        target_user_id=target_user_id,
                        username=username,
                        category=category,
                        severity=severity,
                        confidence=confidence,
                        reason=reason,
                        message_text=message_text,
                        context_json=json.dumps(context, ensure_ascii=False),
                        status="open",
                        occurrence_count=1,
                    )
                    session.add(ticket)
                else:
                    ticket.telegram_message_id = telegram_message_id
                    ticket.username = username or ticket.username
                    ticket.severity = severity
                    ticket.confidence = confidence
                    ticket.reason = reason
                    ticket.message_text = message_text
                    ticket.context_json = json.dumps(context, ensure_ascii=False)
                    ticket.occurrence_count += 1
                    ticket.updated_at = utcnow()

            return ticket


    async def create_test_ticket(
        self,
        *,
        chat_id: int,
        source: str = "manual",
        message_text: str | None = None,
        reason: str | None = None,
        category: str = "scam",
        confidence: float = 0.64,
        telegram_message_id: int | None = None,
    ) -> ModerationTicketRecord:
        """
        Create a synthetic review ticket for QA/demo mode.

        It never points at a real Telegram user or Telegram message.
        """
        if source == "report":
            default_message = (
                "There is a closed topic with good profit. "
                "Details are available only in DM."
            )
            default_reason = (
                "TEST REPORT: a member reported the source message. "
                "Deep re-review detected risk but left the decision to a moderator."
            )
        else:
            default_message = (
                "There is a closed opportunity with good profit. "
                "Details are available only in DM."
            )
            default_reason = (
                "Synthetic ambiguous case: Deep AI detected risk signals "
                "but did not reach the confidence required for autonomous action."
            )

        async with self.session_factory() as session:
            async with session.begin():
                ticket = ModerationTicketRecord(
                    ticket_key="TEST-" + uuid4().hex[:8].upper(),
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    target_user_id=None,
                    username="@test_user",
                    category=category,
                    severity="medium",
                    confidence=confidence,
                    reason=reason or default_reason,
                    message_text=message_text or default_message,
                    context_json=json.dumps(
                        {
                            "test_mode": True,
                            "test_source": source,
                            "simulated": True,
                        },
                        ensure_ascii=False,
                    ),
                    status="open",
                    occurrence_count=1,
                )
                session.add(ticket)

            return ticket

    async def clear_test_tickets(
        self,
        *,
        chat_id: int,
    ) -> int:
        """
        Remove all synthetic TEST-* tickets for the selected community.

        Test data should not pollute the real moderation history/dashboard.
        """
        async with self.session_factory() as session:
            async with session.begin():
                count_statement = select(
                    func.count(ModerationTicketRecord.id)
                ).where(
                    ModerationTicketRecord.chat_id == chat_id,
                    ModerationTicketRecord.ticket_key.like("TEST-%"),
                )
                count_result = await session.execute(
                    count_statement
                )
                count = int(
                    count_result.scalar_one()
                )

                await session.execute(
                    delete(
                        ModerationTicketRecord
                    ).where(
                        ModerationTicketRecord.chat_id == chat_id,
                        ModerationTicketRecord.ticket_key.like("TEST-%"),
                    )
                )

            return count

    async def register_test_artifact(
        self,
        *,
        managed_chat_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        kind: str,
    ) -> TestArtifactRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = TestArtifactRecord(
                    managed_chat_id=managed_chat_id,
                    telegram_chat_id=telegram_chat_id,
                    telegram_message_id=telegram_message_id,
                    kind=kind,
                )
                session.add(record)

            return record

    async def list_test_artifacts(
        self,
        *,
        managed_chat_id: int,
    ) -> list[TestArtifactRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    TestArtifactRecord
                )
                .where(
                    TestArtifactRecord.managed_chat_id
                    == managed_chat_id
                )
                .order_by(
                    TestArtifactRecord.id.asc()
                )
            )
            return list(
                result.scalars().all()
            )

    async def purge_test_artifacts(
        self,
        *,
        managed_chat_id: int,
    ) -> int:
        async with self.session_factory() as session:
            async with session.begin():
                count_result = await session.execute(
                    select(
                        func.count(
                            TestArtifactRecord.id
                        )
                    ).where(
                        TestArtifactRecord.managed_chat_id
                        == managed_chat_id
                    )
                )
                count = int(
                    count_result.scalar_one()
                )

                await session.execute(
                    delete(
                        TestArtifactRecord
                    ).where(
                        TestArtifactRecord.managed_chat_id
                        == managed_chat_id
                    )
                )

            return count

    async def count_open_tickets(self, chat_id: int) -> int:
        async with self.session_factory() as session:
            statement = select(func.count(ModerationTicketRecord.id)).where(
                ModerationTicketRecord.chat_id == chat_id,
                ModerationTicketRecord.status == "open",
            )
            result = await session.execute(statement)
            return int(result.scalar_one())

    async def list_open_tickets(
        self, *, chat_id: int, limit: int = 20
    ) -> list[ModerationTicketRecord]:
        async with self.session_factory() as session:
            statement = (
                select(ModerationTicketRecord)
                .where(
                    ModerationTicketRecord.chat_id == chat_id,
                    ModerationTicketRecord.status == "open",
                )
                .order_by(ModerationTicketRecord.updated_at.desc())
                .limit(limit)
            )
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def get_ticket(self, ticket_id: int) -> ModerationTicketRecord | None:
        async with self.session_factory() as session:
            return await session.get(ModerationTicketRecord, ticket_id)

    async def resolve_ticket(self, *, ticket_id: int, action: str) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                ticket = await session.get(ModerationTicketRecord, ticket_id)
                if ticket is None:
                    return False
                ticket.status = "resolved"
                ticket.resolution_action = action
                ticket.resolved_at = utcnow()
                ticket.updated_at = utcnow()
            return True

    @staticmethod
    def _ticket_is_test_feedback_source(
        ticket: ModerationTicketRecord,
    ) -> bool:
        if ticket.ticket_key.startswith("TEST-"):
            return True

        try:
            payload = json.loads(
                ticket.context_json
                or "{}"
            )
        except Exception:
            return False

        return bool(
            payload.get("test_mode")
        )

    async def save_moderator_feedback(
        self,
        *,
        ticket_id: int,
        moderator_action: str,
        moderator_admin_id: int | None,
    ) -> ModeratorFeedbackRecord | None:
        """
        Learn from one REAL human ticket resolution.

        Idempotent by source_ticket_id.
        """

        async with self.session_factory() as session:
            async with session.begin():
                ticket = await session.get(
                    ModerationTicketRecord,
                    ticket_id,
                )

                if ticket is None:
                    return None

                if self._ticket_is_test_feedback_source(
                    ticket
                ):
                    return None

                existing_result = await session.execute(
                    select(
                        ModeratorFeedbackRecord
                    )
                    .where(
                        ModeratorFeedbackRecord
                        .source_ticket_id
                        == ticket_id
                    )
                    .limit(1)
                )

                existing = (
                    existing_result
                    .scalar_one_or_none()
                )

                if existing is not None:
                    return existing

                active_policy_result = (
                    await session.execute(
                        select(
                            CommunityPolicyVersionRecord
                        )
                        .where(
                            CommunityPolicyVersionRecord
                            .chat_id
                            == ticket.chat_id,
                            CommunityPolicyVersionRecord
                            .active
                            .is_(True),
                        )
                        .order_by(
                            CommunityPolicyVersionRecord
                            .version
                            .desc()
                        )
                        .limit(1)
                    )
                )

                active_policy = (
                    active_policy_result
                    .scalar_one_or_none()
                )

                record = ModeratorFeedbackRecord(
                    source_ticket_id=ticket.id,
                    chat_id=ticket.chat_id,
                    moderator_admin_id=(
                        moderator_admin_id
                    ),
                    message_text=(
                        ticket.message_text
                        or ""
                    ),
                    ai_category=ticket.category,
                    ai_severity=ticket.severity,
                    ai_confidence=(
                        ticket.confidence
                    ),
                    ai_reason=(
                        ticket.reason
                        or ""
                    ),
                    moderator_action=(
                        moderator_action
                    ),
                    policy_version=(
                        active_policy.version
                        if active_policy
                        is not None
                        else None
                    ),
                )

                session.add(record)
                await session.flush()

                return record

    async def recent_moderator_feedback(
        self,
        *,
        chat_id: int,
        limit: int = 12,
    ) -> list[ModeratorFeedbackRecord]:
        """
        Recent human decisions for one chat only.
        """

        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    ModeratorFeedbackRecord
                )
                .where(
                    ModeratorFeedbackRecord
                    .chat_id
                    == chat_id
                )
                .order_by(
                    ModeratorFeedbackRecord
                    .created_at
                    .desc(),
                    ModeratorFeedbackRecord
                    .id
                    .desc(),
                )
                .limit(
                    max(
                        1,
                        min(
                            int(limit),
                            50,
                        ),
                    )
                )
            )

            return list(
                result.scalars().all()
            )

    async def count_moderator_feedback(
        self,
        *,
        chat_id: int,
    ) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    func.count(
                        ModeratorFeedbackRecord.id
                    )
                )
                .where(
                    ModeratorFeedbackRecord.chat_id
                    == chat_id
                )
            )

            return int(
                result.scalar_one()
                or 0
            )

    async def get_activity_summary(
        self, *, chat_id: int, hours: int = 24
    ) -> ActivitySummary:
        since = utcnow() - timedelta(hours=hours)

        async with self.session_factory() as session:
            statement = (
                select(
                    ModerationEventRecord.action,
                    func.count(ModerationEventRecord.id),
                )
                .where(
                    ModerationEventRecord.chat_id == chat_id,
                    ModerationEventRecord.created_at >= since,
                )
                .group_by(ModerationEventRecord.action)
            )
            result = await session.execute(statement)
            counts = {action: int(count) for action, count in result.all()}

        return ActivitySummary(
            hours=hours,
            total=sum(counts.values()),
            deleted=counts.get("delete", 0),
            banned=counts.get("ban", 0),
            muted=counts.get("mute", 0),
            warned=counts.get("warn", 0),
            reviews=counts.get("escalate", 0),
            failed_deletes=counts.get("delete_failed", 0),
        )

    async def get_dashboard_state(
        self, admin_id: int
    ) -> AdminDashboardStateRecord | None:
        async with self.session_factory() as session:
            return await session.get(AdminDashboardStateRecord, admin_id)

    async def save_dashboard_state(
        self,
        *,
        admin_id: int,
        dashboard_message_id: int | None = None,
        selected_chat_id: int | None = None,
        view: str | None = None,
    ) -> AdminDashboardStateRecord:
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(AdminDashboardStateRecord, admin_id)
                if state is None:
                    state = AdminDashboardStateRecord(
                        admin_id=admin_id,
                        dashboard_message_id=dashboard_message_id,
                        selected_chat_id=selected_chat_id,
                        view=view or "dashboard",
                    )
                    session.add(state)
                else:
                    if dashboard_message_id is not None:
                        state.dashboard_message_id = dashboard_message_id
                    if selected_chat_id is not None:
                        state.selected_chat_id = selected_chat_id
                    if view is not None:
                        state.view = view
                    state.updated_at = utcnow()
            return state

    async def dashboard_states_for_chat(
        self, *, chat_id: int, view: str = "dashboard"
    ) -> list[AdminDashboardStateRecord]:
        async with self.session_factory() as session:
            statement = select(AdminDashboardStateRecord).where(
                AdminDashboardStateRecord.selected_chat_id == chat_id,
                AdminDashboardStateRecord.view == view,
                AdminDashboardStateRecord.dashboard_message_id.is_not(None),
            )
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def remember_confirmed_pattern(
        self,
        *,
        chat_id: int,
        normalized_text: str,
        category: str,
        confidence: float,
        decision_json: str,
    ) -> None:
        canonical = normalized_text.strip().casefold()
        if not canonical:
            return

        fingerprint = message_fingerprint(canonical)

        async with self.session_factory() as session:
            async with session.begin():
                statement = (
                    select(KnownModerationPatternRecord)
                    .where(
                        KnownModerationPatternRecord.chat_id == chat_id,
                        KnownModerationPatternRecord.fingerprint == fingerprint,
                    )
                    .limit(1)
                )
                result = await session.execute(statement)
                record = result.scalar_one_or_none()

                if record is None:
                    session.add(
                        KnownModerationPatternRecord(
                            chat_id=chat_id,
                            fingerprint=fingerprint,
                            normalized_text=canonical,
                            category=category,
                            confidence=confidence,
                            decision_json=decision_json,
                            hit_count=0,
                            active=True,
                        )
                    )
                else:
                    record.category = category
                    record.confidence = confidence
                    record.decision_json = decision_json
                    record.active = True
                    record.updated_at = utcnow()

    async def match_confirmed_pattern(
        self,
        *,
        chat_id: int,
        normalized_text: str,
    ) -> str | None:
        canonical = normalized_text.strip().casefold()
        if not canonical:
            return None

        fingerprint = message_fingerprint(canonical)

        async with self.session_factory() as session:
            async with session.begin():
                statement = (
                    select(KnownModerationPatternRecord)
                    .where(
                        KnownModerationPatternRecord.chat_id == chat_id,
                        KnownModerationPatternRecord.fingerprint == fingerprint,
                        KnownModerationPatternRecord.active.is_(True),
                    )
                    .limit(1)
                )
                result = await session.execute(statement)
                record = result.scalar_one_or_none()

                if record is None:
                    return None

                # Collision defense.
                if record.normalized_text != canonical:
                    return None

                record.hit_count += 1
                record.updated_at = utcnow()

                return record.decision_json


    async def record_moderation_ban(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        source: str,
        category: str | None,
        reason: str,
    ) -> ModerationBanRecord:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ModerationBanRecord)
                    .where(
                        ModerationBanRecord.chat_id == chat_id,
                        ModerationBanRecord.user_id == user_id,
                        ModerationBanRecord.active.is_(True),
                    )
                    .order_by(ModerationBanRecord.id.desc())
                    .limit(1)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    record = ModerationBanRecord(
                        chat_id=chat_id,
                        user_id=user_id,
                        username=username,
                        source=source,
                        category=category,
                        reason=reason,
                        active=True,
                    )
                    session.add(record)
                else:
                    record.username = username or record.username
                    record.source = source
                    record.category = category
                    record.reason = reason
                await session.flush()
                return record

    async def count_active_moderation_bans(
        self,
        *,
        chat_id: int,
    ) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count(ModerationBanRecord.id)).where(
                    ModerationBanRecord.chat_id == chat_id,
                    ModerationBanRecord.active.is_(True),
                )
            )
            return int(result.scalar_one() or 0)

    async def list_active_moderation_bans(
        self,
        *,
        chat_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ModerationBanRecord]:
        """Newest active bans first. Pagination is deterministic by timestamp/id."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(ModerationBanRecord)
                .where(
                    ModerationBanRecord.chat_id == chat_id,
                    ModerationBanRecord.active.is_(True),
                )
                .order_by(
                    ModerationBanRecord.created_at.desc(),
                    ModerationBanRecord.id.desc(),
                )
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit), 100)))
            )
            return list(result.scalars().all())

    async def search_active_moderation_bans(
        self,
        *,
        chat_id: int,
        query: str,
        limit: int = 10,
    ) -> list[ModerationBanRecord]:
        """Search active bans by @username fragment or numeric Telegram user id."""
        cleaned = query.strip().lstrip("@").casefold()
        if not cleaned:
            return []

        async with self.session_factory() as session:
            conditions = [
                func.lower(func.coalesce(ModerationBanRecord.username, "")).like(
                    f"%{cleaned}%"
                )
            ]
            if cleaned.lstrip("-").isdigit():
                conditions.append(ModerationBanRecord.user_id == int(cleaned))

            result = await session.execute(
                select(ModerationBanRecord)
                .where(
                    ModerationBanRecord.chat_id == chat_id,
                    ModerationBanRecord.active.is_(True),
                    or_(*conditions),
                )
                .order_by(
                    ModerationBanRecord.created_at.desc(),
                    ModerationBanRecord.id.desc(),
                )
                .limit(max(1, min(int(limit), 50)))
            )
            return list(result.scalars().all())

    async def get_moderation_ban(
        self,
        ban_id: int,
    ) -> ModerationBanRecord | None:
        async with self.session_factory() as session:
            return await session.get(ModerationBanRecord, ban_id)

    async def mark_moderation_unbanned(
        self,
        *,
        ban_id: int,
    ) -> ModerationBanRecord | None:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(ModerationBanRecord, ban_id)
                if record is None:
                    return None
                record.active = False
                record.unbanned_at = utcnow()
                await session.flush()
                return record

    async def register_admin_alert_artifact(
        self,
        *,
        managed_chat_id: int,
        admin_chat_id: int,
        telegram_message_id: int,
        kind: str,
    ) -> AdminAlertArtifactRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = AdminAlertArtifactRecord(
                    managed_chat_id=managed_chat_id,
                    admin_chat_id=admin_chat_id,
                    telegram_message_id=telegram_message_id,
                    kind=kind,
                )
                session.add(record)
                await session.flush()
                return record

    async def list_admin_alert_artifacts(
        self,
        *,
        managed_chat_id: int,
        kind: str | None = None,
    ) -> list[AdminAlertArtifactRecord]:
        async with self.session_factory() as session:
            statement = select(AdminAlertArtifactRecord).where(
                AdminAlertArtifactRecord.managed_chat_id == managed_chat_id
            )
            if kind is not None:
                statement = statement.where(AdminAlertArtifactRecord.kind == kind)
            result = await session.execute(statement.order_by(AdminAlertArtifactRecord.id.asc()))
            return list(result.scalars().all())

    async def purge_admin_alert_artifacts(
        self,
        *,
        managed_chat_id: int,
        kind: str | None = None,
    ) -> int:
        async with self.session_factory() as session:
            async with session.begin():
                count_stmt = select(func.count(AdminAlertArtifactRecord.id)).where(
                    AdminAlertArtifactRecord.managed_chat_id == managed_chat_id
                )
                delete_stmt = delete(AdminAlertArtifactRecord).where(
                    AdminAlertArtifactRecord.managed_chat_id == managed_chat_id
                )
                if kind is not None:
                    count_stmt = count_stmt.where(AdminAlertArtifactRecord.kind == kind)
                    delete_stmt = delete_stmt.where(AdminAlertArtifactRecord.kind == kind)
                count = int((await session.execute(count_stmt)).scalar_one() or 0)
                await session.execute(delete_stmt)
                return count

    async def ensure_raid_settings(
        self,
        *,
        chat_id: int,
    ) -> ChatRaidSettingsRecord:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    ChatRaidSettingsRecord,
                    chat_id,
                )

                if record is None:
                    record = ChatRaidSettingsRecord(
                        chat_id=chat_id,
                        raid_guard_enabled=False,
                    )
                    session.add(record)

            return record

    async def get_raid_guard_enabled(
        self,
        chat_id: int,
    ) -> bool:
        record = await self.ensure_raid_settings(
            chat_id=chat_id
        )
        return bool(
            record.raid_guard_enabled
        )

    async def toggle_raid_guard(
        self,
        chat_id: int,
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    ChatRaidSettingsRecord,
                    chat_id,
                )

                if record is None:
                    record = ChatRaidSettingsRecord(
                        chat_id=chat_id,
                        raid_guard_enabled=True,
                    )
                    session.add(record)
                else:
                    record.raid_guard_enabled = (
                        not record.raid_guard_enabled
                    )
                    record.updated_at = utcnow()

            return bool(
                record.raid_guard_enabled
            )

    async def recent_semantic_observations(
        self,
        *,
        chat_id: int,
        since: datetime,
        limit: int = 160,
    ) -> list[SemanticObservationRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    SemanticObservationRecord
                )
                .where(
                    SemanticObservationRecord.chat_id
                    == chat_id,
                    SemanticObservationRecord.updated_at
                    >= since,
                )
                .order_by(
                    SemanticObservationRecord.updated_at.desc(),
                    SemanticObservationRecord.id.desc(),
                )
                .limit(
                    max(
                        1,
                        min(int(limit), 500),
                    )
                )
            )

            return list(
                result.scalars().all()
            )

    async def save_semantic_observation(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        user_id: int | None,
        message_text: str,
        decision_category: str | None,
        final_action: str | None,
        confidence: float | None,
        current_violation: bool,
        embedding_json: str,
        cluster_key: str | None,
        nearest_similarity: float | None,
    ) -> SemanticObservationRecord:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(
                        SemanticObservationRecord
                    )
                    .where(
                        SemanticObservationRecord.chat_id
                        == chat_id,
                        SemanticObservationRecord.telegram_message_id
                        == telegram_message_id,
                    )
                    .limit(1)
                )

                record = result.scalar_one_or_none()

                if record is None:
                    record = SemanticObservationRecord(
                        chat_id=chat_id,
                        telegram_message_id=telegram_message_id,
                        user_id=user_id,
                        message_text=message_text,
                        decision_category=decision_category,
                        final_action=final_action,
                        confidence=confidence,
                        current_violation=current_violation,
                        embedding_json=embedding_json,
                        cluster_key=cluster_key,
                        nearest_similarity=nearest_similarity,
                    )
                    session.add(record)
                else:
                    record.user_id = user_id
                    record.message_text = message_text
                    record.decision_category = decision_category
                    record.final_action = final_action
                    record.confidence = confidence
                    record.current_violation = current_violation
                    record.embedding_json = embedding_json
                    record.cluster_key = cluster_key
                    record.nearest_similarity = nearest_similarity
                    record.updated_at = utcnow()

                await session.flush()
                return record

    async def assign_semantic_cluster(
        self,
        *,
        observation_id: int,
        cluster_key: str,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    SemanticObservationRecord,
                    observation_id,
                )
                if record is not None:
                    record.cluster_key = cluster_key
                    record.updated_at = utcnow()

    async def semantic_cluster_records(
        self,
        *,
        chat_id: int,
        cluster_key: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[SemanticObservationRecord]:
        async with self.session_factory() as session:
            stmt = select(
                SemanticObservationRecord
            ).where(
                SemanticObservationRecord.chat_id
                == chat_id,
                SemanticObservationRecord.cluster_key
                == cluster_key,
            )

            if since is not None:
                stmt = stmt.where(
                    SemanticObservationRecord.updated_at
                    >= since
                )

            result = await session.execute(
                stmt.order_by(
                    SemanticObservationRecord.updated_at.asc(),
                    SemanticObservationRecord.id.asc(),
                ).limit(
                    max(1, min(int(limit), 500))
                )
            )

            return list(
                result.scalars().all()
            )

    async def get_open_raid_incident_for_cluster(
        self,
        *,
        chat_id: int,
        cluster_key: str,
    ) -> RaidIncidentRecord | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RaidIncidentRecord)
                .where(
                    RaidIncidentRecord.chat_id == chat_id,
                    RaidIncidentRecord.cluster_key == cluster_key,
                    RaidIncidentRecord.status.in_(
                        {"active", "shadow"}
                    ),
                )
                .order_by(
                    RaidIncidentRecord.id.desc()
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def create_or_update_raid_incident(
        self,
        *,
        chat_id: int,
        cluster_key: str,
        shadow_mode: bool,
        auto_ban_enabled: bool,
        similarity: float,
        message_count: int,
        unique_users: int,
        affected_user_ids: list[int],
        affected_message_ids: list[int],
        deleted_messages: int,
        banned_users: int,
    ) -> RaidIncidentRecord:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(RaidIncidentRecord)
                    .where(
                        RaidIncidentRecord.chat_id == chat_id,
                        RaidIncidentRecord.cluster_key == cluster_key,
                        RaidIncidentRecord.status.in_(
                            {"active", "shadow"}
                        ),
                    )
                    .order_by(RaidIncidentRecord.id.desc())
                    .limit(1)
                )

                record = result.scalar_one_or_none()

                if record is None:
                    record = RaidIncidentRecord(
                        incident_key=(
                            "RAID-" + uuid4().hex[:8].upper()
                        ),
                        chat_id=chat_id,
                        cluster_key=cluster_key,
                        status=(
                            "shadow" if shadow_mode else "active"
                        ),
                        shadow_mode=shadow_mode,
                        auto_ban_enabled=auto_ban_enabled,
                        similarity=similarity,
                        message_count=message_count,
                        unique_users=unique_users,
                        deleted_messages=deleted_messages,
                        banned_users=banned_users,
                        affected_user_ids_json=json.dumps(
                            sorted(set(affected_user_ids))
                        ),
                        affected_message_ids_json=json.dumps(
                            sorted(set(affected_message_ids))
                        ),
                    )
                    session.add(record)
                else:
                    existing_users = set(
                        json.loads(
                            record.affected_user_ids_json or "[]"
                        )
                    )
                    existing_messages = set(
                        json.loads(
                            record.affected_message_ids_json or "[]"
                        )
                    )
                    existing_users.update(affected_user_ids)
                    existing_messages.update(affected_message_ids)

                    record.shadow_mode = shadow_mode
                    record.auto_ban_enabled = auto_ban_enabled
                    record.status = (
                        "shadow" if shadow_mode else "active"
                    )
                    record.similarity = max(
                        record.similarity,
                        similarity,
                    )
                    record.message_count = max(
                        record.message_count,
                        message_count,
                    )
                    record.unique_users = max(
                        record.unique_users,
                        unique_users,
                    )
                    record.deleted_messages = max(
                        record.deleted_messages,
                        deleted_messages,
                    )
                    record.banned_users = max(
                        record.banned_users,
                        banned_users,
                    )
                    record.affected_user_ids_json = json.dumps(
                        sorted(existing_users)
                    )
                    record.affected_message_ids_json = json.dumps(
                        sorted(existing_messages)
                    )
                    record.updated_at = utcnow()

                await session.flush()
                return record

    async def get_raid_incident(
        self,
        incident_id: int,
    ) -> RaidIncidentRecord | None:
        async with self.session_factory() as session:
            return await session.get(
                RaidIncidentRecord,
                incident_id,
            )

    async def latest_raid_incident(
        self,
        *,
        chat_id: int,
    ) -> RaidIncidentRecord | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RaidIncidentRecord)
                .where(
                    RaidIncidentRecord.chat_id
                    == chat_id
                )
                .order_by(
                    RaidIncidentRecord.created_at.desc(),
                    RaidIncidentRecord.id.desc(),
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def mark_raid_unbanned(
        self,
        *,
        incident_id: int,
        unbanned_user_ids: list[int],
    ) -> RaidIncidentRecord | None:
        async with self.session_factory() as session:
            async with session.begin():
                record = await session.get(
                    RaidIncidentRecord,
                    incident_id,
                )
                if record is None:
                    return None

                existing = set(
                    json.loads(
                        record.unbanned_user_ids_json
                        or "[]"
                    )
                )
                existing.update(unbanned_user_ids)
                record.unbanned_user_ids_json = json.dumps(
                    sorted(existing)
                )
                record.status = "rolled_back"
                record.updated_at = utcnow()
                await session.flush()
                return record
