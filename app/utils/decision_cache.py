import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar


ValueT = TypeVar("ValueT")


class AsyncDecisionCache(Generic[ValueT]):
    """
    Short TTL cache + single-flight.

    If the same user sends the exact same normalized message several times
    during a spam burst, only one LLM analysis is performed. Concurrent
    duplicates await the same task and then reuse its result.

    The cache never creates a moderation decision on its own.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 45,
    ):
        self.ttl_seconds = ttl_seconds

        self._values: dict[
            str,
            tuple[float, ValueT],
        ] = {}

        self._inflight: dict[
            str,
            asyncio.Task[ValueT],
        ] = {}

        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        key: str,
        factory: Callable[[], Awaitable[ValueT]],
    ) -> tuple[ValueT, bool]:
        now = time.monotonic()

        async with self._lock:
            cached = self._values.get(key)

            if cached is not None:
                expires_at, value = cached

                if expires_at > now:
                    return value, True

                self._values.pop(
                    key,
                    None,
                )

            task = self._inflight.get(
                key
            )

            if task is None:
                task = asyncio.create_task(
                    factory()
                )

                self._inflight[key] = task

        try:
            value = await task

        except Exception:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(
                        key,
                        None,
                    )
            raise

        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(
                    key,
                    None,
                )

                self._values[key] = (
                    time.monotonic()
                    + self.ttl_seconds,
                    value,
                )

        return value, False

    async def invalidate_prefix(self, prefix: str) -> int:
        """Drop cached decisions for one scoped key prefix.

        ModGuard cache keys start with ``<chat_id>:`` so owner-level community
        reset can invalidate one community without disturbing every other chat.
        In-flight tasks are not cancelled; the community reset barrier waits for
        active moderation pipelines before calling this method.
        """
        prefix = str(prefix)
        async with self._lock:
            keys = [key for key in self._values if key.startswith(prefix)]
            for key in keys:
                self._values.pop(key, None)
            return len(keys)
