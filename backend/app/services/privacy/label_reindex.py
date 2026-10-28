"""Rebuild the planes that carry a voice-made speaker NAME when a voiceprint switch changes.

A speaker label printed into a search chunk, a digest or a facts roster comes from
``utils.speaker_labels.canonical_speaker_label_for_row``, which leaves a name a voiceprint made
out while the file's voiceprints are off (plan D5) and puts it back when they are on again. The
documents already in the index were written under the old answer, so a switch change has to
re-index the files whose label a voiceprint decides, or the stale name stays searchable (and
shown in results and chat) until some unrelated mutation touches the file.

This module SELECTS those files and hands them to the existing debounced dispatch
(``search/reindex_dispatch.queue_transcript_reindex`` plus ``dispatch_file_facts``); it owns no
queue of its own. The re-index reads the policy when it RUNS, so a switch flipped back before it
fires rebuilds the same labels and costs nothing.

Three properties make it dependable:

* **Durable.** :func:`request_label_resync` records the scope in a pending record
  (``SystemSettings`` ``privacy.label_resync.pending``) BEFORE anything is dispatched. A scope
  leaves the record only when every one of its files was queued, so a dead broker, a worker
  that died mid-way or a lost task costs a delay, never a stale index.
* **Audited and measured.** A request and a finished scope each write one
  ``privacy.labels.resync`` audit event, the latter with the counts the run measured
  (``files_selected`` / ``files_queued`` / ``files_already_pending`` / ``files_failed`` /
  ``pages``): the resync is evidence, not an assumption.
* **Self-checking.** :func:`detect_label_drift` compares the labels the policy implies now with
  the labels the chunk index holds, for the files a voiceprint can name, and queues the files
  that disagree. It catches what no switch hook can see: a deployment's ceiling resolver or
  consent hook changing its answer (``docs/design/v060_downstream_changes.md``).

Both the pending record and the drift scan are driven by ``search_index_maintenance`` as a
safety net; the normal path is the ``privacy.label_resync`` task the request dispatches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.system_settings import SystemSettings
from app.utils.speaker_identity_source import MACHINE_IDENTITY_SOURCES
from app.utils.speaker_labels import DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD
from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Pending scopes, a JSON list of :func:`_record` dicts. Deduplicated by scope.
PENDING_KEY = "privacy.label_resync.pending"
#: Where the drift scan resumes (a ``media_file.id``); wraps to 0 after the last file.
DRIFT_CURSOR_KEY = "privacy.label_resync.drift_cursor"
#: Files handled per page, in id order.
PAGE_SIZE = 500
#: Files the drift scan compares per maintenance tick: bounds one tick's index queries.
DRIFT_FILES_PER_TICK = 5000

ScopeKind = Literal["instance", "organization", "user"]
_KINDS = ("instance", "organization", "user")

Reindex = Callable[..., Any]
FactsDispatch = Callable[[int], Any]
IndexedLabels = Callable[[list[str]], dict[str, set[str]]]


@dataclass(frozen=True)
class ResyncScope:
    """Whose files a changed switch can relabel.

    ``instance`` = every file; ``organization`` = the files stamped with that organization;
    ``user`` = the files that person owns (their own switch covers all their scopes).
    """

    kind: ScopeKind
    organization_id: int | None = None
    user_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown resync scope {self.kind!r}")
        if self.kind == "organization" and self.organization_id is None:
            raise ValueError("an organization scope needs an organization id")
        if self.kind == "user" and self.user_id is None:
            raise ValueError("a user scope needs a user id")
        if self.kind == "instance" and (self.organization_id is not None or self.user_id):
            raise ValueError("an instance scope names no organization or user")

    @property
    def key(self) -> tuple[str, int | None, int | None]:
        """What makes two requests the same scope."""
        return (self.kind, self.organization_id, self.user_id)


@dataclass
class ScopeOutcome:
    """What one pass over one scope measured."""

    scope: ResyncScope
    files_selected: int = 0
    files_queued: int = 0
    files_already_pending: int = 0
    files_failed: int = 0
    pages: int = 0
    error: str | None = None
    failed_file_ids: list[int] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        """Every selected file was queued (or already pending) and the listing did not fail."""
        return self.files_failed == 0 and self.error is None


# --- selecting the files a switch can relabel ---------------------------------------------


def files_labelled_by_voiceprints(
    db: Session, scope: ResyncScope, *, after_id: int = 0, limit: int = PAGE_SIZE
) -> list[tuple[int, str, int]]:
    """``(id, uuid, owner)`` of completed files with a speaker a voiceprint names, in id order.

    A file is affected when a speaker's ``identity_source`` is a voiceprint one, or when a
    voiceprint SUGGESTION decides the label (a speaker with no name of its own).

    Args:
        db: Session, read only.
        scope: Which files to consider.
        after_id: Return only files with a larger id (the page cursor).
        limit: Page size.
    """
    voice_named = exists().where(
        Speaker.media_file_id == MediaFile.id,
        or_(
            Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
            (
                Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES))
                & Speaker.suggested_name.isnot(None)
                & (Speaker.suggested_name != "")
                & (Speaker.confidence >= DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD)
                & or_(Speaker.display_name.is_(None), Speaker.display_name == "")
            ),
        ),
    )
    query = db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id).filter(
        MediaFile.status == FileStatus.COMPLETED, MediaFile.id > after_id, voice_named
    )
    if scope.kind == "user":
        query = query.filter(MediaFile.user_id == scope.user_id)
    elif scope.kind == "organization":
        query = query.filter(MediaFile.organization_id == scope.organization_id)
    return [
        (int(r[0]), str(r[1]), int(r[2])) for r in query.order_by(MediaFile.id).limit(limit).all()
    ]


# --- the pending record -------------------------------------------------------------------


def _record(scope: ResyncScope, **extra: Any) -> dict[str, Any]:
    return {
        "kind": scope.kind,
        "organization_id": scope.organization_id,
        "user_id": scope.user_id,
        **extra,
    }


def _scope_of(record: dict[str, Any]) -> ResyncScope:
    return ResyncScope(
        kind=record["kind"],
        organization_id=record.get("organization_id"),
        user_id=record.get("user_id"),
    )


def _lock_pending(db: Session) -> SystemSettings:
    """The pending row, created empty if absent and locked until the caller commits."""
    db.execute(
        pg_insert(SystemSettings)
        .values(
            key=PENDING_KEY,
            value="[]",
            description="Voiceprint switch changes whose index relabelling is not finished",
        )
        .on_conflict_do_nothing(index_elements=[SystemSettings.key])
    )
    return db.execute(
        select(SystemSettings).where(SystemSettings.key == PENDING_KEY).with_for_update()
    ).scalar_one()


def _parse(raw: str | None) -> list[dict[str, Any]]:
    import json

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        logger.error("privacy: the pending label resync record is unreadable; ignoring it")
        return []
    return [r for r in value if isinstance(r, dict) and r.get("kind") in _KINDS]


def read_pending(db: Session) -> list[dict[str, Any]]:
    """The scopes whose relabelling is not finished, oldest first (read only, no lock)."""
    raw = db.execute(
        select(SystemSettings.value).where(SystemSettings.key == PENDING_KEY)
    ).scalar_one_or_none()
    return _parse(raw)


def _write_pending(row: SystemSettings, records: list[dict[str, Any]]) -> None:
    import json

    row.value = json.dumps(records, sort_keys=True)  # type: ignore[assignment]


# --- requesting and running a resync ------------------------------------------------------


def _actor_username(db: Session, actor_user_id: int | None) -> str | None:
    if actor_user_id is None:
        return None
    from app.models.user import User

    return db.execute(select(User.email).where(User.id == actor_user_id)).scalar_one_or_none()


def _audit(
    record: dict[str, Any],
    scope: ResyncScope,
    *,
    phase: str,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    **details: Any,
) -> None:
    """One ``privacy.labels.resync`` event. Counts and ids only, never a name."""
    actor = record.get("actor_user_id")
    audit_logger.log(
        event_type=AuditEventType.PRIVACY_LABEL_RESYNC,
        outcome=outcome,
        user_id=actor,
        username=record.get("actor_username"),
        organization_id=scope.organization_id,
        target_user_id=scope.user_id if scope.user_id not in (None, actor) else None,
        details={
            "phase": phase,
            "scope": scope.kind,
            "organization_id": scope.organization_id,
            "user_id": scope.user_id,
            "switch": record.get("switch"),
            "from": record.get("from"),
            "to": record.get("to"),
            **details,
        },
    )


def request_label_resync(
    db: Session,
    scope: ResyncScope,
    *,
    actor_user_id: int | None,
    switch: str,
    from_value: bool,
    to_value: bool,
) -> None:
    """Record that ``scope`` must be relabelled, audit the request, and dispatch the task.

    Call it AFTER the switch change has committed, and only when the value actually changed.
    The scope is written to the pending record (and committed) before the dispatch, so a
    broker that refuses the task leaves the work recorded: the maintenance arm finishes it.
    Never raises: the switch change is already committed and must not become a failed request.
    A second request for a scope already pending replaces its switch values and actor and
    renews ``requested_at``, so a pass that started before it cannot retire it.
    """
    try:
        username = _actor_username(db, actor_user_id)
        record = _record(
            scope,
            actor_user_id=actor_user_id,
            actor_username=username,
            switch=switch,
            **{"from": from_value, "to": to_value},
            requested_at=datetime.now(UTC).isoformat(),
        )
        row = _lock_pending(db)
        kept = [r for r in _parse(row.value) if _scope_of(r).key != scope.key]
        _write_pending(row, [*kept, record])
        db.commit()
        _audit(record, scope, phase="requested")
    except Exception:
        db.rollback()
        logger.exception("privacy: could not record the label resync of %s", scope)
        return
    _dispatch_task()


def _dispatch_task() -> bool:
    """Hand the pending record to the ``privacy.label_resync`` task. False when it could not."""
    try:
        from app.tasks.privacy_label_resync_task import privacy_label_resync_task

        privacy_label_resync_task.delay()
        return True
    except Exception:
        logger.exception("privacy: could not queue the label resync; maintenance will finish it")
        return False


def _queue_file(
    file_id: int, file_uuid: str, owner_id: int, reindex: Reindex, facts: FactsDispatch
) -> str:
    """Queue one file. Returns the dispatch outcome value (``queued`` / ``already_pending`` / ``failed``)."""
    from app.services.search.reindex_dispatch import ReindexDispatch

    result = reindex(file_id=file_id, file_uuid=file_uuid, user_id=owner_id)
    if result is ReindexDispatch.FAILED:
        return "failed"
    # The facts roster fingerprints the resolved speaker label, so a changed label regenerates
    # it (the same pairing the provenance backfill uses).
    facts(file_id)
    return "queued" if result is ReindexDispatch.QUEUED else "already_pending"


def _resolve_dispatchers(
    reindex: Reindex | None, facts: FactsDispatch | None
) -> tuple[Reindex, FactsDispatch]:
    if reindex is None:
        from app.services.search.reindex_dispatch import queue_transcript_reindex

        reindex = queue_transcript_reindex
    if facts is None:
        from app.tasks.ingest_artifacts_task import dispatch_file_facts

        facts = dispatch_file_facts
    return reindex, facts


def _resync_scope(
    db: Session, scope: ResyncScope, reindex: Reindex, facts: FactsDispatch
) -> ScopeOutcome:
    outcome = ScopeOutcome(scope)
    cursor = 0
    try:
        while True:
            page = files_labelled_by_voiceprints(db, scope, after_id=cursor)
            if not page:
                break
            outcome.pages += 1
            for file_id, file_uuid, owner_id in page:
                outcome.files_selected += 1
                result = _queue_file(file_id, file_uuid, owner_id, reindex, facts)
                if result == "queued":
                    outcome.files_queued += 1
                elif result == "already_pending":
                    outcome.files_already_pending += 1
                else:
                    outcome.files_failed += 1
                    outcome.failed_file_ids.append(file_id)
            cursor = page[-1][0]
            if len(page) < PAGE_SIZE:
                break
    except Exception as exc:
        # A listing or dispatch error mid-scope: keep the scope pending, say what kind.
        logger.exception("privacy: label resync of %s was interrupted", scope)
        outcome.error = type(exc).__name__
    return outcome


def run_label_resync(
    db: Session,
    *,
    reindex: Reindex | None = None,
    facts: FactsDispatch | None = None,
) -> list[ScopeOutcome]:
    """Relabel every pending scope. Idempotent; a failed or interrupted scope stays pending.

    Per scope: page by id (``PAGE_SIZE``), queue each file through the debounced re-index
    (``ALREADY_PENDING`` counts as done), then write one ``privacy.labels.resync`` event with
    the counts it measured. A scope is removed from the pending record only when every file was
    queued and only if it was not requested again while the pass ran.

    Args:
        db: Session. Committed as the pending record changes.
        reindex: The per-file dispatch (``queue_transcript_reindex``); injectable for tests.
        facts: The facts regeneration dispatch (``dispatch_file_facts``).

    Returns:
        One outcome per pending scope, in request order.
    """
    reindex, facts = _resolve_dispatchers(reindex, facts)
    outcomes: list[ScopeOutcome] = []
    for record in read_pending(db):
        scope = _scope_of(record)
        outcome = _resync_scope(db, scope, reindex, facts)
        outcomes.append(outcome)
        _audit(
            record,
            scope,
            phase="completed" if outcome.finished else "incomplete",
            outcome=AuditOutcome.SUCCESS if outcome.finished else AuditOutcome.PARTIAL,
            files_selected=outcome.files_selected,
            files_queued=outcome.files_queued,
            files_already_pending=outcome.files_already_pending,
            files_failed=outcome.files_failed,
            pages=outcome.pages,
            **({"error": outcome.error} if outcome.error else {}),
        )
        if outcome.finished:
            _retire(db, scope, record.get("requested_at"))
        logger.info(
            "privacy: label resync %s: %d selected, %d queued, %d already pending, %d failed",
            scope,
            outcome.files_selected,
            outcome.files_queued,
            outcome.files_already_pending,
            outcome.files_failed,
        )
    return outcomes


def _retire(db: Session, scope: ResyncScope, requested_at: str | None) -> None:
    """Drop ``scope`` from the pending record unless it was requested again since ``requested_at``."""
    row = _lock_pending(db)
    records = _parse(row.value)
    kept = [
        r for r in records if _scope_of(r).key != scope.key or r.get("requested_at") != requested_at
    ]
    _write_pending(row, kept)
    db.commit()


# --- drift: what the index holds against what the policy implies -------------------------


def _expected_labels(db: Session, file_ids: list[int]) -> dict[int, set[str]]:
    """``{file id: labels the policy implies now}`` over the speakers that have segments."""
    from app.services.privacy.redact_view import PrivacyView

    view = PrivacyView(db)
    view.preload_files(file_ids)
    has_segments = exists().where(TranscriptSegment.speaker_id == Speaker.id)
    rows = (
        db.query(
            Speaker.media_file_id,
            Speaker.name,
            Speaker.display_name,
            Speaker.suggested_name,
            Speaker.confidence,
            Speaker.suggestion_source,
            Speaker.identity_source,
        )
        .filter(Speaker.media_file_id.in_(file_ids), has_segments)
        .all()
    )
    expected: dict[int, set[str]] = {fid: set() for fid in file_ids}
    for row in rows:
        file_id = int(row.media_file_id)
        label = canonical_speaker_label_for_row(
            row, voiceprint_names=view.voiceprints(file_id).enabled
        )
        if label not in UNKNOWN_SPEAKER_LABELS:
            expected[file_id].add(label)
    return expected


def indexed_speaker_labels(file_uuids: list[str]) -> dict[str, set[str]]:
    """``{file uuid: distinct speaker labels in its chunk documents}``; ``{}`` if unreachable.

    A file with no chunk documents is absent from the answer (the maintenance task's own
    unindexed-file arm handles it); the unknown-speaker spellings are left out.
    """
    from app.core.config import settings
    from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client or not file_uuids:
        return {}
    try:
        response = opensearch_client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={
                "size": 0,
                "query": {
                    "bool": {"filter": [chunk_plane_clause(), {"terms": {"file_uuid": file_uuids}}]}
                },
                "aggs": {
                    "files": {
                        "terms": {"field": "file_uuid", "size": len(file_uuids)},
                        "aggs": {"labels": {"terms": {"field": "speaker", "size": 500}}},
                    }
                },
            },
        )
    except Exception:
        logger.exception("privacy: could not read the indexed speaker labels for the drift scan")
        return {}
    found: dict[str, set[str]] = {}
    for bucket in response.get("aggregations", {}).get("files", {}).get("buckets", []):
        labels = {str(b["key"]) for b in bucket.get("labels", {}).get("buckets", [])}
        found[str(bucket["key"])] = labels - set(UNKNOWN_SPEAKER_LABELS)
    return found


@dataclass
class DriftOutcome:
    """What one drift scan measured."""

    scanned: int = 0
    drifted: int = 0
    queued: int = 0
    already_pending: int = 0
    failed: int = 0


def detect_label_drift(
    db: Session,
    *,
    indexed_labels: IndexedLabels | None = None,
    reindex: Reindex | None = None,
    facts: FactsDispatch | None = None,
    limit: int = DRIFT_FILES_PER_TICK,
) -> DriftOutcome:
    """Queue the files whose indexed speaker labels disagree with what the policy implies now.

    Looks only at files a voiceprint can name (:func:`files_labelled_by_voiceprints`), resumes
    from a persisted id cursor so a large library is covered over several ticks, and compares
    sets of labels per file (a file whose index holds no chunks is skipped). Writes one audit
    event per scan that found drift.

    Args:
        db: Session; the cursor is committed.
        indexed_labels: ``file uuids -> {uuid: labels}``; defaults to the chunk index.
        reindex: The per-file dispatch.
        facts: The facts regeneration dispatch.
        limit: Files compared in this call.
    """
    from app.services.system_settings_service import get_setting_int
    from app.services.system_settings_service import set_setting

    reindex, facts = _resolve_dispatchers(reindex, facts)
    read_index = indexed_labels or indexed_speaker_labels
    scope = ResyncScope("instance")
    outcome = DriftOutcome()
    cursor = get_setting_int(db, DRIFT_CURSOR_KEY, 0)
    while outcome.scanned < limit:
        want = min(PAGE_SIZE, limit - outcome.scanned)
        page = files_labelled_by_voiceprints(db, scope, after_id=cursor, limit=want)
        if not page:
            cursor = 0  # the whole library was covered: start over next tick
            break
        expected = _expected_labels(db, [f[0] for f in page])
        indexed = read_index([f[1] for f in page])
        for file_id, file_uuid, owner_id in page:
            outcome.scanned += 1
            held = indexed.get(file_uuid)
            if not held or held == expected.get(file_id, set()):
                continue
            outcome.drifted += 1
            result = _queue_file(file_id, file_uuid, owner_id, reindex, facts)
            if result == "queued":
                outcome.queued += 1
            elif result == "already_pending":
                outcome.already_pending += 1
            else:
                outcome.failed += 1
        cursor = page[-1][0]
        if len(page) < want:
            cursor = 0  # fewer than asked for: the end of the library
            break
    set_setting(db, DRIFT_CURSOR_KEY, cursor, "Where the voiceprint label drift scan resumes")
    if outcome.drifted:
        audit_logger.log(
            event_type=AuditEventType.PRIVACY_LABEL_RESYNC,
            outcome=AuditOutcome.SUCCESS if not outcome.failed else AuditOutcome.PARTIAL,
            details={
                "phase": "drift",
                "scope": "instance",
                "files_selected": outcome.scanned,
                "files_drifted": outcome.drifted,
                "files_queued": outcome.queued,
                "files_already_pending": outcome.already_pending,
                "files_failed": outcome.failed,
            },
        )
        logger.warning(
            "privacy: %d of %d scanned file(s) held speaker labels that disagree with the "
            "policy; %d re-index(es) queued",
            outcome.drifted,
            outcome.scanned,
            outcome.queued,
        )
    return outcome
