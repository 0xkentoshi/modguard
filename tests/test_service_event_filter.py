from datetime import datetime, timezone

from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User

from app.bot.handlers import should_moderate_message


def make_message(
    *,
    text=None,
    caption=None,
):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(
            id=-100123,
            type=ChatType.SUPERGROUP,
            title="Sandbox",
        ),
        from_user=User(
            id=777,
            is_bot=False,
            first_name="Tester",
        ),
        text=text,
        caption=caption,
    )


def test_normal_text_is_moderated():
    assert should_moderate_message(
        make_message(
            text="hello"
        )
    ) is True


def test_caption_is_moderated():
    assert should_moderate_message(
        make_message(
            caption="promo text"
        )
    ) is True


def test_empty_service_like_event_is_skipped():
    assert should_moderate_message(
        make_message()
    ) is False
