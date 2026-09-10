import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    MessageRecord,
    ModerationEventRecord,
)
from app.utils.datetime_utils import ensure_utc_datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        self.session_factory = session_factory

    async def upsert_message(
        self,
        *,
        telegram_message_id: int,
        chat_id: int,
        user_id: int | None,
        username: str | None,
        full_name: str | None,
        chat_title: str | None,
        content_type: str,
        raw_text: str,
        normalized_text: str,
        is_edited: bool,
        is_forwarded: bool,
        reply_to_message_id: int | None,
        telegram_date: datetime | int | float,
    ) -> MessageRecord:
        safe_telegram_date = ensure_utc_datetime(
            telegram_date
        )

        async with self.session_factory() as session:
            async with session.begin():
                statement = select(MessageRecord).where(
                    MessageRecord.chat_id == chat_id,
                    MessageRecord.telegram_message_id
                    == telegram_message_id,
                )

                result = await session.execute(statement)
                record = result.scalar_one_or_none()

                if record is None:
                    record = MessageRecord(
                        telegram_message_id=telegram_message_id,
                        chat_id=chat_id,
                        user_id=user_id,
                        username=username,
                        full_name=full_name,
                        chat_title=chat_title,
                        content_type=content_type,
                        raw_text=raw_text,
                        normalized_text=normalized_text,
                        is_edited=is_edited,
                        is_forwarded=is_forwarded,
                        reply_to_message_id=reply_to_message_id,
                        telegram_date=safe_telegram_date,
                    )
                    session.add(record)

                else:
                    record.user_id = user_id
                    record.username = username
                    record.full_name = full_name
                    record.chat_title = chat_title
                    record.content_type = content_type
                    record.raw_text = raw_text
                    record.normalized_text = normalized_text
                    record.is_edited = (
                        record.is_edited or is_edited
                    )
                    record.is_forwarded = is_forwarded
                    record.reply_to_message_id = reply_to_message_id
                    record.telegram_date = safe_telegram_date
                    record.updated_at = utcnow()

            return record

    async def get_message(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> MessageRecord | None:
        async with self.session_factory() as session:
            statement = select(MessageRecord).where(
                MessageRecord.chat_id == chat_id,
                MessageRecord.telegram_message_id
                == telegram_message_id,
            )

            result = await session.execute(statement)
            return result.scalar_one_or_none()

    async def get_recent_chat_messages(
        self,
        *,
        chat_id: int,
        limit: int = 20,
        exclude_telegram_message_id: int | None = None,
    ) -> list[MessageRecord]:
        async with self.session_factory() as session:
            statement = select(MessageRecord).where(
                MessageRecord.chat_id == chat_id
            )

            if exclude_telegram_message_id is not None:
                statement = statement.where(
                    MessageRecord.telegram_message_id
                    != exclude_telegram_message_id
                )

            statement = (
                statement
                .order_by(
                    MessageRecord.telegram_date.desc(),
                    MessageRecord.id.desc(),
                )
                .limit(limit)
            )

            result = await session.execute(statement)
            records = list(result.scalars().all())
            records.reverse()
            return records

    async def get_recent_user_messages(
        self,
        *,
        chat_id: int,
        user_id: int,
        limit: int = 10,
        exclude_telegram_message_id: int | None = None,
    ) -> list[MessageRecord]:
        async with self.session_factory() as session:
            statement = select(MessageRecord).where(
                MessageRecord.chat_id == chat_id,
                MessageRecord.user_id == user_id,
            )

            if exclude_telegram_message_id is not None:
                statement = statement.where(
                    MessageRecord.telegram_message_id
                    != exclude_telegram_message_id
                )

            statement = (
                statement
                .order_by(
                    MessageRecord.telegram_date.desc(),
                    MessageRecord.id.desc(),
                )
                .limit(limit)
            )

            result = await session.execute(statement)
            records = list(result.scalars().all())
            records.reverse()
            return records

    async def count_messages(self) -> int:
        async with self.session_factory() as session:
            statement = select(
                func.count(MessageRecord.id)
            )

            result = await session.execute(statement)
            return int(result.scalar_one())


class AuditRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        self.session_factory = session_factory

    async def create_event(
        self,
        *,
        chat_id: int,
        telegram_message_id: int | None,
        target_user_id: int | None,
        action: str,
        category: str | None = None,
        severity: str | None = None,
        confidence: float | None = None,
        reason: str = "",
        autonomous: bool = False,
        reversible: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ModerationEventRecord:
        event = ModerationEventRecord(
            event_key=f"MG-{uuid4().hex[:12].upper()}",
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            target_user_id=target_user_id,
            action=action,
            category=category,
            severity=severity,
            confidence=confidence,
            reason=reason,
            autonomous=autonomous,
            reversible=reversible,
            metadata_json=json.dumps(
                metadata or {},
                ensure_ascii=False,
            ),
        )

        async with self.session_factory() as session:
            async with session.begin():
                session.add(event)

            return event

    async def get_recent_events(
        self,
        *,
        chat_id: int | None = None,
        target_user_id: int | None = None,
        exclude_action: str | None = None,
        limit: int = 50,
    ) -> list[ModerationEventRecord]:
        async with self.session_factory() as session:
            statement = select(
                ModerationEventRecord
            )

            if chat_id is not None:
                statement = statement.where(
                    ModerationEventRecord.chat_id == chat_id
                )

            if target_user_id is not None:
                statement = statement.where(
                    ModerationEventRecord.target_user_id
                    == target_user_id
                )

            if exclude_action is not None:
                statement = statement.where(
                    ModerationEventRecord.action
                    != exclude_action
                )

            statement = (
                statement
                .order_by(
                    ModerationEventRecord.created_at.desc(),
                    ModerationEventRecord.id.desc(),
                )
                .limit(limit)
            )

            result = await session.execute(statement)
            return list(result.scalars().all())

    async def get_effective_user_events(
        self,
        *,
        chat_id: int,
        target_user_id: int,
        limit: int = 10,
    ) -> list[ModerationEventRecord]:
        """
        Only real, non-reversed moderation may influence future decisions.

        DRY RUN is model evaluation, not user reputation.
        """

        raw_events = await self.get_recent_events(
            chat_id=chat_id,
            target_user_id=target_user_id,
            exclude_action="allow",
            limit=max(
                limit * 5,
                50,
            ),
        )

        effective: list[
            ModerationEventRecord
        ] = []

        for event in raw_events:
            if event.reversed:
                continue

            try:
                metadata = json.loads(
                    event.metadata_json or "{}"
                )
            except Exception:
                metadata = {}

            if metadata.get("dry_run") is True:
                continue

            effective.append(event)

            if len(effective) >= limit:
                break

        return effective

    async def has_recent_equivalent_event(
        self,
        *,
        chat_id: int,
        target_user_id: int | None,
        action: str,
        category: str | None,
        seconds: int = 30,
    ) -> bool:
        """
        Used only for admin notification deduplication.

        Audit events are still written individually; this merely prevents
        repeated identical alerts from flooding administrator DMs.
        """

        threshold = utcnow() - timedelta(
            seconds=seconds
        )

        async with self.session_factory() as session:
            statement = select(
                ModerationEventRecord.id
            ).where(
                ModerationEventRecord.chat_id == chat_id,
                ModerationEventRecord.action == action,
                ModerationEventRecord.created_at >= threshold,
            )

            if target_user_id is None:
                statement = statement.where(
                    ModerationEventRecord.target_user_id.is_(None)
                )
            else:
                statement = statement.where(
                    ModerationEventRecord.target_user_id
                    == target_user_id
                )

            if category is None:
                statement = statement.where(
                    ModerationEventRecord.category.is_(None)
                )
            else:
                statement = statement.where(
                    ModerationEventRecord.category == category
                )

            statement = statement.limit(1)

            result = await session.execute(statement)
            return result.scalar_one_or_none() is not None

    async def mark_reversed(
        self,
        event_key: str,
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                statement = select(
                    ModerationEventRecord
                ).where(
                    ModerationEventRecord.event_key
                    == event_key
                )

                result = await session.execute(statement)
                event = result.scalar_one_or_none()

                if event is None:
                    return False

                event.reversed = True
                event.reversed_at = utcnow()

            return True
