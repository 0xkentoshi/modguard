from types import SimpleNamespace

import pytest

from app.moderation.safety_circuit import SafetyCircuitBreaker


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


class FakeRepository:
    def __init__(self):
        self.shadow = {}
        self.set_calls = []

    async def get_chat_settings(self, chat_id):
        return SimpleNamespace(shadow_mode=bool(self.shadow.get(int(chat_id), False)))

    async def set_shadow(self, chat_id, enabled):
        self.shadow[int(chat_id)] = bool(enabled)
        self.set_calls.append((int(chat_id), bool(enabled)))
        return bool(enabled)


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    async def notify_system_alert(self, *, chat_id, title, body):
        self.alerts.append((chat_id, title, body))
        return True


class FakeDashboard:
    def __init__(self):
        self.refreshes = []

    def request_refresh(self, chat_id):
        self.refreshes.append(int(chat_id))


def breaker(*, max_destructive=30, max_punitive=8, max_failures=3, max_pipeline=3):
    clock = FakeClock()
    repo = FakeRepository()
    notifier = FakeNotifier()
    dashboard = FakeDashboard()
    circuit = SafetyCircuitBreaker(
        repository=repo,
        notifier=notifier,
        dashboard_service=dashboard,
        action_window_seconds=60,
        max_destructive_actions=max_destructive,
        max_punitive_actions=max_punitive,
        failure_window_seconds=120,
        max_execution_failures=max_failures,
        max_pipeline_failures=max_pipeline,
        clock=clock,
    )
    return circuit, clock, repo, notifier, dashboard


@pytest.mark.asyncio
async def test_punitive_burst_forces_persistent_shadow_before_next_action():
    circuit, _, repo, notifier, dashboard = breaker(max_punitive=2)
    chat = -1001

    assert await circuit.preflight(chat_id=chat, planned_action="mute") is True
    await circuit.record_execution(chat_id=chat, action="mute", success=True)
    assert await circuit.preflight(chat_id=chat, planned_action="ban") is True
    await circuit.record_execution(chat_id=chat, action="ban", success=True)

    assert await circuit.preflight(chat_id=chat, planned_action="mute") is False
    assert repo.shadow[chat] is True
    assert len(notifier.alerts) == 1
    assert "punitive action burst" in notifier.alerts[0][2]
    assert dashboard.refreshes == [chat]


@pytest.mark.asyncio
async def test_delete_storm_trips_destructive_limit():
    circuit, _, repo, notifier, _ = breaker(max_destructive=3, max_punitive=99)
    chat = -1002
    for _ in range(3):
        assert await circuit.preflight(chat_id=chat, planned_action="delete") is True
        await circuit.record_execution(chat_id=chat, action="delete", success=True)

    assert await circuit.preflight(chat_id=chat, planned_action="delete") is False
    assert repo.shadow[chat] is True
    assert "destructive action burst" in notifier.alerts[0][2]


@pytest.mark.asyncio
async def test_old_action_burst_expires_from_window():
    circuit, clock, repo, notifier, _ = breaker(max_punitive=2)
    chat = -1003
    for _ in range(2):
        await circuit.record_execution(chat_id=chat, action="mute", success=True)
    clock.advance(61)

    assert await circuit.preflight(chat_id=chat, planned_action="mute") is True
    assert repo.shadow.get(chat) is not True
    assert notifier.alerts == []


@pytest.mark.asyncio
async def test_repeated_execution_failures_force_shadow():
    circuit, _, repo, notifier, _ = breaker(max_failures=3)
    chat = -1004
    for _ in range(2):
        await circuit.record_execution(
            chat_id=chat,
            action=None,
            success=False,
            had_failure=True,
        )
        assert repo.shadow.get(chat) is not True

    await circuit.record_execution(
        chat_id=chat,
        action=None,
        success=False,
        had_failure=True,
    )
    assert repo.shadow[chat] is True
    assert "execution failures" in notifier.alerts[0][2]


@pytest.mark.asyncio
async def test_repeated_pipeline_failures_force_shadow():
    circuit, _, repo, notifier, _ = breaker(max_pipeline=2)
    chat = -1005

    await circuit.record_pipeline_failure(
        chat_id=chat,
        component="message_pipeline",
        error=RuntimeError("model timeout"),
    )
    assert repo.shadow.get(chat) is not True

    await circuit.record_pipeline_failure(
        chat_id=chat,
        component="message_pipeline",
        error=RuntimeError("model timeout"),
    )
    assert repo.shadow[chat] is True
    assert "pipeline instability" in notifier.alerts[0][2]


@pytest.mark.asyncio
async def test_already_shadowed_chat_does_not_spam_repeat_alerts():
    circuit, _, repo, notifier, _ = breaker(max_punitive=1)
    chat = -1006
    repo.shadow[chat] = True
    await circuit.record_execution(chat_id=chat, action="ban", success=True)

    assert await circuit.preflight(chat_id=chat, planned_action="ban") is False
    assert notifier.alerts == []
