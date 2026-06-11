"""Time helpers.

Single home for the UTC-now timestamp used across the corpus and responses.
Replaces the deprecated `datetime.utcnow()` (removed in a future Python) while
preserving the exact ISO-8601 `...Z` string format already stored throughout
the corpus, so timestamps stay byte-consistent across the migration.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc_now() -> str:
    """Current UTC time as an ISO-8601 string with a trailing `Z`."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
