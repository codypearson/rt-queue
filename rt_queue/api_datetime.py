"""Parse ISO-like datetime strings from GitHub and Jira REST APIs."""

from __future__ import annotations

import datetime as dt
from typing import Any


def parse_api_datetime(value: Any) -> dt.datetime | None:
    """
    Parse an ISO-like API datetime string into a timezone-aware or naive datetime.

    Handles common Jira and GitHub formats including a ``Z`` suffix and offsets
    without a colon. Returns None if parsing fails or the value is empty.
    """
    if not value:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"

    if len(s) >= 5 and s[-5] in "+-" and s[-4:].isdigit():
        s = s[:-2] + ":" + s[-2:]

    if "." in s:
        try:
            base, rest = s.split(".", 1)
            if "+" in rest:
                _, offset = rest.split("+", 1)
                s = base + "+" + offset
            elif "-" in rest:
                _, offset = rest.split("-", 1)
                s = base + "-" + offset
            else:
                s = base
        except Exception:
            pass

    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None
