"""Single list of engagement-identifying terms banned from the repo and generated data (D-006)."""

from __future__ import annotations

import re

BANNED = ("american foods", "afg", "birst", "kion", "prescott", "ricardo")
BANNED_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BANNED) + r")\b", re.IGNORECASE)
