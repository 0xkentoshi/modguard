from dataclasses import dataclass, field

from app.semantic_clustering.service import SemanticPreparedSignal


@dataclass(slots=True)
class RaidGuardResult:
    triggered: bool = False
    incident_id: int | None = None
    incident_key: str | None = None
    cluster_key: str | None = None
    similarity: float = 0.0
    message_count: int = 0
    unique_users: int = 0
    hard_ratio: float = 0.0
    shadow: bool = False
    prepared: SemanticPreparedSignal | None = None
    deleted_messages: int = 0
    banned_users: int = 0
    affected_users: list[int] = field(default_factory=list)
    affected_messages: list[int] = field(default_factory=list)
