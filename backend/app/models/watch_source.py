"""SQLAlchemy models for Watch Sources (auto-import from local / S3 / SMB).

A ``WatchSource`` is a unified configuration row discriminated by
``source_type`` (``local`` | ``s3`` | ``smb``); per-type columns hold the
connection details and credentials (secrets AES-256-GCM encrypted, mirroring
``user_media_source``/``user_asr_settings``). Each tracked file the scanner has
seen is a ``WatchSourceFile`` row, which records the dedup fingerprint, import
status, skip reason, and (on success) a link to the created ``MediaFile`` or
``Document`` — whichever the scanned file turned out to be.
"""

import uuid as uuid_pkg
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.email_notification_config import WatchSourceEmail
    from app.models.media import MediaFile
    from app.models.user import User

# Default regex for multi-part split recordings: name_P001.ext, name_P002.ext …
DEFAULT_MULTIPART_REGEX = r"^(.+?)_P(\d{3})(\.[^.]+)$"


class WatchSource(Base):
    """A configured source that is polled for new media to auto-import."""

    __tablename__ = "watch_source"

    # ----- identity -----
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # local | s3 | smb
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ----- local -----
    local_path: Mapped[str | None] = mapped_column(
        String(2000), nullable=True
    )  # relative to WATCH_FOLDER_PATH
    delete_after_import: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ----- s3 -----
    s3_endpoint_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    s3_bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_prefix: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    s3_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    s3_access_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_s3_secret_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted
    s3_use_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ----- smb -----
    smb_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_share: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_path: Mapped[str | None] = mapped_column(String(2000), nullable=True, default="/")
    smb_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_smb_password: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted
    smb_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_port: Mapped[int] = mapped_column(Integer, default=445, nullable=False)

    # ----- processing -----
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )  # owner of imported files
    # Cloud-edition seam: tenant scope captured ONCE at source-creation time from
    # the creating request's context (NULL = personal, always NULL in community).
    # Every import this source produces is stamped with this org — background
    # scans never guess the tenant from the owner's memberships (issue #262c).
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True
    )
    polling_interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    use_fs_events: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )  # local-only watchdog opt-in
    file_extensions: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # CSV, e.g. ".mp4,.mp3"; null → all media
    # No column default: null must mean "no age skip". New-source default (30) is
    # supplied by the Pydantic schema, so an explicit null from the UI is stored
    # as null (a column default=30 would silently override it — see issue #26).
    skip_files_older_than_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_transcribe: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_speakers: Mapped[int | None] = mapped_column(Integer, default=1, nullable=True)
    max_speakers: Mapped[int | None] = mapped_column(Integer, default=20, nullable=True)
    collection_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True
    )  # list[str] of collection UUIDs
    tag_names: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[str]

    # ----- multipart stitching -----
    multipart_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multipart_regex: Mapped[str] = mapped_column(
        String(500), nullable=False, default=DEFAULT_MULTIPART_REGEX
    )
    multipart_time_window_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    multipart_wait_scans: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    upload_stitched_to_source: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ----- status (last scan) -----
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # success | error | running
    last_scan_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scan_files_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scan_files_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scan_files_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scan_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_files_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ----- audit -----
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ----- relationships -----
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    files: Mapped[list["WatchSourceFile"]] = relationship(
        "WatchSourceFile", back_populates="watch_source", cascade="all, delete-orphan"
    )
    email_links: Mapped[list["WatchSourceEmail"]] = relationship(
        "WatchSourceEmail", back_populates="watch_source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "ix_watch_source_enabled",
            "is_enabled",
            postgresql_where=text("is_enabled = TRUE"),
        ),
    )

    @property
    def has_s3_secret_key(self) -> bool:
        return bool(self.encrypted_s3_secret_key)

    @property
    def has_smb_password(self) -> bool:
        return bool(self.encrypted_smb_password)

    @property
    def resolved_local_path(self):
        """Absolute, traversal-guarded path for a local source.

        Returns the resolved ``WATCH_FOLDER_PATH / local_path`` and raises
        ``ValueError`` if the result escapes the configured mount (symlink or
        ``..`` traversal) or the feature is not configured.
        """
        import os

        from app.core.config import settings

        if not settings.WATCH_FOLDER_PATH:
            raise ValueError("WATCH_FOLDER_PATH is not configured")
        base = Path(settings.WATCH_FOLDER_PATH).resolve()
        rel = (self.local_path or "").lstrip("/")
        candidate = Path(os.path.realpath(base / rel))
        if base != candidate and base not in candidate.parents:
            raise ValueError(f"Resolved path {candidate} escapes watch root {base}")
        return candidate

    def __repr__(self) -> str:
        return (
            f"<WatchSource(id={self.id}, name={self.name!r}, type={self.source_type}, "
            f"enabled={self.is_enabled})>"
        )


class WatchSourceFile(Base):
    """A single file the scanner has observed in a watch source."""

    __tablename__ = "watch_source_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    watch_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("watch_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remote_path: Mapped[str] = mapped_column(String(2000), nullable=False)  # path within the source
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imohash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    media_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="SET NULL"), nullable=True, index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    skip_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    part_group: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )  # base name for a multi-part group
    part_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    watch_source: Mapped["WatchSource"] = relationship("WatchSource", back_populates="files")
    media_file: Mapped["MediaFile | None"] = relationship("MediaFile", foreign_keys=[media_file_id])
    document: Mapped["Document | None"] = relationship("Document", foreign_keys=[document_id])

    # A unique INDEX, not a UniqueConstraint: ``v366`` created it with
    # ``CREATE UNIQUE INDEX``, so ``pg_constraint`` holds nothing by this name.
    # Same enforcement, different object class — see ``email_notification_config``.
    __table_args__ = (
        Index("_watch_source_file_path_unique", "watch_source_id", "remote_path", unique=True),
        Index("ix_watch_source_file_source_imohash", "watch_source_id", "imohash"),
        Index("ix_watch_source_file_part_group", "part_group", "watch_source_id"),
        Index("ix_watch_source_file_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<WatchSourceFile(id={self.id}, source={self.watch_source_id}, "
            f"path={self.remote_path!r}, status={self.status})>"
        )
