import json

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.database.db import Database


@pytest.mark.asyncio
async def test_policy_draft_apply_version_and_rollback(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'community-policy.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    await repository.save_community_policy_draft(
        admin_id=999,
        chat_id=-100123,
        base_version=None,
        source_text="Удаляй любой мат",
        rules_json=json.dumps(
            [
                {
                    "rule_id": "R1",
                    "title": "Мат",
                    "condition": "Любая ненормативная лексика",
                    "action": "delete",
                    "mute_minutes": None,
                    "exceptions": [],
                }
            ],
            ensure_ascii=False,
        ),
        summary="Delete profanity.",
        changes_json='["Added profanity rule"]',
        ignored_json="[]",
    )

    v1 = await repository.apply_community_policy_draft(
        admin_id=999
    )

    assert v1 is not None
    assert v1.version == 1
    assert v1.active is True

    active = await repository.get_active_community_policy(
        -100123
    )
    assert active is not None
    assert active.version == 1

    await repository.save_community_policy_draft(
        admin_id=999,
        chat_id=-100123,
        base_version=1,
        source_text="Бань рекламу казино",
        rules_json=json.dumps(
            [
                {
                    "rule_id": "R1",
                    "title": "Казино",
                    "condition": "Реклама казино",
                    "action": "ban",
                    "mute_minutes": None,
                    "exceptions": [],
                }
            ],
            ensure_ascii=False,
        ),
        summary="Ban casino ads.",
        changes_json='["Changed custom policy"]',
        ignored_json="[]",
    )

    v2 = await repository.apply_community_policy_draft(
        admin_id=999
    )
    assert v2 is not None
    assert v2.version == 2

    rollback = await repository.rollback_community_policy(
        chat_id=-100123,
        admin_id=999,
    )

    assert rollback is not None
    assert rollback.version == 3
    assert "ненормативная" in rollback.rules_json

    cleared = await repository.clear_custom_community_policy(
        chat_id=-100123,
        admin_id=999,
    )

    assert cleared.version == 4
    assert cleared.rules_json == "[]"

    await database.dispose()


@pytest.mark.asyncio
async def test_policy_is_isolated_per_chat_and_stale_apply_cannot_cross_chat(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'community-policy-isolation.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    await repository.save_community_policy_draft(
        admin_id=999,
        chat_id=-100111,
        base_version=None,
        source_text="Удаляй любой мат",
        rules_json=json.dumps(
            [
                {
                    "rule_id": "R1",
                    "title": "Мат",
                    "condition": "Любая ненормативная лексика",
                    "action": "delete",
                    "mute_minutes": None,
                    "exceptions": [],
                }
            ],
            ensure_ascii=False,
        ),
        summary="Delete profanity only in chat A.",
        changes_json='["Added profanity rule"]',
        ignored_json="[]",
    )

    # A stale Apply callback from another selected chat must do nothing.
    wrong_chat_apply = await repository.apply_community_policy_draft(
        admin_id=999,
        expected_chat_id=-100222,
    )
    assert wrong_chat_apply is None

    # Draft is still available and can be applied only to its own chat.
    applied = await repository.apply_community_policy_draft(
        admin_id=999,
        expected_chat_id=-100111,
    )
    assert applied is not None
    assert applied.chat_id == -100111

    chat_a = await repository.get_active_community_policy(-100111)
    chat_b = await repository.get_active_community_policy(-100222)

    assert chat_a is not None
    assert "ненормативная" in chat_a.rules_json
    assert chat_b is None

    await database.dispose()
