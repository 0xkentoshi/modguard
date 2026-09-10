import asyncio
from contextlib import (
    asynccontextmanager,
)


class ChatLockManager:
    def __init__(
        self,
    ):
        self._locks: dict[
            int,
            asyncio.Lock,
        ] = {}

        self._manager_lock = (
            asyncio.Lock()
        )

    async def get_lock(
        self,
        chat_id: int,
    ) -> asyncio.Lock:
        async with self._manager_lock:
            lock = self._locks.get(
                chat_id
            )

            if lock is None:
                lock = asyncio.Lock()

                self._locks[
                    chat_id
                ] = lock

            return lock

    @asynccontextmanager
    async def lock(
        self,
        chat_id: int,
    ):
        lock = (
            await self.get_lock(
                chat_id
            )
        )

        async with lock:
            yield