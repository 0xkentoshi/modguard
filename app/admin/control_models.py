from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatControlSettingsRecord(Base):
    __tablename__ = "chat_control_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shadow_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ChatMigrationRecord(Base):
    """
    Canonical Telegram chat-id migration (basic group -> supergroup).

    Telegram upgrades a basic group to a supergroup by assigning a new
    ``-100...`` chat id. Keeping the old id as an independent community
    causes duplicate Change-chat entries and makes mute/ban/unban calls hit
    an obsolete basic-group id. This table keeps the old id as an alias of
    the canonical supergroup id.
    """

    __tablename__ = "chat_migrations"

    old_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    new_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ManagedChatTombstoneRecord(Base):
    """
    Marks a previously managed Telegram chat as unavailable without deleting
    moderation history, tickets, bans, policies, or audit data.
    """

    __tablename__ = "managed_chat_tombstones"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ChatEnforcementSettingsRecord(Base):
    """
    Destructive live-action switches kept in a separate table.

    Separate storage avoids requiring an ALTER TABLE migration for existing
    ModGuard databases when new safety switches are introduced.
    """

    __tablename__ = "chat_enforcement_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    live_ban_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ChatMuteSettingsRecord(Base):
    """One mute duration per chat. Default: 60 minutes."""

    __tablename__ = "chat_mute_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mute_duration_minutes: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ChatReputationSettingsRecord(Base):
    """Per-chat decay window for LIGHT offense escalation.

    Only LIGHT reputation (spam/flood/harassment warning ladders) expires.
    MEDIUM/HEAVY safety history remains intact. A value of 0 means the
    LIGHT history never expires.
    """

    __tablename__ = "chat_reputation_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    light_offense_decay_hours: Mapped[int] = mapped_column(
        Integer, default=6, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ModerationTicketRecord(Base):
    __tablename__ = "moderation_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_key: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(
        BigInteger, index=True, nullable=True
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    message_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="open", index=True, nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resolution_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminDashboardStateRecord(Base):
    __tablename__ = "admin_dashboard_state"

    admin_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dashboard_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    selected_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    view: Mapped[str] = mapped_column(String(32), default="dashboard", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class CommunityPolicyVersionRecord(Base):
    """
    Immutable version history for per-chat custom moderation rules.

    These rules are an ADDITIVE overlay. They never replace ModGuard's core
    safety baseline.
    """

    __tablename__ = "community_policy_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CommunityPolicyDraftRecord(Base):
    """Persistent preview awaiting explicit admin confirmation."""

    __tablename__ = "community_policy_drafts"

    admin_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    changes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    ignored_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class TestArtifactRecord(Base):
    """
    Persistent registry of Telegram messages generated by ModGuard Test Mode.

    The registry lets "Clear tests" remove test messages/alerts even after
    several different QA actions were run.
    """

    __tablename__ = "test_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    managed_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class KnownModerationPatternRecord(Base):
    """
    Persistent memory of AI-confirmed malicious messages.

    This is NOT a keyword blacklist. An entry exists only after an LLM decision
    passed policy gates and a real destructive action succeeded.
    """

    __tablename__ = "known_moderation_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    decision_json: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ModeratorFeedbackRecord(Base):
    """
    Human moderator decisions learned from REAL moderation tickets.

    This is chat-scoped soft guidance for future ambiguous cases.
    TEST-* tickets are never stored here.
    """

    __tablename__ = "moderator_feedback"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    source_ticket_id: Mapped[int] = mapped_column(
        Integer,
        unique=True,
        index=True,
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    moderator_admin_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    message_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    ai_category: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    ai_severity: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    ai_confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    ai_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    moderator_action: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
    )

    policy_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
    )


class ShadowFeedbackRecord(Base):
    """
    One real Shadow-mode decision awaiting or containing moderator feedback.

    Confirmed rows are chat-scoped soft memory. Disagreements stay inert until
    the moderator explicitly confirms ModGuard's parsed interpretation.
    """

    __tablename__ = "shadow_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    counterpart_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    message_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    ai_action: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    moderator_admin_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    moderator_explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)

    corrected_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    corrected_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corrected_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    local_rule: Mapped[str] = mapped_column(Text, default="", nullable=False)
    relationship_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    apply_to_same_pair: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interpretation_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def moderator_action(self) -> str:
        return self.corrected_action or self.ai_action

    @property
    def feedback_note(self) -> str:
        return self.moderator_explanation



class ChatRaidSettingsRecord(Base):
    """Per-chat Raid Guard switch. OFF by default."""

    __tablename__ = "chat_raid_settings"

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    raid_guard_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class SemanticObservationRecord(Base):
    """
    Chat-scoped semantic fingerprint for campaign detection.

    This record is analysis evidence, never a direct moderation verdict.
    """

    __tablename__ = "semantic_observations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    telegram_message_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        index=True,
        nullable=True,
    )

    message_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    decision_category: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        nullable=True,
    )

    final_action: Mapped[str | None] = mapped_column(
        String(32),
        index=True,
        nullable=True,
    )

    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    current_violation: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    embedding_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    cluster_key: Mapped[str | None] = mapped_column(
        String(32),
        index=True,
        nullable=True,
    )

    nearest_similarity: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class RaidIncidentRecord(Base):
    """Persistent record of a detected hostile campaign and reversible bans."""

    __tablename__ = "raid_incidents"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    incident_key: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    cluster_key: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        index=True,
        nullable=False,
    )

    shadow_mode: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    auto_ban_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    similarity: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    message_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    unique_users: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    deleted_messages: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    banned_users: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    affected_user_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )

    affected_message_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )

    unbanned_user_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class AdminAlertArtifactRecord(Base):
    """Persistent admin-DM artifacts that may be cleaned without touching dashboard."""

    __tablename__ = "admin_alert_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    managed_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    admin_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ModerationBanRecord(Base):
    """Active bans created by ModGuard outside the raid-specific rollback."""

    __tablename__ = "moderation_bans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="autonomous", nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    unbanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)



class ModerationImmunityRecord(Base):
    """Per-chat full moderation immunity for trusted bots/service accounts."""

    __tablename__ = "moderation_immunity"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "subject_key",
            name="uq_moderation_immunity_chat_subject",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    subject_key: Mapped[str] = mapped_column(String(320), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    created_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
