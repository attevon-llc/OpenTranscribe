import logging
import uuid as uuid_lib
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.endpoints.auth import get_current_user
from app.api.endpoints.files.upload import create_media_file_record
from app.core.constants import TAG_SOURCE_MANUAL
from app.db.base import get_db
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import Tag
from app.models.upload_batch import UploadBatch
from app.models.user import User
from app.schemas.media import PrepareUploadRequest
from app.services.auto_label_service import AutoLabelService
from app.utils import benchmark_timing
from app.utils.file_hash import check_duplicate_by_hash
from app.utils.file_hash import cleanup_failed_duplicates

logger = logging.getLogger(__name__)

router = APIRouter()


class FileMetadata:
    """Lightweight class containing essential file information without the actual file content.
    Used for creating database records before the actual file upload starts.

    Attributes:
        filename: Name of the file to be uploaded
        content_type: MIME type of the file
        file_hash: Hash of the file for duplicate detection
        extracted_from_video: Optional metadata from original video if audio was extracted client-side
    """

    def __init__(
        self,
        filename: str,
        content_type: str,
        extracted_from_video: dict[str, Any] | None = None,
    ):
        self.filename = filename
        self.content_type = content_type
        self.file_hash: str | None = None
        self.extracted_from_video = extracted_from_video


def get_or_create_upload_batch(
    db: Session, batch_uuid: UUID, user_id: int, source: str = "multi_upload"
) -> UploadBatch:
    """Get an existing UploadBatch by UUID or create a new one.

    Uses the client-provided UUID as the batch identifier. If a batch with
    that UUID already exists for this user, returns it. Otherwise creates a new one.

    Args:
        db: Database session
        batch_uuid: Client-generated UUID for the batch
        user_id: Owner user ID
        source: Upload source type (multi_upload, playlist, url_batch)

    Returns:
        UploadBatch record
    """
    batch = (
        db.query(UploadBatch)
        .filter(UploadBatch.uuid == batch_uuid, UploadBatch.user_id == user_id)
        .first()
    )
    if batch:
        return batch  # type: ignore[return-value, no-any-return]

    batch = UploadBatch(
        uuid=batch_uuid,
        user_id=user_id,
        source=source,
        file_count=0,
    )
    db.add(batch)
    db.flush()
    logger.info(f"Created upload batch {batch_uuid} for user {user_id}")
    return batch  # type: ignore[return-value, no-any-return]


def add_file_to_collections(
    db: Session, file_id: int, user_id: int, collection_ids: list[UUID]
) -> None:
    """Add a media file to the specified collections owned by the user.

    Silently skips collections that don't exist or aren't owned by the user.
    """
    for coll_uuid in collection_ids:
        collection = (
            db.query(Collection)
            .filter(Collection.uuid == str(coll_uuid), Collection.user_id == user_id)
            .first()
        )
        if not collection:
            logger.warning(f"Collection {coll_uuid} not found for user {user_id}, skipping")
            continue

        existing = (
            db.query(CollectionMember)
            .filter(
                CollectionMember.collection_id == collection.id,
                CollectionMember.media_file_id == file_id,
            )
            .first()
        )
        if not existing:
            db.add(CollectionMember(collection_id=collection.id, media_file_id=file_id))

    db.flush()


def add_tags_to_file(db: Session, file_id: int, tag_names: list[str]) -> None:
    """Add tags to a media file, creating tags if they don't exist.

    Uses SAVEPOINTs for race-condition safety without corrupting the
    enclosing transaction.
    """
    for name in tag_names:
        name = name.strip()[:50]
        if not name:
            continue

        normalized = AutoLabelService.normalize_name(name)

        tag = db.query(Tag).filter(Tag.name == name).first()
        if not tag:
            try:
                nested = db.begin_nested()
                tag = Tag(name=name, source=TAG_SOURCE_MANUAL, normalized_name=normalized)
                db.add(tag)
                db.flush()
            except IntegrityError:
                nested.rollback()
                tag = db.query(Tag).filter(Tag.name == name).first()
                if not tag:
                    continue

        existing = (
            db.query(FileTag)
            .filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag.id)
            .first()
        )
        if not existing:
            db.add(FileTag(media_file_id=file_id, tag_id=tag.id, source=TAG_SOURCE_MANUAL))

    db.flush()


@router.post("/prepare", response_model=dict[str, Any])
async def prepare_upload(
    request: PrepareUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Prepare for a file upload by creating a MediaFile record and returning the file ID.
    This allows the frontend to track the file ID before the actual upload begins.

    If a file hash is provided, check if a duplicate file already exists. When
    ``use_presigned`` is true, the response additionally includes an
    application-level ``task_id`` and a presigned PUT URL for the browser to
    upload bytes directly to MinIO, bypassing the API container entirely.
    """
    try:
        # If file hash is provided, check for duplicates
        duplicate_id: str | None = None
        if request.file_hash:
            # First clean up any failed files with the same hash to allow re-upload
            await cleanup_failed_duplicates(db, request.file_hash, current_user.id)

            duplicate_id = await check_duplicate_by_hash(db, request.file_hash, current_user.id)

            if duplicate_id:
                logger.info(
                    f"Duplicate file found for {request.filename} (Duplicate ID: {duplicate_id})"
                )
                return {"file_id": duplicate_id, "is_duplicate": 1}

        # Create file metadata object with information needed for the record
        file_metadata = FileMetadata(
            request.filename,
            request.content_type,
            extracted_from_video=request.extracted_from_video,
        )
        file_metadata.file_hash = request.file_hash

        # Create the database record
        db_file = create_media_file_record(db, file_metadata, current_user, request.file_size)  # type: ignore[arg-type]

        # Generate and set storage_path immediately for duplicate detection
        # This allows future uploads with the same file to recognize it as a duplicate
        from app.utils.filename import get_safe_storage_filename

        storage_path = get_safe_storage_filename(request.filename, current_user.id, db_file.id)
        db_file.storage_path = storage_path  # type: ignore[assignment]

        # Store the user's requested whisper model (if any)
        if request.whisper_model:
            db_file.requested_whisper_model = request.whisper_model  # type: ignore[assignment]

        db.flush()

        # If this is extracted audio, store the video metadata in metadata_important
        if request.extracted_from_video:
            db_file.metadata_important = request.extracted_from_video  # type: ignore[assignment]
            db.commit()
            logger.info(f"Stored extracted video metadata for {request.filename}")

        # Link file to upload batch if a batch UUID was provided
        if request.upload_batch_id:
            batch = get_or_create_upload_batch(
                db, request.upload_batch_id, current_user.id, source="multi_upload"
            )
            db_file.upload_batch_id = batch.id  # type: ignore[assignment]
            batch.file_count = (batch.file_count or 0) + 1  # type: ignore[assignment]
            db.flush()
            logger.info(f"Linked file {db_file.id} to upload batch {request.upload_batch_id}")

        # Assign to collections if specified
        if request.collection_ids:
            add_file_to_collections(db, db_file.id, current_user.id, request.collection_ids)

        # Apply tags if specified
        if request.tag_names:
            add_tags_to_file(db, db_file.id, request.tag_names)

        # Commit all assignments (batch, collections, tags)
        db.commit()

        response: dict[str, Any] = {"file_id": str(db_file.uuid), "is_duplicate": 0}

        # Optional: emit a presigned PUT URL so the browser can upload bytes
        # directly to MinIO. The caller follows up with POST /files/complete
        # once the PUT succeeds. We mint the application task_id here so all
        # HTTP-phase markers share the benchmark:{task_id} Redis hash with
        # the downstream pipeline.
        if request.use_presigned:
            from app.services.minio_service import presigned_put_url

            task_id = str(uuid_lib.uuid4())
            put_url = await run_in_threadpool(presigned_put_url, storage_path)
            benchmark_timing.mark(task_id, "prepare_upload_end")
            benchmark_timing.set_context(
                task_id,
                {
                    "file_size_bytes": int(request.file_size or 0),
                    "content_type": request.content_type or "",
                    "http_flow": "presigned",
                },
            )
            response.update(
                {
                    "task_id": task_id,
                    "upload_url": put_url,
                    "upload_method": "PUT",
                    "http_flow": "presigned",
                    "storage_path": storage_path,
                }
            )

        logger.info(f"Prepared upload for file {request.filename} (ID: {db_file.id})")
        return response

    except Exception as e:
        logger.error(f"Error preparing upload: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error preparing upload: {str(e)}",
        ) from e
