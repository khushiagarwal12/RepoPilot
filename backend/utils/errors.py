"""Helpers for turning exceptions into safe, human-readable log text."""
from __future__ import annotations

import os
import re

_SECRET_NAME = re.compile(r"(KEY|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL)", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"(://)([^/\s:@]+):([^/\s@]+)@")
MAX_ERROR_CHARS = 500


def sanitize_error(exc: BaseException, max_chars: int = MAX_ERROR_CHARS) -> str:
    """Return ``ExceptionType: message`` with secrets removed and length capped.

    - Credentials embedded in URLs (``postgresql://user:pass@host``) are masked.
    - The values of environment variables whose names look secret (KEY, TOKEN,
      PASSWORD, SECRET...) are masked if they appear in the message.
    """
    text = f"{type(exc).__name__}: {exc}"
    text = _URL_CREDENTIALS.sub(r"\1***:***@", text)
    for name, value in os.environ.items():
        if value and len(value) >= 6 and _SECRET_NAME.search(name):
            text = text.replace(value, "***")
    text = " ".join(text.split())  # collapse newlines/tabs
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text
