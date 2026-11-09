"""Clearing a file's stored summary columns."""

from collections.abc import Collection

from sqlalchemy.orm import Session

from app.models.media import MediaFile

SUMMARY_COLUMNS = ("summary_data", "summary_opensearch_id")


def clear_summary_columns(db: Session, file_id: int, columns: Collection[str]) -> None:
    """Set the named summary columns of file ``file_id`` to NULL; does not commit.

    Raises:
        ValueError: A name in ``columns`` is not one of ``SUMMARY_COLUMNS``.
    """
    unknown = set(columns) - set(SUMMARY_COLUMNS)
    if unknown:
        msg = f"not summary columns: {sorted(unknown)}"
        raise ValueError(msg)
    db.query(MediaFile).filter(MediaFile.id == file_id).update(
        {getattr(MediaFile, name): None for name in columns}, synchronize_session=False
    )
