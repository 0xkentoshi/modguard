import asyncio

import pytest

from app.utils.alert_throttle import (
    AdminAlertThrottle,
)


@pytest.mark.asyncio
async def test_only_one_concurrent_alert_is_reserved():
    throttle = AdminAlertThrottle(
        cooldown_seconds=60
    )

    results = await asyncio.gather(
        *[
            throttle.reserve(
                "chat:user:spam"
            )
            for _ in range(20)
        ]
    )

    assert sum(
        1
        for should_send, _
        in results
        if should_send
    ) == 1


@pytest.mark.asyncio
async def test_separate_categories_have_separate_alerts():
    throttle = AdminAlertThrottle(
        cooldown_seconds=60
    )

    spam = await throttle.reserve(
        "chat:user:spam"
    )

    threat = await throttle.reserve(
        "chat:user:threat"
    )

    assert spam[0] is True
    assert threat[0] is True
