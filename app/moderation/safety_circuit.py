from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from app.admin.control_repository import ControlRepository
from app.admin.dashboard import DashboardService
from app.admin.notifications import AdminNotifier


logger = logging.getLogger(__name__)


@dataclass
class _ChatSafetyState:
    destructive: deque[float]
    punitive: deque[float]
    execution_failures: deque[float]
    pipeline_failures: deque[float]


class SafetyCircuitBreaker:
    """Fail-closed guard for abnormal autonomous moderation behavior.

    The circuit is intentionally simple and conservative for Pilot Edition:
    - too many mute/ban decisions in a short window -> force SHADOW;
    - too many destructive actions in a short window -> force SHADOW;
    - repeated Telegram execution failures -> force SHADOW;
    - repeated moderation-pipeline exceptions -> force SHADOW.

    A trip is persistent because SHADOW is written to per-chat settings. There
    is no automatic reset: a human must inspect the chat and turn SHADOW off.
    Raid Guard is a separate explicit campaign subsystem and does not execute
    through this per-message circuit, so a confirmed raid is not accidentally
    counted as a runaway ordinary moderation loop.
    """

    DESTRUCTIVE = {"delete", "mute", "ban"}
    PUNITIVE = {"mute", "ban"}

    def __init__(
        self,
        *,
        repository: ControlRepository,
        notifier: AdminNotifier,
        dashboard_service: DashboardService | None = None,
        enabled: bool = True,
        action_window_seconds: int = 60,
        max_destructive_actions: int = 30,
        max_punitive_actions: int = 8,
        failure_window_seconds: int = 120,
        max_execution_failures: int = 3,
        max_pipeline_failures: int = 3,
        clock: Callable[[], float] | None = None,
    ):
        self.repository = repository
        self.notifier = notifier
        self.dashboard_service = dashboard_service
        self.enabled = bool(enabled)
        self.action_window_seconds = max(1, int(action_window_seconds))
        self.max_destructive_actions = max(1, int(max_destructive_actions))
        self.max_punitive_actions = max(1, int(max_punitive_actions))
        self.failure_window_seconds = max(1, int(failure_window_seconds))
        self.max_execution_failures = max(1, int(max_execution_failures))
        self.max_pipeline_failures = max(1, int(max_pipeline_failures))
        self.clock = clock or time.monotonic
        self._states: dict[int, _ChatSafetyState] = {}

    def _state(self, chat_id: int) -> _ChatSafetyState:
        chat_id = int(chat_id)
        state = self._states.get(chat_id)
        if state is None:
            state = _ChatSafetyState(
                destructive=deque(),
                punitive=deque(),
                execution_failures=deque(),
                pipeline_failures=deque(),
            )
            self._states[chat_id] = state
        return state

    @staticmethod
    def _prune(queue: deque[float], *, now: float, window: int) -> None:
        cutoff = now - window
        while queue and queue[0] < cutoff:
            queue.popleft()

    def _prune_state(self, state: _ChatSafetyState, now: float) -> None:
        self._prune(
            state.destructive,
            now=now,
            window=self.action_window_seconds,
        )
        self._prune(
            state.punitive,
            now=now,
            window=self.action_window_seconds,
        )
        self._prune(
            state.execution_failures,
            now=now,
            window=self.failure_window_seconds,
        )
        self._prune(
            state.pipeline_failures,
            now=now,
            window=self.failure_window_seconds,
        )

    async def _trip(self, *, chat_id: int, reason: str) -> None:
        if not self.enabled:
            return

        chat_id = int(chat_id)
        try:
            current = await self.repository.get_chat_settings(chat_id)
            already_shadow = bool(current.shadow_mode)
        except Exception:
            already_shadow = False
            logger.exception(
                "SAFETY CIRCUIT could not read chat settings | chat=%s",
                chat_id,
            )

        try:
            await self.repository.set_shadow(chat_id, True)
        except Exception:
            logger.exception(
                "SAFETY CIRCUIT could not persist SHADOW | chat=%s",
                chat_id,
            )

        logger.critical(
            "SAFETY CIRCUIT TRIPPED | chat=%s | reason=%s",
            chat_id,
            reason,
        )

        if self.dashboard_service is not None:
            self.dashboard_service.request_refresh(chat_id)

        # If the chat was already in SHADOW, the circuit is already fail-closed;
        # avoid repeatedly spamming admins with the same operational alert.
        if already_shadow:
            return

        try:
            await self.notifier.notify_system_alert(
                chat_id=chat_id,
                title="🚨 MODGUARD AUTO-STOP",
                body=(
                    f"Live enforcement was automatically switched to SHADOW.\n"
                    f"Reason: {reason}\n\n"
                    "No further automatic delete/mute/ban actions will run until "
                    "a moderator inspects the community and manually disables SHADOW."
                ),
            )
        except Exception:
            logger.exception(
                "SAFETY CIRCUIT alert delivery failed | chat=%s",
                chat_id,
            )

    async def preflight(
        self,
        *,
        chat_id: int,
        planned_action: str,
    ) -> bool:
        """Return False and force SHADOW before the next abnormal action."""
        if not self.enabled or planned_action not in self.DESTRUCTIVE:
            return True

        now = self.clock()
        state = self._state(chat_id)
        self._prune_state(state, now)

        if (
            planned_action in self.PUNITIVE
            and len(state.punitive) >= self.max_punitive_actions
        ):
            await self._trip(
                chat_id=chat_id,
                reason=(
                    f"punitive action burst: {len(state.punitive)} mute/ban actions "
                    f"within {self.action_window_seconds}s"
                ),
            )
            return False

        if len(state.destructive) >= self.max_destructive_actions:
            await self._trip(
                chat_id=chat_id,
                reason=(
                    f"destructive action burst: {len(state.destructive)} actions "
                    f"within {self.action_window_seconds}s"
                ),
            )
            return False

        return True

    async def record_execution(
        self,
        *,
        chat_id: int,
        action: str | None,
        success: bool | None,
        had_failure: bool = False,
    ) -> None:
        if not self.enabled:
            return

        now = self.clock()
        state = self._state(chat_id)
        self._prune_state(state, now)

        if success is True and action in self.DESTRUCTIVE:
            state.destructive.append(now)
            if action in self.PUNITIVE:
                state.punitive.append(now)

        if had_failure:
            state.execution_failures.append(now)
            if len(state.execution_failures) >= self.max_execution_failures:
                await self._trip(
                    chat_id=chat_id,
                    reason=(
                        f"repeated Telegram execution failures: "
                        f"{len(state.execution_failures)} within "
                        f"{self.failure_window_seconds}s"
                    ),
                )

    async def record_pipeline_failure(
        self,
        *,
        chat_id: int,
        component: str,
        error: Exception | str,
    ) -> None:
        if not self.enabled:
            return

        now = self.clock()
        state = self._state(chat_id)
        self._prune_state(state, now)
        state.pipeline_failures.append(now)

        if len(state.pipeline_failures) >= self.max_pipeline_failures:
            await self._trip(
                chat_id=chat_id,
                reason=(
                    f"moderation pipeline instability in {component}: "
                    f"{len(state.pipeline_failures)} failures within "
                    f"{self.failure_window_seconds}s; last={type(error).__name__ if isinstance(error, Exception) else str(error)[:80]}"
                ),
            )

    def reset_runtime_counters(self, chat_id: int) -> None:
        """Clear only in-memory counters; persistent SHADOW is a human decision."""
        self._states.pop(int(chat_id), None)
