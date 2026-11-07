"""Rewrite the ``availability`` field of a file's OpenSearch documents in place (#1101, #1253).

Trash, restore, quarantine and release change one Postgres row; this makes the index agree,
without a reindex:
one ``update_by_query`` per index (``opensearch_service.update_by_query``, the same call the
tenant backfill uses) whose query selects the file's documents that do NOT already carry the
wanted value, and whose script sets it. Selecting only the documents that differ makes every
call idempotent and makes a no-op cheap: re-running a stamp touches nothing.

Where it runs:

* :func:`restamp` / :func:`restamp_rows` - synchronously, in the trash/restore request (``trash_service``) and
  the quarantine/release action (``takedown_service``), after the commit, with each file's
  value read from its row. A failure never undoes the state change: the Postgres post-filter
  (``file_availability.unavailable_among``) keeps hits out of result lists meanwhile, and the
  caller dispatches ``search.availability_reconcile`` for those files.
* :func:`reconcile` - the ``search.availability_reconcile`` task. Scoped to file ids when
  dispatched after a failed stamp; every trashed and quarantined file when run unscoped (beat,
  and once per boot, which is what stamps the takedowns that predate the field), the guarantee
  that a stamp that failed AND whose retry dispatch failed still converges.

The per-file id lists are chunked at :data:`TERMS_CHUNK`, well under OpenSearch's default
``index.max_terms_count`` of 65,536 (OpenSearch docs, "Terms query"); nothing here builds a
query whose size grows with the trash beyond that.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.services.search.availability_field import AVAILABILITY_FIELD
from app.services.search.availability_field import AVAILABLE
from app.services.search.availability_field import QUARANTINED
from app.services.search.availability_field import TRASHED
from app.services.search.availability_field import VALUES
from app.services.search.availability_field import unavailable_term

if TYPE_CHECKING:
    from app.models.media import MediaFile

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
    if value not in VALUES:
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
        value: One of :data:`~availability_field.VALUES`.
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


def _stamped_unavailable_ids(client: Any, target: StampTarget) -> set[int]:
    """The file ids whose documents in ``target`` read trashed or quarantined, paged with a composite
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
                "query": unavailable_term(),
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
    stamped_quarantined: int = 0
    stamped_available: int = 0
    incomplete: list[str] = field(default_factory=list)

    def add(self, value: str, result: StampOutcome) -> None:
        if value == TRASHED:
            self.stamped_trashed += result.updated
        elif value == QUARANTINED:
            self.stamped_quarantined += result.updated
        else:
            self.stamped_available += result.updated
        self.incomplete.extend(result.incomplete)

    @property
    def changed(self) -> bool:
        return bool(self.stamped_trashed or self.stamped_quarantined or self.stamped_available)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stamped_trashed": self.stamped_trashed,
            "stamped_quarantined": self.stamped_quarantined,
            "stamped_available": self.stamped_available,
            "incomplete": sorted(set(self.incomplete)),
        }


def _by_value(states: dict[int, str]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for file_id, value in sorted(states.items()):
        grouped.setdefault(value, []).append(file_id)
    return grouped


def reconcile(file_ids: Iterable[int] | None = None, *, client: Any = None) -> ReconcileOutcome:
    """Bring the index's ``availability`` in line with Postgres.

    Scoped (``file_ids`` given): each existing file is stamped with its row's current value,
    so a restore or release that raced the retry wins. Unscoped (beat, boot): every trashed or
    quarantined file is stamped with its value, and every file whose documents read trashed or
    quarantined but whose row is available again is stamped ``available``. A file with no row
    is left alone: its documents are the purge's and the integrity sweep's, not this task's.

    Raises:
        Any database or OpenSearch error, so the task run is recorded as failed.
    """
    from app.db.session_utils import session_scope
    from app.services.file_availability import index_availability_by_id
    from app.services.file_availability import unavailable_file_states

    if client is None:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
    outcome = ReconcileOutcome()
    if client is None:
        return outcome

    if file_ids is not None:
        with session_scope() as db:
            states = index_availability_by_id(db, file_ids)
    else:
        with session_scope() as db:
            states = unavailable_file_states(db)
        candidates: set[int] = set()
        for target in stamp_targets():
            if client.indices.exists(index=target.index):
                candidates |= _stamped_unavailable_ids(client, target)
        candidates -= set(states)
        with session_scope() as db:
            released = index_availability_by_id(db, candidates)
        states.update({i: v for i, v in released.items() if v == AVAILABLE})

    for value, ids in _by_value(states).items():
        outcome.add(value, stamp_files(ids, value, client=client))
    if outcome.changed:
        logger.info("availability reconcile: %s", outcome.as_dict())
    return outcome


def restamp_rows(files: Iterable[MediaFile]) -> None:
    """:func:`restamp` with each value read from its loaded row, after the commit."""
    from app.services.file_availability import index_availability

    restamp({int(f.id): index_availability(f) for f in files})


def restamp(states: Mapping[int, str]) -> None:
    """Rewrite each file's index documents to its value, now; never raises.

    For the request that just committed a trash, restore, quarantine or release: synchronous
    (``refresh=True``), so the response already reads without the file in counts, facets and
    speaker matches. The values come from the rows (``file_availability.index_availability``),
    never from the action, so a restore of a quarantined file stamps ``quarantined`` and a
    release of a trashed one stamps ``trashed``. Any failure or shortfall dispatches a scoped
    reconcile; the state change is committed and stays committed.

    Args:
        states: File id to the value its documents must read.
    """
    if not states:
        return
    complete = True
    for value, ids in _by_value(dict(states)).items():
        try:
            complete = stamp_files(ids, value).complete and complete
        except Exception:  # noqa: BLE001 - the post-filter covers the gap; the retry converges it
            logger.warning("availability stamp failed; dispatching a reconcile", exc_info=True)
            complete = False
    if not complete:
        dispatch_reconcile(sorted(states))


def dispatch_reconcile(file_ids: Sequence[int] | None) -> None:
    """Queue ``search.availability_reconcile``; a failed dispatch is left to beat. Never raises.

    Scoped to ``file_ids``, or unscoped (every trashed and quarantined file) with ``None``, which
    is the once-per-boot run. By task name (``send_task``), routed by ``TASK_ROUTES`` like any
    dispatch.
    """
    if file_ids is not None and not file_ids:
        return
    from app.core.celery import celery_app
    from app.core.queues import UtilityPriority

    try:
        celery_app.send_task(
            AVAILABILITY_RECONCILE_TASK,
            kwargs={"file_ids": None if file_ids is None else [int(i) for i in file_ids]},
            priority=UtilityPriority.ROUTINE,
        )
    except Exception:  # noqa: BLE001 - the 15-minute beat tick reconciles every unavailable file
        logger.warning(
            "availability reconcile dispatch failed; the beat tick will converge", exc_info=True
        )
