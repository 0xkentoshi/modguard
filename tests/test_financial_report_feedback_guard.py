from types import SimpleNamespace
from app.feedback.service import ModeratorFeedbackService

def test_hard_safety_allow_feedback_is_not_eligible_precedent():
    unsafe=SimpleNamespace(moderator_action="allow",ai_category="scam")
    normal=SimpleNamespace(moderator_action="allow",ai_category="other")
    assert ModeratorFeedbackService._eligible_feedback_example(unsafe) is False
    assert ModeratorFeedbackService._eligible_feedback_example(normal) is True
