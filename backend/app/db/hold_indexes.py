"""Indexes on ``content_hold`` that the runner builds concurrently after the upgrade (v467)."""

from __future__ import annotations

from app.db.concurrent_indexes import IndexSpec

#: The operator's "active platform holds" read. Partial, so it holds only the open platform holds.
PLATFORM_ACTIVE = IndexSpec(
    "ix_content_hold_platform_active",
    "content_hold",
    ("id",),
    where="authority = 'platform' AND released_at IS NULL",
)

HOLD_INDEXES = (PLATFORM_ACTIVE,)
