from pathlib import Path


def test_core_provenance_is_frozen_before_community_overlay():
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    # Three overlay-capable paths freeze independent Core snapshots:
    # reported target, report-first, and ordinary moderation.
    assert "target_core_decision = target_decision.model_copy(deep=True)" in handlers
    assert "target_core_policy = target_policy.model_copy(deep=True)" in handlers
    assert handlers.count("core_decision = decision.model_copy(deep=True)") >= 2
    assert handlers.count("core_policy = policy.model_copy(deep=True)") >= 2

    # Runtime logs must preserve all three stages instead of labelling an
    # effective Community Policy action as the raw LLM/Core recommendation.
    assert handlers.count("core=%s | policy=%s | effective=%s | ") >= 2
    assert '"LLM=%s | policy=%s | "' not in handlers


def test_protected_feedback_rejection_is_not_called_correct_action():
    admin = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")

    assert 'save_outcome == "protected_audit_only"' in admin
    assert "Requested correction:" in admin
    assert "Protected Core blocked this policy change." in admin
    assert "Feedback saved.</b> Correct action:" not in admin
    assert "save_outcome=promotion" in admin
    assert 'save_outcome="confirmed"' in admin
