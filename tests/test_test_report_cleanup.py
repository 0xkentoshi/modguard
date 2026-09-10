from types import SimpleNamespace

import pytest

from app.admin.test_mode import (
    build_test_mode_payload,
    clear_all_test_artifacts,
    execute_test_ticket_action,
    run_report_simulation,
)


class FakeRepository:
    def __init__(self):
        self.artifacts = []
        self.created_ticket_kwargs = None
        self.cleared_tickets = False
        self.purged_artifacts = False

    async def get_chat_settings(self, chat_id):
        return SimpleNamespace(shadow_mode=False)

    async def get_live_ban_enabled(self, chat_id):
        return False

    async def register_test_artifact(
        self,
        *,
        managed_chat_id,
        telegram_chat_id,
        telegram_message_id,
        kind,
    ):
        item = SimpleNamespace(
            managed_chat_id=managed_chat_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            kind=kind,
        )
        self.artifacts.append(item)
        return item

    async def create_test_ticket(self, **kwargs):
        self.created_ticket_kwargs = kwargs
        return SimpleNamespace(
            id=77,
            ticket_key="TEST-REPORT1",
            chat_id=kwargs["chat_id"],
            username="@test_user",
            category=kwargs["category"],
            confidence=kwargs["confidence"],
        )

    async def list_test_artifacts(self, *, managed_chat_id):
        return [
            item
            for item in self.artifacts
            if item.managed_chat_id == managed_chat_id
        ]

    async def clear_test_tickets(self, *, chat_id):
        self.cleared_tickets = True
        return 1

    async def purge_test_artifacts(self, *, managed_chat_id):
        self.purged_artifacts = True
        count = len(
            [
                item
                for item in self.artifacts
                if item.managed_chat_id == managed_chat_id
            ]
        )
        self.artifacts = [
            item
            for item in self.artifacts
            if item.managed_chat_id != managed_chat_id
        ]
        return count


class FakeBot:
    def __init__(self):
        self.next_message_id = 100
        self.sent = []
        self.deleted = []

    async def send_message(self, *, chat_id, text, **kwargs):
        message_id = self.next_message_id
        self.next_message_id += 1
        self.sent.append(
            SimpleNamespace(
                chat_id=chat_id,
                text=text,
                kwargs=kwargs,
                message_id=message_id,
            )
        )
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeDashboard:
    def __init__(self):
        self.repository = FakeRepository()
        self.bot = FakeBot()
        self.admin_ids = {999}
        self.global_dry_run = False
        self.live_delete_enabled = True

    async def _chat_title(self, chat_id):
        return "Sandbox"


@pytest.mark.asyncio
async def test_test_mode_keeps_report_and_removes_duplicate_ticket_button():
    dashboard = FakeDashboard()

    text, keyboard = await build_test_mode_payload(
        dashboard_service=dashboard,
        chat_id=-100123,
    )

    labels = [
        button.text
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "📣 Test Report" in labels
    assert "⚠️ Test Ticket" not in labels
    assert "message → reporter reply → Ticket" in text


@pytest.mark.asyncio
async def test_report_simulation_posts_target_reply_and_ticket_alert():
    dashboard = FakeDashboard()

    ticket, status = await run_report_simulation(
        dashboard_service=dashboard,
        chat_id=-100123,
    )

    assert ticket.ticket_key.startswith("TEST-")
    assert "Ticket" in status

    # 2 community messages + 1 admin alert.
    assert len(dashboard.bot.sent) == 3

    target = dashboard.bot.sent[0]
    report = dashboard.bot.sent[1]
    alert = dashboard.bot.sent[2]

    assert target.chat_id == -100123
    assert "TEST · MEMBER MESSAGE" in target.text

    assert report.chat_id == -100123
    assert "TEST · REPORTER REPLY" in report.text
    assert report.kwargs["reply_parameters"].message_id == target.message_id

    assert alert.chat_id == 999
    assert "TEST · Review needed" in alert.text

    assert dashboard.repository.created_ticket_kwargs[
        "telegram_message_id"
    ] == target.message_id

    assert {
        item.kind
        for item in dashboard.repository.artifacts
    } == {
        "report_target",
        "report_reply",
        "test_ticket_alert",
    }


@pytest.mark.asyncio
async def test_clear_tests_removes_registered_messages_and_test_tickets():
    dashboard = FakeDashboard()

    await dashboard.repository.register_test_artifact(
        managed_chat_id=-100123,
        telegram_chat_id=-100123,
        telegram_message_id=140,
        kind="delete_probe",
    )
    await dashboard.repository.register_test_artifact(
        managed_chat_id=-100123,
        telegram_chat_id=999,
        telegram_message_id=200,
        kind="shadow_test_alert",
    )

    status = await clear_all_test_artifacts(
        dashboard_service=dashboard,
        chat_id=-100123,
    )

    assert (-100123, 140) in dashboard.bot.deleted
    assert (999, 200) in dashboard.bot.deleted
    assert dashboard.repository.cleared_tickets is True
    assert dashboard.repository.purged_artifacts is True
    assert "Messages removed: 2" in status



@pytest.mark.asyncio
async def test_test_ticket_delete_really_deletes_synthetic_target():
    dashboard = FakeDashboard()

    ticket, _ = await run_report_simulation(
        dashboard_service=dashboard,
        chat_id=-100123,
    )

    target_message_id = (
        dashboard.repository.created_ticket_kwargs[
            "telegram_message_id"
        ]
    )

    ok, status = await execute_test_ticket_action(
        dashboard_service=dashboard,
        ticket=SimpleNamespace(
            id=ticket.id,
            ticket_key=ticket.ticket_key,
            chat_id=-100123,
            telegram_message_id=target_message_id,
            context_json=(
                '{"test_mode": true, "test_source": "report"}'
            ),
        ),
        action="delete",
    )

    assert ok is True
    assert (-100123, target_message_id) in dashboard.bot.deleted
    assert "actually deleted" in status


@pytest.mark.asyncio
async def test_test_ticket_allow_leaves_synthetic_target_visible():
    dashboard = FakeDashboard()

    ok, status = await execute_test_ticket_action(
        dashboard_service=dashboard,
        ticket=SimpleNamespace(
            ticket_key="TEST-ALLOW1",
            chat_id=-100123,
            telegram_message_id=140,
            context_json='{"test_mode": true}',
        ),
        action="allow",
    )

    assert ok is True
    assert dashboard.bot.deleted == []
    assert "intentionally left" in status


@pytest.mark.asyncio
async def test_test_ticket_ban_is_safe_but_removes_synthetic_message():
    dashboard = FakeDashboard()

    ok, status = await execute_test_ticket_action(
        dashboard_service=dashboard,
        ticket=SimpleNamespace(
            ticket_key="TEST-BAN1",
            chat_id=-100123,
            telegram_message_id=150,
            context_json='{"test_mode": true}',
        ),
        action="ban",
    )

    assert ok is True
    assert (-100123, 150) in dashboard.bot.deleted
    assert "no real user was banned" in status


@pytest.mark.asyncio
async def test_test_ticket_warn_posts_visible_test_warning():
    dashboard = FakeDashboard()

    ok, status = await execute_test_ticket_action(
        dashboard_service=dashboard,
        ticket=SimpleNamespace(
            ticket_key="TEST-WARN1",
            chat_id=-100123,
            telegram_message_id=160,
            context_json='{"test_mode": true}',
        ),
        action="warn",
    )

    assert ok is True

    warning = dashboard.bot.sent[-1]
    assert warning.chat_id == -100123
    assert "TEST WARN" in warning.text
    assert (
        warning.kwargs["reply_parameters"].message_id
        == 160
    )

    assert "TEST WARN" in status
