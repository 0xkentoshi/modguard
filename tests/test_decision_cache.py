import asyncio

import pytest

from app.utils.decision_cache import AsyncDecisionCache


@pytest.mark.asyncio
async def test_cache_reuses_value():
    cache = AsyncDecisionCache[str](
        ttl_seconds=30
    )

    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "result"

    first, first_hit = (
        await cache.get_or_create(
            "key",
            factory,
        )
    )

    second, second_hit = (
        await cache.get_or_create(
            "key",
            factory,
        )
    )

    assert first == "result"
    assert second == "result"
    assert first_hit is False
    assert second_hit is True
    assert calls == 1


@pytest.mark.asyncio
async def test_singleflight_shares_concurrent_work():
    cache = AsyncDecisionCache[str](
        ttl_seconds=30
    )

    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "shared"

    results = await asyncio.gather(
        *[
            cache.get_or_create(
                "same",
                factory,
            )
            for _ in range(5)
        ]
    )

    assert calls == 1

    assert {
        value
        for value, _ in results
    } == {"shared"}
