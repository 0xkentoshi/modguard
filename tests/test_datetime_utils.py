from datetime import datetime, timezone

import pytest

from app.utils.datetime_utils import ensure_utc_datetime


def test_datetime_is_preserved():
    value = datetime(
        2026,
        9,
        7,
        12,
        30,
        tzinfo=timezone.utc,
    )

    result = ensure_utc_datetime(
        value
    )

    assert result == value
    assert result.tzinfo is not None


def test_naive_datetime_becomes_utc():
    value = datetime(
        2026,
        9,
        7,
        12,
        30,
    )

    result = ensure_utc_datetime(
        value
    )

    assert result.tzinfo == timezone.utc


def test_unix_timestamp_becomes_datetime():
    timestamp = 1788796770

    result = ensure_utc_datetime(
        timestamp
    )

    assert isinstance(
        result,
        datetime,
    )

    assert result.tzinfo == timezone.utc

    assert (
        int(result.timestamp())
        == timestamp
    )


def test_float_timestamp_is_supported():
    timestamp = 1788796770.5

    result = ensure_utc_datetime(
        timestamp
    )

    assert (
        result.timestamp()
        == pytest.approx(timestamp)
    )


def test_invalid_type_is_rejected():
    with pytest.raises(TypeError):
        ensure_utc_datetime(
            "2026-09-07"
        )