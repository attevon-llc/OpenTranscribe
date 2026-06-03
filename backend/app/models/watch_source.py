"""SQLAlchemy models for Watch Sources (auto-import from local / S3 / SMB).

A ``WatchSource`` is a unified configuration row discriminated by
``source_type`` (``local`` | ``s3`` | ``smb``); per-type columns hold the
connection details and credentials (secrets AES-256-GCM encrypted, mirroring
``user_media_source``/``user_asr_settings``). Each tracked file the scanner has
seen is a ``WatchSourceFile`` row, which records the dedup fingerprint, import
status, skip reason, and (on success) a link to the created ``MediaFile``.
"""

import uuid as uuid_pkg

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

# Default regex for multi-part split recordings: name_P001.ext, name_P002.ext …
DEFAULT_MULTIPART_REGEX = r"^(.+?)_P(\d{3})(\.[^.]+)$"


class WatchSource(Base):
    """A configured source that is polled for new media to auto-import."""

    __tablename__ = "watch_source"

    # ----- identity -----
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String(200), nullable=False)
    source_type = Column(String(20), nullable=False)  # local | s3 | smb
    is_enabled = Column(Boolean, default=True, nullable=False)

    # ----- local -----
    local_path = Column(String(2000), nullable=True)  # relative to WATCH_FOLDER_PATH
    delete_after_import = Column(Boolean, default=False, nullable=False)

    # ----- s3 -----
    s3_endpoint_url = Column(String(500), nullable=True)
    s3_bucket_name = Column(String(255), nullable=True)
    s3_prefix = Column(String(1000), nullable=True)
    s3_region = Column(String(100), nullable=True)
    s3_access_key_id = Column(Text, nullable=True)
    encrypted_s3_secret_key = Column(Text, nullable=True)  # AES-256-GCM encrypted
    s3_use_ssl = Column(Boolean, default=True, nullable=False)

    # ----- smb -----
    smb_server = Column(String(255), nullable=True)
    smb_share = Column(String(255), nullable=True)
    smb_path = Column(String(2000), nullable=True, default="/")
    smb_username = Column(String(255), nullable=True)
    encrypted_smb_password = Column(Text, nullable=True)  # AES-256-GCM encrypted
    smb_domain = Column(String(255), nullable=True)
    smb_port = Column(Integer, default=445, nullable=False)

    # ----- processing -----
    user_id = Column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )  # owner of imported files
    polling_interval_minutes = Column(Integer, default=15, nullable=False)
    use_fs_events = Column(Boolean, default=False, nullable=False)  # local-only watchdog opt-in
    file_extensions = Column(Text, nullable=True)  # CSV, e.g. ".mp4,.mp3"; null → all media
    # No column default: null must mean "no age skip". New-source default (30) is
    # supplied by the Pydantic schema, so an explicit null from the UI is stored
    # as null (a column default=30 would silently override it — see issue #26).
    skip_files_older_than_days = Column(Integer, nullable=True)
    recursive = Column(Boolean, default=True, nullable=False)
    auto_transcribe = Column(Boolean, default=True, nullable=False)
    min_speakers = Column(Integer, default=1, nullable=True)
    max_speakers = Column(Integer, default=20, nullable=True)
    collection_ids = Column(JSON, nullable=True)  # list[str] of collection UUIDs
    tag_names = Column(JSON, nullable=True)  # list[str]

    # ----- multipart stitching -----
    multipart_enabled = Column(Boolean, default=False, nullable=False)
    multipart_regex = Column(String(500), nullable=False, default=DEFAULT_MULTIPART_REGEX)
    multipart_time_window_hours = Column(Integer, default=24, nullable=False)
    multipart_wait_scans = Column(Integer, default=3, nullable=False)
    upload_stitched_to_source = Column(Boolean, default=False, nullable=False)

    # ----- status (last scan) -----
    last_scan_at = Column(DateTime(timezone=True), nullable=True)
    last_scan_status = Column(String(20), nullable=True)  # success | error | running
    last_scan_message = Column(Text, nullable=True)
    last_scan_files_found = Column(Integer, default=0, nullable=False)
    last_scan_files_imported = Column(Integer, default=0, nullable=False)
    last_scan_files_skipped = Column(Integer, default=0, nullable=False)
    last_scan_duration_seconds = Column(Float, nullable=True)
    total_files_imported = Column(Integer, default=0, nullable=False)

    # ----- audit -----
    created_by = Column(Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # ----- relationships -----
    user = relationship("User", foreign_keys=[user_id])
    creator = relationship("User", foreign_keys=[created_by])
    files = relationship(
        "WatchSourceFile", back_populates="watch_source", cascade="all, delete-orphan"
    )
    email_links = relationship(
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
        from pathlib import Path

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

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    watch_source_id = Column(
        Integer, ForeignKey("watch_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remote_path = Column(String(2000), nullable=False)  # path within the source
    filename = Column(String(500), nullable=False)
    file_size = Column(BigInteger, nullable=True)
    file_modified_at = Column(DateTime(timezone=True), nullable=True)
    imohash = Column(String(64), nullable=True, index=True)
    media_file_id = Column(
        Integer, ForeignKey("media_file.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status = Column(String(30), nullable=False, default="pending")
    skip_reason = Column(String(50), nullable=True)
    part_group = Column(String(500), nullable=True)  # base name for a multi-part group
    part_number = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    watch_source = relationship("WatchSource", back_populates="files")
    media_file = relationship("MediaFile", foreign_keys=[media_file_id])

    __table_args__ = (
        UniqueConstraint("watch_source_id", "remote_path", name="_watch_source_file_path_unique"),
        Index("ix_watch_source_file_source_imohash", "watch_source_id", "imohash"),
        Index("ix_watch_source_file_part_group", "part_group", "watch_source_id"),
        Index("ix_watch_source_file_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<WatchSourceFile(id={self.id}, source={self.watch_source_id}, "
            f"path={self.remote_path!r}, status={self.status})>"
        )
