"""Hand a departing member's organization content to an administrator (issue #1104).

Organization content belongs to the organization, not to the person who happened to create it.
When a member is removed, or their account is deleted, the organization-stamped rows they own
are re-owned by an administrator (``user_id`` from -> to) so the account's removal no longer
destroys them. Personal (``organization_id IS NULL``) rows are never touched; they stay with the
person and go with the account.

Scope (plan ``docs/design/1091_data_safety_cluster_plan.md`` §6.2, OD-5): files (trashed ones
included), speakers, speaker profiles/collections/clusters, collections, tags, summary prompts,
custom vocabulary, watch sources and group ownership. Not chats, personal settings or comments.

**Rows that hang off a file follow the file, selected by the file ids that moved** (``_FILE_TABLES``:
speakers, topic suggestions and task records). They are never selected by their own
``organization_id``: the pipeline creates every ``Speaker`` with that column NULL
(``tasks/transcription/speaker_processor``), so a tenant-stamp select matched none of them, and
account deletion then deleted the handed-over files' speakers by ``user_id``, detached their
segments and erased their voiceprints. Selecting by file also leaves a held file's rows with it.

Held files are NOT re-owned: a legal hold freezes the file as it stands, so it is skipped and
named in the result (``skipped_held``) and the audit event. A quarantined file moves like any
other; its quarantine state is untouched.

Every re-owned row gets ``owner_since`` (v471, ``models/ownership``): its creation time is
kept, and the org-member erasure re-run judges a row by when its current owner got it, so
content handed to an already-erased member is not mistaken for a restore of theirs.

The Postgres move and its audit event commit together. The OpenSearch ``user_id`` restamp runs
after the commit as a Celery task with retries (``org_content_transfer.restamp``), selecting by
the ids that moved and idempotent (:func:`restamp_documents`). Account deletion otherwise removes documents
by ``user_id``, which is why the restamp matters.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from kombu.exceptions import OperationalError
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.audit import AuditOutcome
from app.auth.audit_events import TENANT_SCOPE_ORG
from app.auth.audit_events import AuditEventType
from app.models.collection import Collection
from app.models.custom_vocabulary import CustomVocabulary
from app.models.group import UserGroup
from app.models.media import MediaFile
from app.models.media import Task
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerProfile
from app.models.tag import Tag
from app.models.topic import TopicSuggestion
from app.models.user import User
from app.models.watch_source import WatchSource
from app.services import audit_outbox
from app.services.hold_guard import held_pred
from app.services.hold_guard import not_held_pred

logger = logging.getLogger(__name__)

_TERMS_CHUNK = 5000

#: (result key, model, file column). Re-owned when their file moved, whatever their own
#: ``organization_id`` says (module docstring). Comments are absent on purpose (OD-5).
_FILE_TABLES: tuple[tuple[str, Any, str], ...] = (
    ("speakers", Speaker, "media_file_id"),
    ("topic_suggestions", TopicSuggestion, "media_file_id"),
    ("tasks", Task, "media_file_id"),
)

#: (result key, model, owner column). Every one is also filtered by ``organization_id``.
_TABLES: tuple[tuple[str, Any, str], ...] = (
    ("speaker_profiles", SpeakerProfile, "user_id"),
    ("speaker_collections", SpeakerCollection, "user_id"),
    ("speaker_clusters", SpeakerCluster, "user_id"),
    ("collections", Collection, "user_id"),
    ("tags", Tag, "user_id"),
    ("summary_prompts", SummaryPrompt, "user_id"),
    ("custom_vocabulary", CustomVocabulary, "user_id"),
    ("watch_sources", WatchSource, "user_id"),
    ("user_groups", UserGroup, "owner_id"),
)


class TransferError(Exception):
    """A refusal with a stable ``code``: ``organization_not_found``, ``same_user``,
    ``not_an_org_admin`` (the target), ``target_not_active``, ``no_transfer_target``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TransferResult:
    """What moved. ``counts`` holds rows re-owned per kind; ``skipped_held`` the held files left
    with their owner; ``skipped_conflict`` rows left because the target already has one of the
    same name (a unique index), per kind."""

    organization_id: int
    from_user_id: int
    to_user_id: int
    counts: dict[str, int] = field(default_factory=dict)
    skipped_held: int = 0
    skipped_conflict: dict[str, int] = field(default_factory=dict)
    file_ids: list[int] = field(default_factory=list)
    profile_ids: list[int] = field(default_factory=list)
    cluster_uuids: list[str] = field(default_factory=list)
    restamp_queued: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _active_admin_ids(db: Session, organization_id: int, *, excluding: int) -> list[int]:
    """Active ``org:admin`` user ids, longest-standing membership first."""
    rows = db.execute(
        select(OrganizationMembership.user_id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == "org:admin",
            OrganizationMembership.user_id != excluding,
            User.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at, OrganizationMembership.id)
    )
    return [int(row[0]) for row in rows]


def default_target(db: Session, organization_id: int, *, from_user_id: int) -> int | None:
    """The organization's owner when that is another active admin, else the longest-standing
    active admin who is not ``from_user_id``; None when there is nobody."""
    admins = _active_admin_ids(db, organization_id, excluding=from_user_id)
    owner = db.query(Organization.owner_user_id).filter(Organization.id == organization_id).scalar()
    if owner is not None and int(owner) in admins:
        return int(owner)
    return admins[0] if admins else None


def has_org_content(db: Session, organization_id: int, user_id: int) -> bool:
    """Whether ``user_id`` owns anything organization-stamped that a transfer would move."""
    if (
        db.query(MediaFile.id)
        .filter(MediaFile.organization_id == organization_id, MediaFile.user_id == user_id)
        .first()
    ):
        return True
    for _key, model, column in _TABLES:
        if (
            db.query(model.id)
            .filter(model.organization_id == organization_id, getattr(model, column) == user_id)
            .first()
        ):
            return True
    return False


def _validate_target(db: Session, organization_id: int, from_user_id: int, to_user_id: int) -> None:
    if to_user_id == from_user_id:
        raise TransferError("same_user", "The target must be a different person.")
    if to_user_id not in _active_admin_ids(db, organization_id, excluding=from_user_id):
        is_admin = (
            db.query(OrganizationMembership.id)
            .filter(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == to_user_id,
                OrganizationMembership.role == "org:admin",
            )
            .first()
        )
        if is_admin:
            raise TransferError("target_not_active", "The target's account is not active.")
        raise TransferError(
            "not_an_org_admin", "The target must be an administrator of this organization."
        )


def _move(
    db: Session, model: Any, column: str, ids: Sequence[int | str], to_user_id: int
) -> tuple[int, int]:
    """Re-own ``ids``; returns (moved, skipped_conflict).

    One bulk UPDATE in a savepoint. A unique name index (a profile, collection, vocabulary term
    or group the target already has) makes it fail whole; then each row gets its own savepoint
    and only the colliding ones stay behind, reported, never renamed.
    """
    if not ids:
        return 0, 0
    savepoint = db.begin_nested()
    try:
        for start in range(0, len(ids), _TERMS_CHUNK):
            db.execute(
                _reown(model, column, model.id.in_(ids[start : start + _TERMS_CHUNK]), to_user_id)
            )
        savepoint.commit()
        return len(ids), 0
    except IntegrityError:
        savepoint.rollback()
    moved = 0
    for row_id in ids:
        savepoint = db.begin_nested()
        try:
            db.execute(_reown(model, column, model.id == row_id, to_user_id))
            savepoint.commit()
            moved += 1
        except IntegrityError:
            savepoint.rollback()
            logger.info("Transfer left %s %s with its owner (name conflict)", model, row_id)
    return moved, len(ids) - moved


def _reown(model: Any, column: str, where: Any, to_user_id: int) -> Any:
    # The wall clock, not the transaction's now(): owner_since must read after an erasure that
    # completed while this transaction was open (models/ownership).
    return (
        update(model)
        .where(where)
        .values({column: to_user_id, "owner_since": func.clock_timestamp()})
        .execution_options(synchronize_session=False)
    )


def _move_file_rows(db: Session, result: TransferResult) -> None:
    """Re-own the departing member's rows on the files that moved (``_FILE_TABLES``)."""
    for key, model, file_column in _FILE_TABLES:
        ids: list[int | str] = []  # task ids are Celery ids (str)
        for chunk in _chunks(result.file_ids):
            ids.extend(
                row[0]
                for row in db.execute(
                    select(model.id).where(
                        getattr(model, file_column).in_(chunk),
                        model.user_id == result.from_user_id,
                    )
                )
            )
        moved, skipped = _move(db, model, "user_id", ids, result.to_user_id)
        result.counts[key] = moved
        if skipped:
            result.skipped_conflict[key] = skipped


def transfer_org_content(
    db: Session,
    *,
    organization_id: int,
    from_user_id: int,
    to_user_id: int | None = None,
    actor: User | None = None,
    client: dict[str, Any] | None = None,
    reason: str = "member_removed",
) -> TransferResult:
    """Re-own ``from_user_id``'s content of one organization; commit; restamp the search index.

    Args:
        db: Session; committed by this function.
        organization_id: The only tenant touched. Rows of other organizations and personal rows
            are never selected.
        from_user_id: The departing member.
        to_user_id: The receiving administrator; :func:`default_target` when omitted.
        actor: The administrator acting, None for a system call (account deletion).
        client: Request context for the audit event.
        reason: ``member_removed`` | ``account_deleted``; recorded.

    Raises:
        TransferError: nothing is written on a refusal.
    """
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org is None:
        raise TransferError("organization_not_found", "Organization not found.")
    if to_user_id is None:
        to_user_id = default_target(db, organization_id, from_user_id=from_user_id)
        if to_user_id is None:
            raise TransferError(
                "no_transfer_target", "The organization has no other active administrator."
            )
    else:
        _validate_target(db, organization_id, from_user_id, to_user_id)

    result = TransferResult(organization_id, from_user_id, to_user_id)
    owned_files = select(MediaFile.id).where(
        MediaFile.organization_id == organization_id, MediaFile.user_id == from_user_id
    )
    result.file_ids = [int(r[0]) for r in db.execute(owned_files.where(not_held_pred()))]
    result.skipped_held = int(
        db.query(MediaFile.id)
        .filter(
            MediaFile.organization_id == organization_id,
            MediaFile.user_id == from_user_id,
            held_pred(),
        )
        .count()
    )
    moved, _ = _move(db, MediaFile, "user_id", result.file_ids, to_user_id)
    result.counts["files"] = moved
    _move_file_rows(db, result)

    for key, model, column in _TABLES:
        ids = [
            int(r[0])
            for r in db.execute(
                select(model.id).where(
                    model.organization_id == organization_id,
                    getattr(model, column) == from_user_id,
                )
            )
        ]
        moved, skipped = _move(db, model, column, ids, to_user_id)
        result.counts[key] = moved
        if skipped:
            result.skipped_conflict[key] = skipped
        if key == "speaker_profiles" and ids:
            result.profile_ids = [
                int(r[0])
                for r in db.execute(
                    select(SpeakerProfile.id).where(
                        SpeakerProfile.id.in_(ids), SpeakerProfile.user_id == to_user_id
                    )
                )
            ]
        elif key == "speaker_clusters" and ids:
            result.cluster_uuids = [
                str(r[0])
                for r in db.execute(
                    select(SpeakerCluster.uuid).where(
                        SpeakerCluster.id.in_(ids), SpeakerCluster.user_id == to_user_id
                    )
                )
            ]

    ctx = client or {}
    audit_outbox.stage(
        db,
        event_type=AuditEventType.ORG_CONTENT_TRANSFER,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(actor.id) if actor is not None else None,
        source_ip=ctx.get("source_ip"),
        user_agent=ctx.get("user_agent"),
        organization_id=organization_id,
        target_user_id=to_user_id,
        resource_type="organization",
        resource_uuid=str(org.uuid),
        tenant_scope=TENANT_SCOPE_ORG,
        details={
            "reason": reason,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "counts": result.counts,
            "skipped_held": result.skipped_held,
            "skipped_conflict": result.skipped_conflict,
            "actor_kind": "admin" if actor is not None else "system",
        },
    )
    db.commit()
    audit_outbox.deliver_staged(db)
    result.restamp_queued = dispatch_restamp(result)
    return result


def _chunks(items: list[Any]) -> list[list[Any]]:
    return [items[i : i + _TERMS_CHUNK] for i in range(0, len(items), _TERMS_CHUNK)]


class RestampIncompleteError(Exception):
    """Some documents were left unstamped (a version conflict with a concurrent write, or a
    per-document failure). Raised so the task's retry runs the same idempotent restamp again."""


def restamp_documents(
    *,
    to_user_id: int,
    file_ids: list[int],
    profile_ids: list[int],
    cluster_uuids: list[str],
    client: Any = None,
) -> int:
    """Point the moved rows' search documents at the new owner; returns documents updated.

    Per-file documents (chunks, digests, summaries, transcripts, per-file speaker documents) are
    selected by the ids that moved, never by the old ``user_id`` alone: a held file kept by the
    departing member shares that ``user_id`` and must keep it. Each query also excludes documents
    already on ``to_user_id``, so a re-run (a Celery retry, a replayed message) changes nothing.

    Raises:
        OpenSearchException: transport or index errors (the task retries them).
        RestampIncompleteError: documents were skipped by a conflict or failure.
    """
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices
    from app.services.opensearch_service.update_by_query import update_by_query
    from app.services.search.availability_stamp import stamp_targets

    if client is None:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
    if client is None:
        return 0
    script = {
        "source": "ctx._source.user_id = params.to",
        "lang": "painless",
        "params": {"to": to_user_id},
    }
    not_yet = {"must_not": [{"term": {"user_id": to_user_id}}]}
    updated = 0
    incomplete = False

    def run(index: str, filters: list[dict[str, Any]]) -> None:
        nonlocal updated, incomplete
        response = update_by_query(client, index, {"bool": {"filter": filters, **not_yet}}, script)
        updated += int(response.get("updated", 0))
        incomplete |= bool(response.get("failures") or int(response.get("version_conflicts", 0)))

    for target in stamp_targets():
        if not client.indices.exists(index=target.index):
            continue
        for chunk in _chunks(file_ids):
            run(target.index, [{"terms": {target.id_field: chunk}}])
    for index in speaker_embedding_indices():
        if not client.indices.exists(index=index):
            continue
        for doc_type, field_name, ids in (
            ("profile", "profile_id", profile_ids),
            ("cluster", "cluster_uuid", cluster_uuids),
        ):
            for chunk in _chunks(list(ids)):
                run(index, [{"term": {"document_type": doc_type}}, {"terms": {field_name: chunk}}])
    if incomplete:
        raise RestampIncompleteError("some documents were not restamped")
    return updated


RESTAMP_TASK = "org_content_transfer.restamp"


def dispatch_restamp(result: TransferResult) -> bool:
    """Queue the durable restamp after the commit; True when every message was accepted.

    One message per ``_TERMS_CHUNK`` of files (profile and cluster ids ride the first), by task
    name so the service layer does not import the task layer. The task retries on its own
    (``tasks/org_content_transfer_task``); a broker outage here is the only way a transfer ends
    without a restamp queued, and the caller sees ``False``.
    """
    from app.core.celery import celery_app

    chunks = _chunks(result.file_ids) or [[]]
    try:
        for position, files in enumerate(chunks):
            first = position == 0
            celery_app.send_task(
                RESTAMP_TASK,
                kwargs={
                    "to_user_id": result.to_user_id,
                    "file_ids": files,
                    "profile_ids": result.profile_ids if first else [],
                    "cluster_uuids": result.cluster_uuids if first else [],
                },
            )
    except OperationalError as error:
        logger.error("Content transfer restamp not dispatched: %s", error)
        return False
    return True
