from types import SimpleNamespace

import pytest

from app.moderation.fake_admin import FakeAdminDetector


class FakeBot:
    async def get_chat_administrators(self, chat_id):
        return [
            SimpleNamespace(
                user=SimpleNamespace(
                    id=101,
                    username="real_admin",
                    first_name="Crypto",
                    last_name="Admin",
                )
            )
        ]

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status=("administrator" if user_id == 101 else "member")
        )


def message(*, user_id, username, first_name, last_name="", text="hello"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        ),
        text=text,
        caption=None,
    )


@pytest.mark.asyncio
async def test_real_admin_is_never_flagged_even_with_dangerous_text():
    detector = FakeAdminDetector(bot=FakeBot())
    finding = await detector.inspect(
        message(
            user_id=101,
            username="real_admin",
            first_name="Crypto",
            last_name="Admin",
            text="Urgent connect wallet https://example.xyz and DM me",
        )
    )
    assert finding is None


@pytest.mark.asyncio
async def test_close_copy_of_real_admin_with_wallet_link_is_high_risk():
    detector = FakeAdminDetector(bot=FakeBot())
    finding = await detector.inspect(
        message(
            user_id=202,
            username="real_adm1n",
            first_name="Crypto",
            last_name="Admin",
            text="Срочно подключите кошелек https://evil.xyz и напишите в ЛС",
        )
    )
    assert finding is not None
    assert finding.confidence >= 0.94
    decision = finding.decision()
    assert decision.category == "impersonation"
    assert decision.action == "ban"
    assert decision.delete_message is True
    policy = finding.policy()
    assert policy.final_action == "ban"
    assert policy.final_delete_message is True


@pytest.mark.asyncio
async def test_authority_word_alone_does_not_trigger_on_safe_message():
    detector = FakeAdminDetector(bot=FakeBot())
    finding = await detector.inspect(
        message(
            user_id=303,
            username="community_support",
            first_name="Community",
            last_name="Support",
            text="Всем привет, сегодня обновили FAQ.",
        )
    )
    assert finding is None


@pytest.mark.asyncio
async def test_fake_support_requires_multiple_danger_signals():
    detector = FakeAdminDetector(bot=FakeBot())
    finding = await detector.inspect(
        message(
            user_id=404,
            username="official_support",
            first_name="Official",
            last_name="Support",
            text="Verify your wallet here https://evil.xyz and DM me now",
        )
    )
    assert finding is not None
    assert "administrator/moderator/support" in " ".join(finding.evidence)


@pytest.mark.asyncio
async def test_compact_admin_marker_plus_danger_is_detected():
    detector = FakeAdminDetector(bot=FakeBot())
    finding = await detector.inspect(
        message(
            user_id=505,
            username="CryptoAdminHelp",
            first_name="CryptoAdminHelp",
            text="Connect wallet https://evil.xyz and DM me for verification",
        )
    )
    assert finding is not None
    assert finding.decision().category == "impersonation"

@pytest.mark.asyncio
async def test_current_role_recheck_prevents_stale_cache_false_positive():
    class PromotedBot(FakeBot):
        async def get_chat_administrators(self, chat_id):
            # Cache snapshot does not yet contain the newly promoted user.
            return await super().get_chat_administrators(chat_id)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="administrator")

    detector = FakeAdminDetector(bot=PromotedBot())
    finding = await detector.inspect(
        message(
            user_id=606,
            username="CryptoAdminHelp",
            first_name="CryptoAdminHelp",
            text="Connect wallet https://evil.xyz and DM me for verification",
        )
    )
    assert finding is None
