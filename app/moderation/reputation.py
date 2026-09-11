from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.schemas import MessageContext


LIGHT_REPUTATION_CATEGORIES = {
    "spam",
    "flood",
    "harassment",
}


def apply_light_reputation_decay(
    context: MessageContext,
    *,
    hours: int,
    now: datetime | None = None,
) -> MessageContext:
    """Remove expired LIGHT moderation history before both AI and policy gates.

    This prevents stale minor offenses from biasing the LLM itself, not only the
    deterministic escalation ladder. MEDIUM/HEAVY history is intentionally kept.
    ``hours <= 0`` disables decay for communities that want a strict permanent
    LIGHT history.
    """

    hours = int(hours)
    if hours <= 0:
        return context.model_copy(
            update={"light_offense_decay_hours": 0}
        )

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    kept = []
    for event in context.user_moderation_history or []:
        if event.category not in LIGHT_REPUTATION_CATEGORIES:
            kept.append(event)
            continue

        created_at = event.created_at
        if created_at is None:
            # Missing time on a LIGHT reputation event is not enough evidence
            # to escalate a future user action.
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        if created_at >= cutoff:
            kept.append(event)

    return context.model_copy(
        update={
            "light_offense_decay_hours": hours,
            "user_moderation_history": kept,
        }
    )
