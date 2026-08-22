"""Protocol interfaces for key service boundaries.

These protocols define the contracts that services must satisfy.
Python's structural typing means existing implementations already
conform if they have matching method signatures -- no changes to
existing classes are required.

Protocols covered:

- **StorageService** -- object storage (MinIO).
  Matches ``MinIOService`` in ``minio_service.py``.

- **SearchService** -- transcript indexing and search (OpenSearch).
  Matches the module-level functions in ``opensearch_service.py``.

- **CacheService** -- API-response caching (Redis).
  Matches ``RedisCacheService`` in ``redis_cache_service.py``.

- **NotificationService** -- real-time WebSocket notifications.
  Matches ``send_ws_event`` in ``utils/websocket_notify.py``.
"""

from __future__ import annotations

from typing import Any
from typing import Protocol

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageService(Protocol):
    """Interface for object storage operations.

    Mirrors the public API of ``MinIOService`` and the module-level
    helper functions in ``minio_service.py``.
    """

    def upload_file(
        self,
        file_path: str,
        bucket_name: str,
        object_name: str,
        content_type: str | None = None,
    ) -> None:
        """Upload a local file to a storage bucket."""
        ...

    def download_file(
        self,
        object_name: str,
        file_path: str,
        bucket_name: str | None = None,
    ) -> None:
        """Download a stored object to a local file path."""
        ...

    def get_presigned_url(
        self,
        bucket_name: str,
        object_name: str,
        expires: int = 3600,
    ) -> str:
        """Return a time-limited URL for direct object access."""
        ...

    def delete_object(self, bucket_name: str, object_name: str) -> None:
        """Remove an object from a storage bucket."""
        ...


# ---------------------------------------------------------------------------
# Search / indexing
# ---------------------------------------------------------------------------


class SearchService(Protocol):
    """Interface for transcript indexing.

    Mirrors the module-level functions in ``opensearch_service``.

    It declared a ``search_transcripts`` member until #542. Nothing implemented it
    usefully: the only implementation searched the legacy full-document index and had
    a semantic branch the mapping could not serve, and it had zero callers. A Protocol
    member that no implementation can honour advertises a capability at the seam that
    does not exist, so it went with the function. Transcript SEARCH is the chunk plane
    (``services/search/hybrid_search_service.py``), which this Protocol does not model.
    """

    def index_transcript(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        transcript_text: str,
        speakers: list[str],
        title: str,
        tags: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Index a transcript for full-text and optional vector search."""
        ...

    def remove_speaker_embedding(self, speaker_uuid: str) -> bool:
        """Remove a speaker embedding from all speaker indices."""
        ...


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class CacheService(Protocol):
    """Interface for API-response caching.

    Mirrors ``RedisCacheService`` in ``redis_cache_service.py``.
    All operations degrade gracefully (return ``None`` / ``0``) when
    the cache backend is unavailable.
    """

    def get(self, key: str) -> Any | None:
        """Retrieve a cached value.  Returns ``None`` on miss or error."""
        ...

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """Store a value with a TTL in seconds."""
        ...

    def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern.  Returns count deleted."""
        ...


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationService(Protocol):
    """Interface for sending real-time notifications to users.

    Mirrors ``send_ws_event`` in ``utils/websocket_notify.py``.
    Implementations publish to the WebSocket pub/sub channel so the
    frontend receives live updates.
    """

    def send(self, user_id: int, notification_type: str, data: dict) -> bool:
        """Publish a notification to a specific user.

        Returns ``True`` on success, ``False`` on failure.
        """
        ...
