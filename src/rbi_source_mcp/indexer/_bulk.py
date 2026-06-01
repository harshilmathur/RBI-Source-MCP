"""Shared dataclass + helpers for bulk indexer runs.

Before PR 2 each `build_*.py` family declared its own near-identical
result dataclass (one `BulkResult`, one `CircularBulkResult`, three
underscore-private `_BulkResult`s). They had drifted slightly — only two
exposed an `elapsed()` method; only one exposed a `total` property. This
module collapses them to one definition so future drift is impossible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class BulkResult:
    """Outcome of one bulk indexer run.

    Each entry is identified by an indexer-family-specific external ID
    (`md_id`, `notif_id`, `prid`, `faq_id`, `mc_id`). The shape is
    intentionally string-typed at this layer — callers know what their
    family's ID is, and the merge across families lives one level up.

    Attributes:
        started_at: monotonic-equivalent wall-clock timestamp captured at
            the start of the run (from ``time.time()``).
        success: list of IDs that crawled, indexed, and persisted cleanly.
        skipped: list of IDs that were short-circuited by the skip-set
            (already-indexed predicate).
        failed: list of ``(id, error_message)`` tuples; one entry per
            terminal failure (after any retry budget is exhausted).
    """

    started_at: float
    success: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Count of every ID the run touched."""
        return len(self.success) + len(self.skipped) + len(self.failed)

    def elapsed(self) -> float:
        """Wall-clock seconds since ``started_at``."""
        return time.time() - self.started_at
