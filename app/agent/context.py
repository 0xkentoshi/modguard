from aiogram.types import Message

from app.agent.schemas import (
    MessageContext,
    MessageSnapshot,
    ModerationHistoryItem,
)
from app.database.models import (
    MessageRecord,
    ModerationEventRecord,
)
from app.database.repository import (
    AuditRepository,
    MessageRepository,
)
from app.moderation.signals import build_behavior_signals
from app.utils.datetime_utils import ensure_utc_datetime
from app.utils.text_normalization import analyze_text


def record_to_snapshot(
    record: MessageRecord,
) -> MessageSnapshot:
    return MessageSnapshot(
        db_id=record.id,
        telegram_message_id=record.telegram_message_id,
        chat_id=record.chat_id,
        user_id=record.user_id,
        username=record.username,
        full_name=record.full_name,
        content_type=record.content_type,
        raw_text=record.raw_text,
        normalized_text=record.normalized_text,
        is_edited=record.is_edited,
        is_forwarded=record.is_forwarded,
        reply_to_message_id=record.reply_to_message_id,
        telegram_date=ensure_utc_datetime(
            record.telegram_date
        ),
    )


def event_to_history_item(
    event: ModerationEventRecord,
) -> ModerationHistoryItem:
    return ModerationHistoryItem(
        event_key=event.event_key,
        action=event.action,
        category=event.category,
        severity=event.severity,
        confidence=event.confidence,
        reason=event.reason,
        autonomous=event.autonomous,
        reversed=event.reversed,
        created_at=ensure_utc_datetime(
            event.created_at
        ),
    )


def get_message_text(
    message: Message,
) -> str:
    if message.text:
        return message.text

    if message.caption:
        return message.caption

    return ""


def get_content_type(
    message: Message,
) -> str:
    content_type = message.content_type

    if hasattr(content_type, "value"):
        return str(content_type.value)

    return str(content_type)


def get_telegram_date(
    message: Message,
    *,
    edited: bool,
):
    if (
        edited
        and message.edit_date is not None
    ):
        source_date = message.edit_date
    else:
        source_date = message.date

    return ensure_utc_datetime(
        source_date
    )


def telegram_message_to_snapshot(
    message: Message,
) -> MessageSnapshot:
    text = get_message_text(message)
    signals = analyze_text(text)
    user = message.from_user

    return MessageSnapshot(
        db_id=0,
        telegram_message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=(
            user.id
            if user
            else None
        ),
        username=(
            f"@{user.username}"
            if user and user.username
            else None
        ),
        full_name=(
            user.full_name
            if user
            else None
        ),
        content_type=get_content_type(message),
        raw_text=signals.raw_text,
        normalized_text=signals.normalized_text,
        is_edited=False,
        is_forwarded=(
            message.forward_origin is not None
        ),
        reply_to_message_id=(
            message.reply_to_message.message_id
            if message.reply_to_message
            else None
        ),
        telegram_date=ensure_utc_datetime(
            message.date
        ),
    )


def reported_target_context(
    source: MessageContext,
) -> MessageContext | None:
    target = source.reply_target_message

    if target is None:
        return None

    signals = analyze_text(
        target.raw_text
    )

    # Re-reviewing the target: target becomes CURRENT MESSAGE for policy.
    # Reporter text is context only.
    behavior = build_behavior_signals(
        current=target,
        text_signals=signals,
        user_history=[],
    )

    return MessageContext(
        current_message=target,
        text_signals=signals,
        behavior_signals=behavior,
        recent_chat_messages=source.recent_chat_messages,
        recent_user_messages=[],
        user_moderation_history=[],
        reply_target_message=None,
    )


class MessageContextBuilder:
    def __init__(
        self,
        repository: MessageRepository,
        audit_repository: AuditRepository | None = None,
        *,
        chat_history_limit: int = 20,
        user_history_limit: int = 10,
        moderation_history_limit: int = 10,
    ):
        self.repository = repository
        self.audit_repository = audit_repository
        self.chat_history_limit = chat_history_limit
        self.user_history_limit = user_history_limit
        self.moderation_history_limit = moderation_history_limit

    async def build(
        self,
        message: Message,
        *,
        edited: bool = False,
    ) -> MessageContext:
        raw_text = get_message_text(message)
        text_signals = analyze_text(raw_text)

        user = message.from_user

        user_id = (
            user.id
            if user
            else None
        )

        username = None
        full_name = None

        if user:
            username = (
                f"@{user.username}"
                if user.username
                else None
            )
            full_name = user.full_name

        reply_to_message_id = None
        reply_target = None

        if message.reply_to_message:
            reply_to_message_id = (
                message.reply_to_message.message_id
            )

            stored_target = (
                await self.repository.get_message(
                    message.chat.id,
                    reply_to_message_id,
                )
            )

            if stored_target is not None:
                reply_target = record_to_snapshot(
                    stored_target
                )
            else:
                reply_target = telegram_message_to_snapshot(
                    message.reply_to_message
                )

        telegram_date = get_telegram_date(
            message,
            edited=edited,
        )

        stored = await self.repository.upsert_message(
            telegram_message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=user_id,
            username=username,
            full_name=full_name,
            chat_title=message.chat.title,
            content_type=get_content_type(message),
            raw_text=text_signals.raw_text,
            normalized_text=text_signals.normalized_text,
            is_edited=edited,
            is_forwarded=(
                message.forward_origin is not None
            ),
            reply_to_message_id=reply_to_message_id,
            telegram_date=telegram_date,
        )

        current = record_to_snapshot(stored)

        chat_history_records = (
            await self.repository.get_recent_chat_messages(
                chat_id=message.chat.id,
                limit=self.chat_history_limit,
                exclude_telegram_message_id=message.message_id,
            )
        )

        recent_chat = [
            record_to_snapshot(record)
            for record in chat_history_records
        ]

        recent_user: list[
            MessageSnapshot
        ] = []

        moderation_history: list[
            ModerationHistoryItem
        ] = []

        if user_id is not None:
            user_history_records = (
                await self.repository.get_recent_user_messages(
                    chat_id=message.chat.id,
                    user_id=user_id,
                    limit=self.user_history_limit,
                    exclude_telegram_message_id=message.message_id,
                )
            )

            recent_user = [
                record_to_snapshot(record)
                for record in user_history_records
            ]

            if self.audit_repository is not None:
                events = (
                    await self.audit_repository.get_effective_user_events(
                        chat_id=message.chat.id,
                        target_user_id=user_id,
                        limit=self.moderation_history_limit,
                    )
                )

                moderation_history = [
                    event_to_history_item(event)
                    for event in events
                ]

        behavior = build_behavior_signals(
            current=current,
            text_signals=text_signals,
            user_history=recent_user,
        )

        return MessageContext(
            current_message=current,
            text_signals=text_signals,
            behavior_signals=behavior,
            recent_chat_messages=recent_chat,
            recent_user_messages=recent_user,
            user_moderation_history=moderation_history,
            reply_target_message=reply_target,
        )
