import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.admin.control_repository import ControlRepository
from app.agent.schemas import MessageContext, ModerationDecision, PolicyEvaluation
from app.semantic_clustering.math import cosine_similarity
from app.semantic_clustering.ollama_embeddings import OllamaEmbeddingProvider


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SemanticPreparedSignal:
    vector: list[float]
    nearest_record: object | None
    similarity: float
    cluster_key: str | None


@dataclass(slots=True)
class SemanticObservationJob:
    context: MessageContext
    decision: ModerationDecision
    policy: PolicyEvaluation
    prepared: SemanticPreparedSignal | None = None


class SemanticClusterService:
    """
    Chat-scoped semantic campaign analysis.

    Observation never changes moderation. Raid Guard may consume a prepared
    signal through its own stricter policy layer.
    """

    def __init__(
        self,
        *,
        repository: ControlRepository,
        embedding_provider: OllamaEmbeddingProvider,
        similarity_threshold: float = 0.88,
        window_seconds: int = 600,
        min_cluster_size: int = 3,
        queue_size: int = 128,
        recent_limit: int = 160,
        enabled: bool = True,
    ):
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.similarity_threshold = max(
            0.0,
            min(1.0, float(similarity_threshold)),
        )
        self.window_seconds = max(60, int(window_seconds))
        self.min_cluster_size = max(2, int(min_cluster_size))
        self.recent_limit = max(10, int(recent_limit))
        self.enabled = bool(enabled)
        self._queue = asyncio.Queue(
            maxsize=max(8, int(queue_size))
        )
        self._worker_task: asyncio.Task | None = None
        self._closing = False

    @property
    def active(self) -> bool:
        return bool(
            self.enabled
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    async def warmup(self) -> bool:
        if not self.enabled:
            return False
        available = await self.embedding_provider.warmup()
        if not available:
            self.enabled = False
        return available

    async def start(self) -> None:
        if not self.enabled or self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(
            self._worker(),
            name="modguard-semantic-clustering",
        )
        logger.info(
            "Semantic clustering ready | observational=True | "
            "threshold=%.2f | window=%ss | min_cluster=%s",
            self.similarity_threshold,
            self.window_seconds,
            self.min_cluster_size,
        )

    @staticmethod
    def _decode_embedding(raw: str) -> list[float] | None:
        try:
            values = json.loads(raw)
            if not isinstance(values, list) or not values:
                return None
            return [float(value) for value in values]
        except Exception:
            return None

    async def prepare_signal(
        self,
        *,
        context: MessageContext,
    ) -> SemanticPreparedSignal | None:
        if not self.enabled:
            return None

        current = context.current_message
        text = current.raw_text.strip()
        if len(text) < 3:
            return None

        vector = await self.embedding_provider.embed(text)
        since = datetime.now(timezone.utc) - timedelta(
            seconds=self.window_seconds
        )
        recent = await self.repository.recent_semantic_observations(
            chat_id=current.chat_id,
            since=since,
            limit=self.recent_limit,
        )

        best_record = None
        best_similarity = 0.0
        for record in recent:
            if record.telegram_message_id == current.telegram_message_id:
                continue
            other_vector = self._decode_embedding(
                record.embedding_json
            )
            if other_vector is None:
                continue
            similarity = cosine_similarity(vector, other_vector)
            if similarity > best_similarity:
                best_similarity = similarity
                best_record = record

        cluster_key = None
        if (
            best_record is not None
            and best_similarity >= self.similarity_threshold
        ):
            cluster_key = (
                best_record.cluster_key
                or f"SC-{best_record.id:06d}"
            )

        return SemanticPreparedSignal(
            vector=vector,
            nearest_record=best_record,
            similarity=best_similarity,
            cluster_key=cluster_key,
        )

    def should_force_deep(
        self,
        prepared: SemanticPreparedSignal | None,
        *,
        threshold: float = 0.93,
    ) -> bool:
        """
        Compatibility method retained for older callers.

        Semantic similarity must never force ordinary per-message moderation.
        It remains available to Raid Guard/campaign analysis as observational
        evidence only. This hard False prevents a similar-looking harmless
        message from inheriting a previous scam/phishing verdict.
        """
        _ = prepared, threshold
        return False

    def enqueue(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        prepared: SemanticPreparedSignal | None = None,
    ) -> bool:
        if not self.enabled or self._closing:
            return False
        if len(context.current_message.raw_text.strip()) < 3:
            return False

        try:
            self._queue.put_nowait(
                SemanticObservationJob(
                    context=context,
                    decision=decision,
                    policy=policy,
                    prepared=prepared,
                )
            )
            return True
        except asyncio.QueueFull:
            logger.warning(
                "SEMANTIC OBSERVE DROP | reason=queue_full | chat=%s | message=%s",
                context.current_message.chat_id,
                context.current_message.telegram_message_id,
            )
            return False

    async def _worker(self) -> None:
        while True:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self.observe_once(
                    context=job.context,
                    decision=job.decision,
                    policy=job.policy,
                    prepared=job.prepared,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Semantic observation failed; moderation is unaffected"
                )
            finally:
                self._queue.task_done()

    async def observe_once(
        self,
        *,
        context: MessageContext,
        decision: ModerationDecision,
        policy: PolicyEvaluation,
        prepared: SemanticPreparedSignal | None = None,
    ) -> str | None:
        current = context.current_message
        text = current.raw_text.strip()
        if len(text) < 3 or not self.enabled:
            return None

        if prepared is None:
            prepared = await self.prepare_signal(
                context=context
            )
        if prepared is None:
            return None

        cluster_key = prepared.cluster_key
        best_record = prepared.nearest_record

        if (
            cluster_key is not None
            and best_record is not None
            and best_record.cluster_key is None
        ):
            await self.repository.assign_semantic_cluster(
                observation_id=best_record.id,
                cluster_key=cluster_key,
            )

        record = await self.repository.save_semantic_observation(
            chat_id=current.chat_id,
            telegram_message_id=current.telegram_message_id,
            user_id=current.user_id,
            message_text=text,
            decision_category=decision.category,
            final_action=policy.final_action,
            confidence=decision.confidence,
            current_violation=decision.current_message_violation,
            embedding_json=json.dumps(
                prepared.vector,
                separators=(",", ":"),
            ),
            cluster_key=cluster_key,
            nearest_similarity=(
                prepared.similarity
                if best_record is not None
                else None
            ),
        )

        if cluster_key is None:
            return None

        since = datetime.now(timezone.utc) - timedelta(
            seconds=self.window_seconds
        )
        rows = await self.repository.semantic_cluster_records(
            chat_id=current.chat_id,
            cluster_key=cluster_key,
            since=since,
            limit=self.recent_limit,
        )
        users = {
            row.user_id
            for row in rows
            if row.user_id is not None
        }

        if len(rows) >= self.min_cluster_size:
            logger.info(
                "SEMANTIC CLUSTER | chat=%s | campaign=%s | "
                "similarity=%.3f | messages=%s | users=%s | "
                "category=%s | action=%s | observational=True",
                current.chat_id,
                cluster_key,
                prepared.similarity,
                len(rows),
                len(users),
                decision.category,
                policy.final_action,
            )

        return record.cluster_key

    async def close(self) -> None:
        self._closing = True
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(
                    self._queue.join(),
                    timeout=2.0,
                )
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        await self.embedding_provider.close()
