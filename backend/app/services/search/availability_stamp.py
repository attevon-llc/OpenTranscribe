"""Rewrite the ``availability`` field of a file's OpenSearch documents in place (issue #1101).

Trash and restore change one Postgres row; this makes the index agree, without a reindex:
one ``update_by_query`` per index (``opensearch_service.update_by_query``, the same call the
tenant backfill uses) whose query selects the file's documents that do NOT already carry the
wanted value, and whose script sets it. Selecting only the documents that differ makes every
call idempotent and makes a no-op cheap: re-running a stamp touches nothing.

Where it runs:

* :func:`stamp_files` - synchronously, in the trash/restore request (``trash_service``),
  after the commit. A failure never undoes the trash: the Postgres post-filter
  (``file_availability.unavailable_among``) keeps hits out meanwhile, and the request
  dispatches ``search.availability_reconcile`` for those files.
* :func:`reconcile` - the ``search.availability_reconcile`` task. Scoped to file ids when
  dispatched after a failed stamp; the whole trash when run by beat, which is the guarantee
  that a stamp that failed AND whose retry dispatch failed still converges.

The per-file id lists are chunked at :data:`TERMS_CHUNK`, well under OpenSearch's default
``index.max_terms_count`` of 65,536 (OpenSearch docs, "Terms query"); nothing here builds a
query whose size grows with the trash beyond that.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.core.config import settings
from app.services.search.availability_field import AVAILABILITY_FIELD
from app.services.search.availability_field import AVAILABLE
from app.services.search.availability_field import TRASHED

logger = logging.getLogger(__name__)

#: Ids per ``terms`` clause (and per composite-aggregation page in the reconcile).
TERMS_CHUNK = 5000

#: The converging task (``tasks/search_availability_task``), dispatched by name so this
#: service does not import the task layer.
AVAILABILITY_RECONCILE_TASK = "search.availability_reconcile"

_STAMP_SCRIPT_SOURCE = f"ctx._source.{AVAILABILITY_FIELD} = params.value"


@dataclass(frozen=True)
class StampTarget:
    """An index holding per-file documents, and the field naming the file on them."""

    index: str
    id_field: str


def stamp_targets() -> list[StampTarget]:
    """Every index with per-file documents: chunks (chunk, digest and summary planes), the
    legacy whole-transcript index, and every speaker index (per-file voiceprints carry
    ``media_file_id``; profile and cluster documents carry none and are never matched)."""
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    return [
        StampTarget(settings.OPENSEARCH_CHUNKS_INDEX, "file_id"),
        StampTarget(settings.OPENSEARCH_TRANSCRIPT_INDEX, "file_id"),
        *(StampTarget(name, "media_file_id") for name in speaker_embedding_indices()),
    ]


def stamp_query(id_field: str, file_ids: Sequence[int], value: str) -> dict[str, Any]:
    """The documents of ``file_ids`` that do not already read ``value``."""
    return {
        "bool": {
            "filter": [{"terms": {id_field: [int(i) for i in file_ids]}}],
            "must_not": [{"term": {AVAILABILITY_FIELD: value}}],
        }
    }


def stamp_script(value: str) -> dict[str, Any]:
    """Set ``availability`` to ``value``; the value is a parameter, never interpolated."""
    if value not in (AVAILABLE, TRASHED):
        raise ValueError(f"unknown availability value: {value!r}")
    return {"source": _STAMP_SCRIPT_SOURCE, "lang": "painless", "params": {"value": value}}


@dataclass
class StampOutcome:
    """What one stamp pass did. ``incomplete`` lists indices where documents were left
    unstamped (a version conflict with a concurrent write, or a per-document failure)."""

    updated: int = 0
    incomplete: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete


def _chunks(ids: Sequence[int], size: int = TERMS_CHUNK) -> list[list[int]]:
    return [list(ids[i : i + size]) for i in range(0, len(ids), size)]


def stamp_files(file_ids: Iterable[int], value: str, *, client: Any = None) -> StampOutcome:
    """Make every document of ``file_ids`` read ``availability = value``.

    Args:
        file_ids: The files, all to the same value.
        value: :data:`~availability_field.TRASHED` or :data:`~availability_field.AVAILABLE`.
        client: OpenSearch client; the process singleton when omitted.

    Returns:
        The outcome. With OpenSearch disabled there is nothing indexed and nothing to do.

    Raises:
        Any transport or index error; the caller decides whether to retry.
    """
    from app.services.opensearch_service.update_by_query import update_by_query

    ids = sorted({int(i) for i in file_ids})
    script = stamp_script(value)
    outcome = StampOutcome()
    if not ids:
        return outcome
    if client is None:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
    if client is None:
        return outcome
    for target in stamp_targets():
        if not client.indices.exists(index=target.index):
            continue
        for chunk in _chunks(ids):
            response = update_by_query(
                client, target.index, stamp_query(target.id_field, chunk, value), script
            )
            outcome.updated += int(response.get("updated", 0))
            if response.get("failures") or int(response.get("version_conflicts", 0)):
                outcome.incomplete.append(target.index)
    return outcome


def _stamped_trashed_ids(client: Any, target: StampTarget) -> set[int]:
    """The file ids whose documents in ``target`` read ``trashed``, paged with a composite
    aggregation (OpenSearch docs, "Composite aggregations": ``after_key`` paging over every
    bucket, so the set is complete at any size)."""
    found: set[int] = set()
    after: dict[str, Any] | None = None
    while True:
        composite: dict[str, Any] = {
            "size": TERMS_CHUNK,
            "sources": [{"file": {"terms": {"field": target.id_field}}}],
        }
        if after is not None:
            composite["after"] = after
        response = client.search(
            index=target.index,
            body={
                "size": 0,
                "query": {"term": {AVAILABILITY_FIELD: TRASHED}},
                "aggs": {"files": {"composite": composite}},
            },
        )
        agg = response.get("aggregations", {}).get("files", {})
        for bucket in agg.get("buckets", []):
            key = bucket.get("key", {}).get("file")
            if key is not None:
                found.add(int(key))
        after = agg.get("after_key")
        if not agg.get("buckets") or after is None:
            return found


@dataclass
class ReconcileOutcome:
    """What one reconcile pass changed."""

    stamped_trashed: int = 0
    stamped_available: int = 0
    incomplete: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stamped_trashed": self.stamped_trashed,
            "stamped_available": self.stamped_available,
            "incomplete": sorted(set(self.incomplete)),
        }


def reconcile(file_ids: Iterable[int] | None = None, *, client: Any = None) -> ReconcileOutcome:
    """Bring the index's ``availability`` in line with Postgres.

    Scoped (``file_ids`` given): each existing file is stamped with its row's current value,
    so a restore that raced the retry wins. Unscoped (beat): every trashed file is stamped
    ``trashed`` and every file stamped ``trashed`` whose row is live again is stamped
    ``available``. A file with no row is left alone: its documents are the purge's and the
    integrity sweep's, not this task's.

    Raises:
        Any database or OpenSearch error, so the task run is recorded as failed.
    """
    from app.db.session_utils import session_scope
    from app.services.file_availability import index_availability_by_id
    from app.services.file_availability import trashed_file_ids

    if client is None:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
    outcome = ReconcileOutcome()
    if client is None:
        return outcome

    if file_ids is not None:
        with session_scope() as db:
            states = index_availability_by_id(db, file_ids)
        trashed = [i for i, v in states.items() if v == TRASHED]
        live = [i for i, v in states.items() if v == AVAILABLE]
    else:
        with session_scope() as db:
            trashed = trashed_file_ids(db)
        candidates: set[int] = set()
        for target in stamp_targets():
            if client.indices.exists(index=target.index):
                candidates |= _stamped_trashed_ids(client, target)
        with session_scope() as db:
            states = index_availability_by_id(db, candidates)
        live = sorted(i for i, v in states.items() if v == AVAILABLE)

    for ids, value in ((trashed, TRASHED), (live, AVAILABLE)):
        result = stamp_files(ids, value, client=client)
        if value == TRASHED:
            outcome.stamped_trashed += result.updated
        else:
            outcome.stamped_available += result.updated
        outcome.incomplete.extend(result.incomplete)
    if outcome.stamped_trashed or outcome.stamped_available:
        logger.info("availability reconcile: %s", outcome.as_dict())
    return outcome


def dispatch_reconcile(file_ids: Sequence[int]) -> None:
    """Queue a scoped ``search.availability_reconcile``; a failed dispatch is left to beat.

    By task name (``send_task``), routed by ``TASK_ROUTES`` like any dispatch.
    """
    if not file_ids:
        return
    from app.core.celery import celery_app
    from app.core.queues import UtilityPriority

    try:
        celery_app.send_task(
            AVAILABILITY_RECONCILE_TASK,
            kwargs={"file_ids": [int(i) for i in file_ids]},
            priority=UtilityPriority.ROUTINE,
        )
    except Exception:  # noqa: BLE001 - the 15-minute beat tick reconciles the whole trash
        logger.warning(
            "availability reconcile dispatch failed; the beat tick will converge", exc_info=True
        )
