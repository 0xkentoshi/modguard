from typing import Literal

from pydantic import BaseModel, Field

from app.agent.schemas import ModerationAction


EnforcementTier = Literal[
    "light",
    "medium",
    "heavy",
]


class CommunityPolicyRule(BaseModel):
    rule_id: str = Field(
        default="",
        max_length=24,
    )
    title: str = Field(
        min_length=1,
        max_length=100,
    )
    condition: str = Field(
        min_length=1,
        max_length=600,
    )
    action: ModerationAction
    enforcement_tier: EnforcementTier | None = None
    # Kept for backwards compatibility with stored v1 rules. Runtime mute
    # duration is always loaded from the selected chat settings.
    mute_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
    )
    exceptions: list[str] = Field(
        default_factory=list,
        max_length=6,
    )


class CommunityPolicyCompilation(BaseModel):
    rules: list[CommunityPolicyRule] = Field(
        default_factory=list,
        max_length=20,
    )
    summary: str = Field(
        min_length=1,
        max_length=800,
    )
    changes: list[str] = Field(
        default_factory=list,
        max_length=12,
    )
    ignored_core_conflicts: list[str] = Field(
        default_factory=list,
        max_length=8,
    )


class CommunityPolicyTriage(BaseModel):
    route: Literal[
        "no_match",
        "deep",
    ]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )
    reason: str = Field(
        min_length=1,
        max_length=260,
    )


class CommunityPolicyMatch(BaseModel):
    matched: bool
    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )
    winning_rule_id: str | None = Field(
        default=None,
        max_length=24,
    )
    matched_rule_ids: list[str] = Field(
        default_factory=list,
        max_length=8,
    )
    ambiguous: bool = False
    reason: str = Field(
        min_length=1,
        max_length=600,
    )
    current_message_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )
