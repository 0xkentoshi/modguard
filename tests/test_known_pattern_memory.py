from datetime import datetime, timezone

import pytest

from app.admin import control_models  # noqa: F401
from app.admin.control_repository import ControlRepository
from app.agent.moderator import ModeratorAgent
from app.agent.schemas import (
    BehaviorSignals,
    MessageContext,
    MessageSnapshot,
    ModerationDecision,
)
from app.database.db import Database
from app.llm.base import LLMProvider
from app.utils.text_normalization import analyze_text


class NeverCalledProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def generate_structured(self, **kwargs):
        self.calls += 1
        raise AssertionError("LLM must not run on known exact pattern")


def make_context(text: str, user_id: int = 777):
    return MessageContext(
        current_message=MessageSnapshot(
            db_id=1,
            telegram_message_id=1,
            chat_id=-100123,
            user_id=user_id,
            username="@tester",
            full_name="Tester",
            content_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_date=datetime.now(timezone.utc),
        ),
        text_signals=analyze_text(text),
        behavior_signals=BehaviorSignals(),
    )


@pytest.mark.asyncio
async def test_confirmed_pattern_skips_llm(tmp_path):
    database = Database(
        "sqlite+aiosqlite:///"
        f"{(tmp_path / 'memory.db').as_posix()}"
    )
    await database.init()

    repository = ControlRepository(
        database.session_factory
    )

    decision = ModerationDecision(
        detected_language="English",
        current_message_violation=True,
        category="scam",
        severity="high",
        confidence=0.99,
        action="ban",
        delete_message=True,
        mute_minutes=None,
        needs_human_review=False,
        reason="Confirmed scam.",
        current_message_evidence=["Fake reward"],
        context_evidence=[],
    )

    await repository.remember_confirmed_pattern(
        chat_id=-100123,
        normalized_text="FREE 500 USDT",
        category="scam",
        confidence=0.99,
        decision_json=decision.model_dump_json(),
    )

    provider = NeverCalledProvider()

    agent = ModeratorAgent(
        provider,
        control_repository=repository,
    )

    result = await agent.analyze(
        make_context("FREE 500 USDT")
    )

    assert result.category == "scam"
    assert provider.calls == 0

    await database.dispose()
