from pydantic import BaseModel, Field

from app.agent.schemas import (
    ModerationAction,
    ModerationCategory,
    ModerationSeverity,
)


class FeedbackReviewDecision(BaseModel):
    """
    Deep semantic judgment about whether stored human decisions
    genuinely apply to the current ambiguous case.
    """

    relevant: bool = False

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    matched_feedback_ids: list[int] = Field(
        default_factory=list,
        max_length=8,
    )

    recommended_action: ModerationAction

    category: ModerationCategory
    severity: ModerationSeverity

    current_message_violation: bool

    reason: str = Field(
        min_length=1,
        max_length=500,
    )

    current_message_evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
    )


class ShadowFeedbackInterpretation(BaseModel):
    """Structured interpretation of a moderator's free-text Shadow correction."""

    corrected_action: ModerationAction
    category: ModerationCategory
    severity: ModerationSeverity
    current_message_violation: bool

    summary: str = Field(
        min_length=1,
        max_length=600,
    )

    local_rule: str = Field(
        min_length=1,
        max_length=800,
    )

    relationship_relevant: bool = False
    relationship_note: str = Field(
        default="",
        max_length=500,
    )
    apply_to_same_pair: bool = False

    unsupported_assumptions: list[str] = Field(
        default_factory=list,
        max_length=5,
    )
