from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MessageRecord(Base):
    __tablename__ = "messages"

    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "telegram_message_id",
            name="uq_chat_telegram_message",
        ),
        Index(
            "ix_messages_chat_date",
            "chat_id",
            "telegram_date",
        ),
        Index(
            "ix_messages_chat_user_date",
            "chat_id",
            "user_id",
            "telegram_date",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    telegram_message_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    username: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    full_name: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    chat_title: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    content_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    raw_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    normalized_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    is_edited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    is_forwarded: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    reply_to_message_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    telegram_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class ModerationEventRecord(Base):
    """
    Фундамент audit trail.

    Пока ModGuard ещё не выполняет реальные moderation actions,
    но таблицу создаём сейчас, чтобы потом не переделывать архитектуру.
    """

    __tablename__ = "moderation_events"

    __table_args__ = (
        Index(
            "ix_moderation_events_chat_date",
            "chat_id",
            "created_at",
        ),
        Index(
            "ix_moderation_events_user_date",
            "target_user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    event_key: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    telegram_message_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    target_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    action: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    category: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    severity: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    autonomous: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    reversible: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    reversed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    reversed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    metadata_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )