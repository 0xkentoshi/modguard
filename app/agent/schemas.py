from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.utils.text_normalization import TextSignals


ModerationCategory = Literal[
    "safe",
    "spam",
    "scam",
    "phishing",
    "malicious_link",
    "unsolicited_advertising",
    "flood",
    "harassment",
    "hate",
    "threat",
    "adult_content",
    "impersonation",
    "evasion_attempt",
    "other",
]

ModerationSeverity = Literal[
    "none",
    "low",
    "medium",
    "high",
    "critical",
]

ModerationAction = Literal[
    "allow",
    "warn",
    "delete",
    "mute",
    "ban",
    "escalate",
]


class MessageSnapshot(BaseModel):
    db_id: int
    telegram_message_id: int
    chat_id: int

    user_id: int | None = None
    username: str | None = None
    full_name: str | None = None

    content_type: str

    raw_text: str
    normalized_text: str

    is_edited: bool = False
    is_forwarded: bool = False

    reply_to_message_id: int | None = None

    telegram_date: datetime


class BehaviorSignals(BaseModel):
    has_urls: bool = False
    url_count: int = 0
    mention_count: int = 0
    messages_last_60s: int = 0
    repeated_recent_messages: int = 0
    uppercase_ratio: float = 0.0
    uppercase_ratio_without_urls: float = 0.0
    is_reply: bool = False
    is_edited: bool = False
    is_forwarded: bool = False


class ModerationHistoryItem(BaseModel):
    event_key: str
    action: str
    category: str | None = None
    severity: str | None = None
    confidence: float | None = None
    reason: str = ""
    autonomous: bool = False
    reversed: bool = False
    created_at: datetime


class MessageContext(BaseModel):
    current_message: MessageSnapshot

    text_signals: TextSignals
    behavior_signals: BehaviorSignals

    recent_chat_messages: list[MessageSnapshot] = Field(
        default_factory=list
    )

    recent_user_messages: list[MessageSnapshot] = Field(
        default_factory=list
    )

    user_moderation_history: list[ModerationHistoryItem] = Field(
        default_factory=list
    )

    # LIGHT offense reputation window for this community. 0 means no decay.
    # Spam/flood/harassment ladders use this; MEDIUM/HEAVY safety history does not.
    light_offense_decay_hours: int = Field(default=6, ge=0, le=8760)

    # The actual Telegram message being replied to, when available.
    reply_target_message: MessageSnapshot | None = None


class TriageDecision(BaseModel):
    route: Literal[
        "safe",
        "deep",
    ]

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason: str = Field(
        min_length=1,
        max_length=240,
    )

    # LLM semantic intent: current reply appears to report/flag the target.
    report_target: bool = False

    report_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
    )

    report_reason: str = Field(
        default="",
        max_length=300,
    )


class ModerationDecision(BaseModel):
    detected_language: str

    current_message_violation: bool

    category: ModerationCategory
    severity: ModerationSeverity

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    action: ModerationAction

    delete_message: bool = False

    mute_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
    )

    needs_human_review: bool = False

    conflict_context: Literal[
        "not_applicable",
        "clear_current_aggressor",
        "ambiguous_multi_party",
    ] = "not_applicable"

    reason: str = Field(
        min_length=1,
        max_length=800,
    )

    current_message_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )

    context_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )

    # The current message itself is a semantic report about reply target.
    # This does NOT make the reporter a violator.
    report_target: bool = False

    report_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
    )

    report_reason: str = Field(
        default="",
        max_length=300,
    )


class PolicyEvaluation(BaseModel):
    original_action: ModerationAction
    final_action: ModerationAction
    final_delete_message: bool = False
    final_mute_minutes: int | None = None
    autonomous: bool = False
    requires_human_review: bool = False
    policy_reason: str

    # Additive metadata. Core PolicyGate callers do not need to set these.
    source: Literal[
        "core",
        "community_policy",
    ] = "core"

    community_policy_version: int | None = None

    matched_community_rules: list[str] = Field(
        default_factory=list,
        max_length=8,
    )


class ExecutionResult(BaseModel):
    final_action: ModerationAction
    dry_run: bool
    executed: bool
    audit_event_key: str
    admin_notified: bool = False
    details: str = ""
