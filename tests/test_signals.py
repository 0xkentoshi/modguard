from datetime import datetime, timedelta, timezone

from app.agent.schemas import MessageSnapshot
from app.moderation.signals import (
    build_behavior_signals,
    calculate_uppercase_ratio,
    count_mentions,
    extract_urls,
)
from app.utils.text_normalization import analyze_text


def make_message(
    *,
    db_id: int,
    message_id: int,
    text: str,
    date: datetime,
) -> MessageSnapshot:
    return MessageSnapshot(
        db_id=db_id,
        telegram_message_id=message_id,
        chat_id=-100123,
        user_id=100,
        username="@tester",
        full_name="Tester",
        content_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_date=date,
    )


def test_url_detection():
    urls = extract_urls(
        "Check https://example.com and t.me/test"
    )

    assert len(urls) == 2


def test_mentions():
    assert count_mentions(
        "hello @alice and @bob_123"
    ) == 2


def test_uppercase_ratio():
    ratio = calculate_uppercase_ratio(
        "FREE MONEY"
    )

    assert ratio == 1.0


def test_chinese_does_not_create_fake_uppercase_ratio():
    ratio = calculate_uppercase_ratio(
        "免费领取"
    )

    assert ratio == 0.0


def test_repeated_messages_are_detected():
    now = datetime.now(timezone.utc)

    current = make_message(
        db_id=3,
        message_id=3,
        text="FREE USDT",
        date=now,
    )

    history = [
        make_message(
            db_id=1,
            message_id=1,
            text="FREE USDT",
            date=now - timedelta(seconds=20),
        ),
        make_message(
            db_id=2,
            message_id=2,
            text="FREE USDT",
            date=now - timedelta(seconds=10),
        ),
    ]

    text_signals = analyze_text(
        current.raw_text
    )

    result = build_behavior_signals(
        current=current,
        text_signals=text_signals,
        user_history=history,
    )

    assert (
        result.repeated_recent_messages
        == 2
    )

    assert result.messages_last_60s == 3

def test_url_does_not_reduce_uppercase_signal():
    from app.moderation.signals import (
        remove_urls,
    )

    text = (
        "JOIN https://example.com "
        "FREE MONEY"
    )

    without_urls = remove_urls(
        text
    )

    ratio = (
        calculate_uppercase_ratio(
            without_urls
        )
    )

    assert ratio == 1.0