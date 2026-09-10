ACTION_NAMES = {
    "allow": "ALLOW",
    "warn": "WARN",
    "delete": "DELETE",
    "mute": "MUTE",
    "ban": "BAN",
    "escalate": "HUMAN REVIEW",
    "delete_failed": "DELETE FAILED",
}


def get_action_name(
    action: str,
) -> str:
    return ACTION_NAMES.get(
        action,
        action.replace(
            "_",
            " ",
        ).upper(),
    )


def is_reversible(
    action: str,
) -> bool:
    """
    Telegram-side reversibility.

    A ban/mute can be undone later.
    A deleted Telegram message cannot be restored by the bot.
    """

    return action in {
        "mute",
        "ban",
    }
