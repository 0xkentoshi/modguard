import asyncio
import time


class AdminAlertThrottle:
    """
    In-memory atomic notification throttle.

    It solves the race where 20 concurrent spam messages all check the DB
    before any of them has created an event and therefore all notify admin.

    Audit events are NOT suppressed.
    Only duplicate Telegram DM alerts are suppressed.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: int = 60,
    ):
        self.cooldown_seconds = cooldown_seconds

        self._lock = asyncio.Lock()

        self._last_sent: dict[
            str,
            float,
        ] = {}

        self._suppressed: dict[
            str,
            int,
        ] = {}

    async def reserve(
        self,
        key: str,
    ) -> tuple[bool, int]:
        """
        Returns:
          should_send
          suppressed_since_previous_window
        """

        now = time.monotonic()

        async with self._lock:
            last = self._last_sent.get(
                key
            )

            if (
                last is not None
                and (
                    now - last
                    < self.cooldown_seconds
                )
            ):
                self._suppressed[key] = (
                    self._suppressed.get(
                        key,
                        0,
                    )
                    + 1
                )

                return False, 0

            suppressed = (
                self._suppressed.pop(
                    key,
                    0,
                )
            )

            self._last_sent[key] = now

            return True, suppressed
