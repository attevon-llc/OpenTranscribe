"""Statements behind upload preparation: batches, collection membership, tag attachment.

Plain ``Session`` helpers. None of them commits; the upload routes and the importers
that share them keep their own transaction boundaries.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import ScalarSelect
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import ConflictError
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.upload_batch import UploadBatch
from app.services.collection_visibility import usable_collections
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tags

logger = logging.getLogger(__name__)


class UploadBatchInUseError(ConflictError):
    """The client-chosen batch UUID already belongs to another user."""


def insert_upload_batch_if_absent(db: Session, batch_uuid: UUID, user_id: int, source: str) -> bool:
    """Create the batch row unless its UUID exists; True when this call created it.

    Atomic: parallel prepare requests of one multi-file upload share the uuid, so a
    read-then-insert would let two requests both miss and the loser hit the unique index.
    A concurrent uncommitted insert makes this statement wait for it, then do nothing.
    """
    inserted = db.execute(
        pg_insert(UploadBatch)
        .values(uuid=batch_uuid, user_id=user_id, source=source, file_count=0)
        .on_conflict_do_nothing(index_elements=[UploadBatch.uuid])
        .returning(UploadBatch.id)
    ).first()
    return inserted is not None


def get_upload_batch(db: Session, batch_uuid: UUID) -> UploadBatch:
    """The batch with this public UUID (raises if absent)."""
    return db.query(UploadBatch).filter(UploadBatch.uuid == batch_uuid).one()


def bump_upload_batch_file_count(db: Session, batch_id: int) -> None:
    """Atomically add one to ``file_count`` (no read-modify-write, safe under concurrency)."""
    db.execute(
        update(UploadBatch)
        .where(UploadBatch.id == batch_id)
        .values(file_count=UploadBatch.file_count + 1)
    )


def usable_collections_in_file_tenant(
    db: Session, user_id: int, file_id: int, collection_uuids: list[str]
) -> list[Collection]:
    """The named collections that belong to the FILE's tenant and ``user_id`` may edit.

    The tenant is a correlated subquery, not a separate SELECT, so the lookup stays one
    round trip (issue #284 A2.8).
    """
    file_org: ScalarSelect[Any] = (
        select(MediaFile.organization_id).where(MediaFile.id == file_id).scalar_subquery()
    )
    return usable_collections(db, user_id, file_org, collection_uuids=collection_uuids)


def member_collection_ids(db: Session, collection_ids: list[int], file_id: int) -> set[int]:
    """Which of ``collection_ids`` already contain the file."""
    return {
        row[0]
        for row in db.query(CollectionMember.collection_id).filter(
            CollectionMember.collection_id.in_(collection_ids),
            CollectionMember.media_file_id == file_id,
        )
    }


def attach_tags_to_file(db: Session, file_id: int, tag_ids: list[int]) -> None:
    """Attach tags to a file as manual tags, skipping any already attached.

    ON CONFLICT DO NOTHING on (media_file_id, tag_id): an already-attached tag, or one a
    concurrent caller attaches first, is skipped instead of failing the insert.
    """
    db.execute(
        pg_insert(FileTag)
        .values(
            [
                {"media_file_id": file_id, "tag_id": tag_id, "source": TAG_SOURCE_MANUAL}
                for tag_id in dict.fromkeys(tag_ids)
            ]
        )
        .on_conflict_do_nothing(index_elements=[FileTag.media_file_id, FileTag.tag_id])
    )


def get_or_create_upload_batch(
    db: Session, batch_uuid: UUID, user_id: int, source: str = "multi_upload"
) -> UploadBatch:
    """Get an existing UploadBatch by UUID or create a new one.

    Uses the client-provided UUID as the batch identifier. If a batch with
    that UUID already exists for this user, returns it. Otherwise creates a new one.
    Raises UploadBatchInUseError if the UUID belongs to a different user.

    Args:
        db: Database session
        batch_uuid: Client-generated UUID for the batch
        user_id: Owner user ID
        source: Upload source type (multi_upload, playlist, url_batch)

    Returns:
        UploadBatch record
    """
    created = insert_upload_batch_if_absent(db, batch_uuid, user_id, source)
    batch = get_upload_batch(db, batch_uuid)
    if batch.user_id != user_id:
        # Never link a file to a batch another user owns.
        raise UploadBatchInUseError("Upload batch id is already in use")
    if created:
        logger.info(f"Created upload batch {batch_uuid} for user {user_id}")
    return batch


def add_file_to_collections(
    db: Session, file_id: int, user_id: int, collection_ids: list[UUID]
) -> None:
    """Add a media file to the named collections of the file's tenant.

    Only collections of the FILE's tenant qualify (issue #1051): for a personal
    file, ``user_id``'s own personal collections; for an organization file, the
    organization's collections, provided ``user_id`` is still a member. Anything
    else — another tenant's collection, a stranger's — is silently skipped, so an
    import can never put one tenant's recording into another tenant's collection.

    Two queries regardless of how many collections are named (issue #284 A2.8). This
    used to run a `Collection` lookup and a `CollectionMember` existence check per
    UUID — 2N round trips on the upload-prep path, which the SPA calls once per file
    in a multi-file upload, so a 50-file batch across 5 collections paid 500 queries.
    Bounded N is not unbounded, but it is on the hot upload path and the batched form
    is no harder to read.
    """
    if not collection_ids:
        return

    wanted = [str(coll_uuid) for coll_uuid in collection_ids]

    # A collection qualifies when it belongs to the file's tenant and ``user_id`` may edit it
    # (collection_visibility.usable_collections), resolved in one round trip.
    collections = usable_collections_in_file_tenant(db, user_id, file_id, wanted)
    by_uuid = {str(collection.uuid): collection for collection in collections}

    for coll_uuid in wanted:
        if coll_uuid not in by_uuid:
            logger.warning(f"Collection {coll_uuid} not found for user {user_id}, skipping")

    resolved_ids = [collection.id for collection in collections]
    if not resolved_ids:
        db.flush()
        return

    already_member = member_collection_ids(db, resolved_ids, file_id)

    # dict.fromkeys keeps insertion order while de-duplicating a repeated UUID —
    # the per-UUID loop relied on the just-added row being visible to the next
    # existence check, which autoflush=False sessions do not guarantee.
    for collection_id in dict.fromkeys(resolved_ids):
        if collection_id not in already_member:
            db.add(CollectionMember(collection_id=collection_id, media_file_id=file_id))

    db.flush()


def add_tags_to_file(db: Session, file_id: int, tag_names: list[str], user_id: int) -> None:
    """Add tags to a media file, creating tags if they don't exist.

    ``user_id`` owns (or, on an organization file, is credited with) any tag
    this creates. Background importers (watch sources, yt-dlp playlists) must
    pass the **file owner**, never leave it unset — a tag with neither owner nor
    tenant is a system tag and would be published to every account.
    Tags resolve in the **file's tenant** (``MediaFile.organization_id``), read
    here rather than taken from each caller, so no importer can land an org
    file's tags in someone's personal vocabulary (issue #1050). A same-named
    system tag is reused rather than forked, so applying a seeded default still
    attaches the shared row.

    Resolving the names (normalize, match normalized-exact, insert under a
    SAVEPOINT) runs through the one implementation all tag creation shares, in
    ``app/services/tag_service.py``, and stays batched — a constant number of
    SELECTs regardless of list length (issue #284 A2.8), not 2N. These names
    were typed by a person, so the fuzzy suggestion lookup is deliberately not
    consulted.
    """
    # Session.get, not a query: every caller has the row in the identity map
    # already, so this costs no SELECT (#284 A2.8 pins the count).
    media_file = db.get(MediaFile, file_id)
    organization_id = media_file.organization_id if media_file is not None else None
    tag_ids = [
        tag.id
        for tag in resolve_or_create_tags(
            db, tag_names, user_id=user_id, organization_id=organization_id
        )
    ]

    if not tag_ids:
        db.flush()
        return

    db.flush()
    attach_tags_to_file(db, file_id, tag_ids)

    on_tags_changed(db, [file_id], user_id=user_id)
