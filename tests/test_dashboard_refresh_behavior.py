from aiogram.exceptions import TelegramBadRequest

from app.admin.dashboard import (
    is_missing_edit_target,
    is_not_modified_error,
)


def test_not_modified_is_treated_as_noop():
    exc = TelegramBadRequest(
        method=None,
        message="Bad Request: message is not modified",
    )

    assert is_not_modified_error(exc) is True
    assert is_missing_edit_target(exc) is False


def test_missing_message_can_create_fresh_dashboard():
    exc = TelegramBadRequest(
        method=None,
        message="Bad Request: message to edit not found",
    )

    assert is_missing_edit_target(exc) is True
