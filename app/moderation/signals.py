import re
from datetime import timedelta

from app.agent.schemas import (
    BehaviorSignals,
    MessageSnapshot,
)
from app.utils.datetime_utils import (
    ensure_utc_datetime,
)
from app.utils.text_normalization import (
    TextSignals,
)


URL_RE = re.compile(
    r"""(?ix)
    \b(
        https?://[^\s<>()]+
        |
        www\.[^\s<>()]+
        |
        t\.me/[^\s<>()]+
        |
        telegram\.me/[^\s<>()]+
    )
    """
)

MENTION_RE = re.compile(
    r"(?<!\w)@[A-Za-z0-9_]{3,32}"
)


def extract_urls(
    text: str,
) -> list[str]:
    return [
        match.group(0)
        for match in URL_RE.finditer(
            text
        )
    ]


def count_urls(
    text: str,
) -> int:
    return len(
        extract_urls(
            text
        )
    )


def remove_urls(
    text: str,
) -> str:
    return URL_RE.sub(
        " ",
        text,
    )


def extract_mentions(
    text: str,
) -> list[str]:
    return MENTION_RE.findall(
        text
    )


def count_mentions(
    text: str,
) -> int:
    return len(
        extract_mentions(
            text
        )
    )


def calculate_uppercase_ratio(
    text: str,
) -> float:
    letters = [
        char
        for char in text
        if char.isalpha()
    ]

    if not letters:
        return 0.0

    uppercase = sum(
        1
        for char in letters
        if char.isupper()
    )

    return uppercase / len(
        letters
    )


def calculate_uppercase_ratio_without_urls(
    text: str,
) -> float:
    return calculate_uppercase_ratio(
        remove_urls(
            text
        )
    )


def normalize_for_repeat_compare(
    text: str,
) -> str:
    return (
        text.strip()
        .casefold()
    )


def count_repeated_messages(
    current_text: str,
    previous_texts: list[str],
) -> int:
    current = normalize_for_repeat_compare(
        current_text
    )

    if not current:
        return 0

    return sum(
        1
        for item in previous_texts
        if normalize_for_repeat_compare(
            item
        ) == current
    )


_uppercase_ratio = (
    calculate_uppercase_ratio
)

_without_urls = (
    remove_urls
)


def _canonical_text(
    text: str,
) -> str:
    return normalize_for_repeat_compare(
        text
    )


def build_behavior_signals(
    *,
    current: MessageSnapshot,
    text_signals: TextSignals,
    user_history: list[
        MessageSnapshot
    ],
) -> BehaviorSignals:
    raw_text = (
        text_signals.raw_text
    )

    urls = extract_urls(
        raw_text
    )

    mentions = extract_mentions(
        raw_text
    )

    current_time = (
        ensure_utc_datetime(
            current.telegram_date
        )
    )

    cutoff = (
        current_time
        - timedelta(
            seconds=60
        )
    )

    messages_last_60s = 1

    for item in user_history:
        item_time = (
            ensure_utc_datetime(
                item.telegram_date
            )
        )

        if (
            cutoff
            <= item_time
            <= current_time
        ):
            messages_last_60s += 1

    repeated_recent_messages = (
        count_repeated_messages(
            current.normalized_text,
            [
                item.normalized_text
                for item in user_history
            ],
        )
    )

    return BehaviorSignals(
        has_urls=bool(
            urls
        ),
        url_count=len(
            urls
        ),
        mention_count=len(
            mentions
        ),
        messages_last_60s=(
            messages_last_60s
        ),
        repeated_recent_messages=(
            repeated_recent_messages
        ),
        uppercase_ratio=(
            calculate_uppercase_ratio(
                raw_text
            )
        ),
        uppercase_ratio_without_urls=(
            calculate_uppercase_ratio_without_urls(
                raw_text
            )
        ),
        is_reply=(
            current.reply_to_message_id
            is not None
        ),
        is_edited=(
            current.is_edited
        ),
        is_forwarded=(
            current.is_forwarded
        ),
    )
