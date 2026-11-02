"""The OpenSearch, object-storage, Redis, filesystem, mirror and backup stores of the inventory.

Every measurer takes ``(scope, captured)`` and returns a count, or RAISES when the store could not
be asked. ``measure.measure_all`` turns a raise into ``None`` (unverified): an unreachable store
must never read as an empty one. The measurers are written independently of the deletes they
check, so a bug in a delete cannot hide itself from its own check.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.erasure_receipt.stores_pg import Disposition
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope
from app.services.erasure_receipt.types import object_prefixes

logger = logging.getLogger(__name__)

Measurer = Callable[[ErasureScope, Captured], int]
_ALL = ("user", "org_member", "organization", "file")
_BATCH = 1000

#: Physical index name -> logical key. A prefix match, so a versioned name (``..._v6``) maps too.
#: An index that matches none of these and is not a system index (name starting with ``.``) is
#: ``os.unclassified``: the store an erasure forgets.
INDEX_PATTERNS: tuple[tuple[str, str], ...] = (
    ("transcript_chunks", "os.transcript_chunks"),
    ("transcript_summaries", "os.transcript_summaries"),
    ("transcripts", "os.transcripts"),
    ("speakers", "os.speakers"),
    ("audit-logs-", "os.audit"),
)


#: Indices a plugin writes by itself, with no per-subject records the application can address:
#: the Query Insights plugin's rolling daily ``top_queries-*`` index. It rotates on the plugin's
#: own retention; the erasure docs state that window.
PLUGIN_INDEX_PREFIXES: tuple[str, ...] = ("top_queries-",)


@dataclass(frozen=True)
class ExternalStore:
    key: str
    group: str
    disposition: Disposition
    reason: str | None
    measure: Measurer
    scopes: tuple[str, ...] = _ALL


def classify_index(name: str, configured: dict[str, str] | None = None) -> str | None:
    """Logical key of a physical index, or ``None`` when nobody classified it.

    System indices (a leading ``.``) are not the application's data and are skipped by the caller.
    """
    for physical, key in (configured or {}).items():
        if name == physical or name.startswith(f"{physical}_"):
            return key
    for prefix, key in INDEX_PATTERNS:
        if name.startswith(prefix):
            return key
    return None


def _configured_indices() -> dict[str, str]:
    from app.core.config import settings

    return {
        settings.OPENSEARCH_TRANSCRIPT_INDEX: "os.transcripts",
        settings.OPENSEARCH_CHUNKS_INDEX: "os.transcript_chunks",
        settings.OPENSEARCH_SUMMARY_INDEX: "os.transcript_summaries",
        settings.OPENSEARCH_SPEAKER_INDEX: "os.speakers",
    }


def _os_client() -> Any:
    from app.services.opensearch_service import opensearch_client

    if opensearch_client is None:
        raise RuntimeError("OpenSearch client unavailable")
    return opensearch_client


def _count(index: str, query: dict[str, Any]) -> int:
    """Count after a refresh. An absent index is the cluster answering 0."""
    client = _os_client()
    if not client.indices.exists(index=index):
        return 0
    client.indices.refresh(index=index)
    return int(client.count(index=index, body={"query": query})["count"])


def _os_transcripts(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    ids = captured.file_uuids
    return sum(
        _count(settings.OPENSEARCH_TRANSCRIPT_INDEX, {"ids": {"values": ids[i : i + _BATCH]}})
        for i in range(0, len(ids), _BATCH)
    )


def _os_chunks(scope: ErasureScope, captured: Captured) -> int:
    from app.services.search.indexing_service import TranscriptIndexingService

    service = TranscriptIndexingService()
    return sum(int(service.count_file_documents(u)) for u in captured.file_uuids)


def _os_summaries(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    ids = captured.file_ids
    return sum(
        _count(settings.OPENSEARCH_SUMMARY_INDEX, {"terms": {"file_id": ids[i : i + _BATCH]}})
        for i in range(0, len(ids), _BATCH)
    )


def _speaker_stamp(scope: ErasureScope) -> dict[str, Any] | None:
    """The same stamp clause ``_erase_speaker_voiceprints`` deletes by."""
    terms: list[dict[str, Any]] = []
    if scope.kind in ("user", "org_member"):
        terms.append({"term": {"user_id": scope.user_id}})
    if scope.kind in ("org_member", "organization"):
        terms.append({"term": {"organization_id": scope.organization_id}})
    return {"bool": {"filter": terms}} if terms else None


def _os_speakers(scope: ErasureScope, captured: Captured) -> int:
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids

    ids = [
        *captured.speaker_uuids,
        *(f"profile_{u}" for u in captured.profile_uuids),
        *(f"cluster_{u}" for u in captured.cluster_uuids),
    ]
    errors: list[dict[str, Any]] = []
    found = count_speaker_docs_by_ids(ids, also=_speaker_stamp(scope), errors=errors)
    if found is None:
        raise RuntimeError("speaker documents could not be counted")
    return found


def _os_audit(scope: ErasureScope, captured: Captured) -> int:
    client = _os_client()
    if scope.user_id is None:
        return 0
    query = {
        "bool": {
            "should": [
                {"term": {"user_id": scope.user_id}},
                {"term": {"target_user_id": scope.user_id}},
            ],
            "minimum_should_match": 1,
        }
    }
    if not client.indices.exists(index="audit-logs-*"):
        return 0
    return int(client.count(index="audit-logs-*", body={"query": query})["count"])


def ignored_indices() -> set[str]:
    """Index names an operator classified as holding no personal data (``SystemSettings``)."""
    import json

    from app.db.session_utils import session_scope
    from app.models.system_settings import SystemSettings

    with session_scope() as db:
        row = db.query(SystemSettings).filter(SystemSettings.key == IGNORED_INDICES_KEY).first()
        raw = row.value if row is not None else None
    try:
        parsed = json.loads(raw) if raw else []
    except ValueError:
        return set()
    return {str(n) for n in parsed} if isinstance(parsed, list) else set()


IGNORED_INDICES_KEY = "erasure.receipt.ignored_indices"


def unclassified_indices() -> list[str]:
    """Application indices no entry of the inventory covers, minus the operator's allow-list."""
    names = [r["index"] for r in _os_client().cat.indices(format="json")]
    configured = _configured_indices()
    ignored = ignored_indices()
    return sorted(
        n
        for n in names
        if not n.startswith(".")
        and not n.startswith(PLUGIN_INDEX_PREFIXES)
        and n not in ignored
        and classify_index(n, configured) is None
    )


def _os_unclassified(scope: ErasureScope, captured: Captured) -> int:
    """Always raises while an unclassified index exists: it cannot be shown clean."""
    stray = unclassified_indices()
    if stray:
        raise RuntimeError(f"{len(stray)} unclassified index(es)")
    return 0


# --- object storage ---------------------------------------------------------------------------


def _client() -> Any:
    from app.services.minio_service import minio_client

    return minio_client


def _count_versions(bucket: str, prefix: str, *, exact: bool = False) -> int:
    """Objects, noncurrent versions and delete markers under ``prefix``."""
    total = 0
    for obj in _client().list_objects(bucket, prefix=prefix, recursive=True, include_version=True):
        if getattr(obj, "is_dir", False):
            continue
        if exact and obj.object_name != prefix:
            continue
        total += 1
    return total


def _s3_file_objects(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    return sum(
        _count_versions(settings.MEDIA_BUCKET_NAME, p) for p in object_prefixes(scope, captured)
    )


def _s3_avatars(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    total = sum(
        _count_versions(settings.MEDIA_BUCKET_NAME, p, exact=True) for p in captured.avatar_paths
    )
    if scope.kind == "user" and scope.user_id is not None:
        total += _count_versions(settings.MEDIA_BUCKET_NAME, f"avatars/{scope.user_id}/")
    return total


def _s3_temp(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings
    from app.services.minio_service import TEMP_PREPROCESS_PREFIX

    return sum(
        _count_versions(settings.MEDIA_BUCKET_NAME, f"{TEMP_PREPROCESS_PREFIX}/{u}/")
        for u in captured.file_uuids
    )


def _s3_multipart(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    total = 0
    for prefix in object_prefixes(scope, captured):
        result = _client()._list_multipart_uploads(settings.MEDIA_BUCKET_NAME, prefix=prefix)
        total += len(result.uploads or [])
    return total


def _s3_cache(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings
    from app.services.video_processing_service import VideoProcessingService

    return sum(
        _count_versions(
            settings.CACHE_BUCKET_NAME, f"{VideoProcessingService.DERIVED_CACHE_PREFIX}{fid}_"
        )
        for fid in captured.file_ids
        if fid not in captured.held_file_ids
    )


# --- redis, filesystem, mirror, backup --------------------------------------------------------


def _redis_lockout(scope: ErasureScope, captured: Captured) -> int:
    from app.auth.lockout import identifier_has_record

    return int(bool(captured.email) and identifier_has_record(str(captured.email)))


def _fs_temp(scope: ErasureScope, captured: Captured) -> int:
    from app.core.config import settings

    root = Path(settings.TEMP_DIR)
    if not root.is_dir():
        return 0
    wanted = set(captured.file_uuids)
    return sum(1 for entry in root.iterdir() if any(u in entry.name for u in wanted))


def _mirror_media(scope: ErasureScope, captured: Captured) -> int:
    from app.services.media_mirror_erasure import count_mirrored

    return count_mirrored(object_prefixes(scope, captured))


def _backup_dumps(scope: ErasureScope, captured: Captured) -> int:
    from app.services import backup_service

    cfg = backup_service.get_settings(None)
    destination = cfg.get("destination")
    return len(backup_service.list_backups(destination)) if destination else 0


EXTERNAL_STORES: tuple[ExternalStore, ...] = (
    ExternalStore("os.transcripts", "os", "deleted", None, _os_transcripts),
    ExternalStore("os.transcript_chunks", "os", "deleted", None, _os_chunks),
    ExternalStore("os.transcript_summaries", "os", "deleted", None, _os_summaries),
    ExternalStore("os.speakers", "os", "deleted", None, _os_speakers),
    ExternalStore("os.audit", "os", "retained", "audit_trail", _os_audit, ("user", "org_member")),
    ExternalStore("os.unclassified", "os", "deleted", None, _os_unclassified),
    ExternalStore("s3.media.file_objects", "s3", "deleted", None, _s3_file_objects),
    ExternalStore("s3.media.avatars", "s3", "deleted", None, _s3_avatars),
    ExternalStore("s3.media.temp", "s3", "deleted", None, _s3_temp),
    ExternalStore("s3.media.multipart", "s3", "deleted", None, _s3_multipart),
    ExternalStore("s3.cache.derived", "s3", "deleted", None, _s3_cache),
    ExternalStore("redis.lockout", "redis", "deleted", None, _redis_lockout, ("user",)),
    ExternalStore("fs.temp", "fs", "deleted", None, _fs_temp),
    ExternalStore("mirror.media", "mirror", "deleted", None, _mirror_media),
    ExternalStore("backup.pg_dumps", "backup", "retained", "until_rotation", _backup_dumps),
)
