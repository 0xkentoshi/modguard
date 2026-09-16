from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.community_policy.service import CommunityPolicyService


def test_handlers_freeze_protected_core_before_community_overlay():
    """Protected Core provenance is frozen on paths that apply Community Policy."""
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    # v1.4.11 deliberately changed the pure-report route: a safe reporter is
    # metadata, not moderation content, so it bypasses both Protected Core and
    # Community Policy. The two paths that DO apply chat-local policy remain:
    # ordinary moderation and reported-target re-review. Both must keep the
    # v1.4.8 ordering: Core guard -> freeze Core -> Community Policy overlay.
    assert handlers.count("apply_core_guard(") >= 2
    assert handlers.count("core_guard_applied=True") >= 2

    main_marker = "await community_policy_service.apply_core_guard("
    core_snapshot = "core_decision = decision"
    overlay_marker = "await community_policy_service.apply_overlay("
    start = handlers.rfind(main_marker)
    assert start >= 0
    core_at = handlers.find(core_snapshot, start)
    overlay_at = handlers.find(overlay_marker, core_at)
    assert start < core_at < overlay_at

    report_start = handlers.index("# REPORT-FIRST ROUTE")
    report_end = handlers.index("# Ordinary moderation path.", report_start)
    report_branch = handlers[report_start:report_end]
    assert "REPORTER SAFE BYPASS" in report_branch
    assert "apply_core_guard" not in report_branch
    assert "apply_overlay" not in report_branch


@pytest.mark.asyncio
async def test_overlay_does_not_repeat_protected_review_when_core_is_already_guarded():
    repo = SimpleNamespace(get_active_community_policy=AsyncMock(return_value=None))
    service = CommunityPolicyService(
        repository=repo,
        deep_provider=SimpleNamespace(),
    )
    service._runtime_protected_guard = AsyncMock(
        side_effect=AssertionError("protected guard must not run twice")
    )

    decision = SimpleNamespace()
    policy = SimpleNamespace()
    context = SimpleNamespace(current_message=SimpleNamespace(chat_id=-100123))

    final_decision, final_policy = await service.apply_overlay(
        context=context,
        baseline_decision=decision,
        baseline_policy=policy,
        policy_gate=SimpleNamespace(),
        core_guard_applied=True,
    )

    assert final_decision is decision
    assert final_policy is policy
    service._runtime_protected_guard.assert_not_awaited()
