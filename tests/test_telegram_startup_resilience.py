from types import SimpleNamespace

import pytest
from aiogram.exceptions import (
    TelegramNetworkError,
)
from aiogram.methods import GetMe

import app.main as main_module


class FakeBot:
    def __init__(
        self,
        *,
        fail_times: int,
    ):
        self.fail_times = fail_times
        self.calls = 0

    async def get_me(self):
        self.calls += 1

        if self.calls <= self.fail_times:
            raise TelegramNetworkError(
                method=GetMe(),
                message=(
                    "temporary network failure"
                ),
            )

        return SimpleNamespace(
            id=123,
            username="modguard_test",
        )


@pytest.mark.asyncio
async def test_startup_get_me_retries_transient_network_failure(
    monkeypatch,
):
    sleeps = []

    async def fake_sleep(
        seconds,
    ):
        sleeps.append(
            seconds
        )

    monkeypatch.setattr(
        main_module.asyncio,
        "sleep",
        fake_sleep,
    )

    bot = FakeBot(
        fail_times=2
    )

    me = (
        await main_module
        .telegram_get_me_with_retry(
            bot,
            attempts=4,
            base_delay_seconds=1.0,
        )
    )

    assert me.id == 123
    assert bot.calls == 3

    assert sleeps == [
        1.0,
        2.0,
    ]


@pytest.mark.asyncio
async def test_startup_get_me_reraises_after_retry_budget(
    monkeypatch,
):
    async def fake_sleep(
        _seconds,
    ):
        return None

    monkeypatch.setattr(
        main_module.asyncio,
        "sleep",
        fake_sleep,
    )

    bot = FakeBot(
        fail_times=10
    )

    with pytest.raises(
        TelegramNetworkError
    ):
        await (
            main_module
            .telegram_get_me_with_retry(
                bot,
                attempts=3,
                base_delay_seconds=0.0,
            )
        )

    assert bot.calls == 3


def test_main_cleanup_wraps_get_me_and_closes_telegram_session():
    from pathlib import Path

    source = Path(
        "app/main.py"
    ).read_text(
        encoding="utf-8"
    )

    get_me_pos = source.find(
        "me = await "
        "telegram_get_me_with_retry("
    )

    assert get_me_pos >= 0

    # Find the try that actually encloses the startup get_me call,
    # instead of relying on a brittle exact escaped-newline string.
    try_pos = source.rfind(
        "try:",
        0,
        get_me_pos,
    )

    finally_pos = source.find(
        "finally:",
        get_me_pos,
    )

    close_pos = source.find(
        "await bot.session.close()",
        finally_pos,
    )

    assert try_pos >= 0
    assert try_pos < get_me_pos
    assert finally_pos > get_me_pos
    assert close_pos > finally_pos

    # All local providers and DB should also be closed
    # inside the same cleanup region.
    assert (
        source.find(
            "await fast_provider.close()",
            finally_pos,
        )
        > finally_pos
    )

    assert (
        source.find(
            "await deep_provider.close()",
            finally_pos,
        )
        > finally_pos
    )

    assert (
        source.find(
            "await policy_compiler_provider.close()",
            finally_pos,
        )
        > finally_pos
    )

    assert (
        source.find(
            "await database.dispose()",
            finally_pos,
        )
        > finally_pos
    )
