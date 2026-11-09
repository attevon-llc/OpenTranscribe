"""Indexes on ``chat_message`` that the runner builds concurrently after startup (#1337)."""

from __future__ import annotations

from app.db.concurrent_indexes import IndexSpec

#: Serves ``citations @> '[{"file_uuid": "<uuid>"}]'``, the probe ``scrub_chat_citations`` runs
#: before rewriting history. ``jsonb_path_ops`` is the smaller, faster GIN operator class and
#: supports ``@>`` (PostgreSQL docs, "jsonb Indexing"), the only operator the probe uses. No
#: revision: it adds no column or constraint, so there is nothing for the ladder to detect,
#: and ``_retry_concurrent_indexes`` builds it on every start until it exists.
CITATIONS_GIN = IndexSpec(
    "ix_chat_message_citations_gin",
    "chat_message",
    ("citations jsonb_path_ops",),
    method="gin",
)

CHAT_INDEXES = (CITATIONS_GIN,)
