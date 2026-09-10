from pathlib import Path


def test_feedback_runs_before_community_policy_overlay():
    source = Path(
        "app/bot/handlers.py"
    ).read_text(
        encoding="utf-8"
    )

    feedback_pos = source.find(
        "feedback_service.refine_gray_case"
    )

    community_pos = source.find(
        "community_policy_service.apply_overlay"
    )

    assert feedback_pos >= 0
    assert community_pos >= 0
    assert feedback_pos < community_pos


def test_test_ticket_branch_returns_before_feedback_learning():
    source = Path(
        "app/bot/admin_handlers.py"
    ).read_text(
        encoding="utf-8"
    )

    test_branch = source.find(
        "if is_test_ticket(ticket)"
    )

    learn = source.find(
        "feedback_service.learn_from_ticket"
    )

    assert test_branch >= 0
    assert learn > test_branch
