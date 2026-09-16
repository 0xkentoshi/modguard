from typing import Literal

from pydantic import BaseModel, Field

from app.agent.schemas import ModerationAction


EnforcementTier = Literal[
    "light",
    "medium",
    "heavy",
]


PolicyFamily = Literal[
    "security_fraud",
    "threat",
    "harassment",
    "spam_flood",
    "advertising",
    "adult_content",
    "impersonation",
    "evasion",
    "hate",
    "other",
]


class CommunityPolicyRule(BaseModel):
    rule_id: str = Field(
        default="",
        max_length=24,
    )
    source: Literal["manual", "shadow_feedback"] = "manual"
    source_ref: int | None = Field(
        default=None,
        ge=1,
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
    # Stable topic family used by Adaptive Shadow reconciliation. Older stored
    # rules validate as "other" for backwards compatibility.
    policy_family: PolicyFamily = "other"
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


class ProtectedFeedbackSafetyReview(BaseModel):
    """Independent safety classification run before learned policy promotion."""

    protected: bool
    category: Literal[
        "none",
        "scam",
        "phishing",
        "malicious_link",
        "threat",
    ] = "none"
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)
    current_message_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )


class LearnedAllowBoundaryReview(BaseModel):
    """Safety/context gate for learned harassment/banter ALLOW exceptions."""

    blocked: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)
    boundary_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )


class LearnedRuleReconciliation(BaseModel):
    """Decide whether a new learned rule adds, replaces, or conflicts."""

    operation: Literal[
        "add",
        "replace",
        "manual_conflict",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    replace_rule_ids: list[str] = Field(default_factory=list, max_length=8)
    manual_conflict_rule_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=500)
