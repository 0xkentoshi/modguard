from pathlib import Path


def test_raid_guard_runs_after_policy_layers_and_before_executor():
    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    feedback = source.find("feedback_service.refine_gray_case")
    community = source.find("community_policy_service.apply_overlay")
    raid = source.find("raid_guard_service.inspect_before_execution")
    executor = source.find("await moderation_executor.execute")

    assert feedback >= 0
    assert community > feedback
    assert raid > community
    assert executor > raid


def test_semantic_observation_happens_only_after_executor():
    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    executor = source.find("await moderation_executor.execute")
    observe = source.find("semantic_cluster_service.observe_once")
    enqueue = source.find("semantic_cluster_service.enqueue")

    assert executor >= 0
    assert (observe > executor) or (enqueue > executor)


def test_raid_guard_is_default_off_in_model():
    source = Path("app/admin/control_models.py").read_text(encoding="utf-8")
    assert "raid_guard_enabled" in source
    assert "default=False" in source
