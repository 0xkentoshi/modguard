from pathlib import Path


def test_shadow_alert_exposes_agree_and_disagree_only_for_feedback_choice():
    source = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert 'text="✅ Agree"' in source
    assert 'text="❌ Disagree"' in source
    # The first disagreement step is intentionally free-text, not a taxonomy menu.
    assert 'callback_data=f"mg:sdisagree:{shadow_case.id}"' in source


def test_disagreement_requires_free_text_and_explicit_save_confirmation():
    handlers = Path("app/bot/admin_handlers.py").read_text(encoding="utf-8")
    assert "Explain in one normal message" in handlers
    assert "interpret_shadow_feedback" in handlers
    assert 'callback_data=f"mg:sfsave:{case_id}"' in handlers
    assert "save_shadow_feedback_interpretation" in handlers
    assert "confirm_shadow_feedback" in handlers


def test_unconfirmed_shadow_corrections_are_not_feedback_memory():
    repo = Path("app/admin/control_repository.py").read_text(encoding="utf-8")
    assert 'case.status = "awaiting_confirmation"' in repo
    assert 'ShadowFeedbackRecord.status == "confirmed"' in repo


def test_shadow_feedback_is_chat_scoped_and_hard_safety_remains_protected():
    repo = Path("app/admin/control_repository.py").read_text(encoding="utf-8")
    service = Path("app/feedback/service.py").read_text(encoding="utf-8")
    assert "ShadowFeedbackRecord.chat_id == chat_id" in repo
    assert "HARD_SAFETY_CATEGORIES" in service
    assert 'review.recommended_action\n            == "allow"' in service


def test_relationship_signal_is_weak_not_friendship_permission():
    prompts = Path("app/agent/prompts.py").read_text(encoding="utf-8")
    assert "weak chat-local familiarity signal" in prompts
    assert "It is NOT proof of friendship" in prompts
    assert "never use familiarity to ignore a clear request to stop" in prompts


def test_same_pair_moderator_feedback_can_be_prioritized():
    service = Path("app/feedback/service.py").read_text(encoding="utf-8")
    feedback_prompts = Path("app/feedback/prompts.py").read_text(encoding="utf-8")
    assert "same_pair=self._same_user_pair" in service
    assert "same_user_pair" in feedback_prompts
    assert "relationship_note" in feedback_prompts
