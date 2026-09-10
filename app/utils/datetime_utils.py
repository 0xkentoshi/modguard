from datetime import datetime, timezone


def ensure_utc_datetime(
    value: datetime | int | float,
) -> datetime:
    """
    Converts Telegram/DB date values into a timezone-aware UTC datetime.

    Telegram normally exposes dates as datetime objects, but some update
    paths or library/model conversions may still expose a Unix timestamp.

    We normalize both forms here so SQLite never receives an integer
    for a DateTime column.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(
            value,
            tz=timezone.utc,
        )

    raise TypeError(
        f"Unsupported datetime value: {value!r} "
        f"({type(value).__name__})"
    )