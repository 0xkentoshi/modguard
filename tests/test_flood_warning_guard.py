from types import SimpleNamespace

from app.moderation.executor import ModerationExecutor


def test_sustained_flood_context_detects_high_frequency_other():
    executor = object.__new__(
        ModerationExecutor
    )

    context = SimpleNamespace(
        behavior_signals=SimpleNamespace(
            messages_last_60s=9
        )
    )

    decision = SimpleNamespace(
        category="other"
    )

    assert executor._sustained_flood_context(
        context=context,
        decision=decision,
    ) is True


def test_sustained_flood_context_does_not_flag_normal_rate():
    executor = object.__new__(
        ModerationExecutor
    )

    context = SimpleNamespace(
        behavior_signals=SimpleNamespace(
            messages_last_60s=4
        )
    )

    decision = SimpleNamespace(
        category="flood"
    )

    assert executor._sustained_flood_context(
        context=context,
        decision=decision,
    ) is False
