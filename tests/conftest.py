"""Pytest bootstrap for the public repository.

The application intentionally requires TELEGRAM_BOT_TOKEN at runtime.
Tests must remain runnable from a fresh clone without a real secret, so a
non-functional dummy token is supplied only when the environment does not
already provide one.
"""

import os


os.environ.setdefault(
    "TELEGRAM_BOT_TOKEN",
    "123456789:TEST_TOKEN_ONLY_DO_NOT_USE_12345678901234567890",
)
