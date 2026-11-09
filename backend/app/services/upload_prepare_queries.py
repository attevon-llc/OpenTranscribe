"""Statements behind upload preparation: batches, collection membership, tag attachment.

Plain ``Session`` helpers. None of them commits; the upload routes and the importers
that share them keep their own transaction boundaries.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ScalarSelect
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.upload_batch import UploadBatch
from app.services.collection_visibility import usable_collections


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
