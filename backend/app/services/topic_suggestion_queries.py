"""Reads and batch bookkeeping behind the topic-suggestion endpoints.

Functions take a ``Session`` and plain arguments and never commit; the caller owns the
transaction.
"""

import uuid as uuid_pkg
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.topic import TopicSuggestion
from app.models.upload_batch import UploadBatch


def suggestion_for_file(db: Session, media_file_id: int) -> TopicSuggestion | None:
    """The stored AI suggestion row for a file, if one exists."""
    return db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == media_file_id).first()


def link_files_to_new_batch(
    db: Session, file_uuids: Sequence[str], user_id: int
) -> UploadBatch | None:
    """Group ``file_uuids`` under one new ``UploadBatch`` unless they already share exactly one.

    Downstream batch grouping in the extraction task keys on ``upload_batch_id``. Flushes the
    new batch and the file updates but does not commit. Returns the new batch, or None when
    the files already share a single batch.
    """
    files = db.query(MediaFile).filter(MediaFile.uuid.in_(file_uuids)).all()
    batch_ids = {mf.upload_batch_id for mf in files if mf.upload_batch_id}
    if len(batch_ids) == 1:
        return None
    batch = UploadBatch(
        uuid=uuid_pkg.uuid4(),
        user_id=user_id,
        source="batch_extract",
        file_count=len(files),
    )
    db.add(batch)
    db.flush()
    for mf in files:
        mf.upload_batch_id = batch.id
    return batch
