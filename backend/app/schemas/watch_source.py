"""Pydantic schemas for Watch Sources (auto-import from local / S3 / SMB)."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.models.watch_source import DEFAULT_MULTIPART_REGEX


class SourceType(str, Enum):
    """Type discriminator for a watch source."""

    LOCAL = "local"
    S3 = "s3"
    SMB = "smb"


class WatchFileStatus(str, Enum):
    """Lifecycle status of a tracked watch-source file."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    IMPORTED = "imported"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_OLD = "skipped_old"
    SKIPPED_INVALID = "skipped_invalid"
    PROCESSING = "processing"
    ERROR = "error"
    STITCHED_PART = "stitched_part"
    WAITING_FOR_PARTS = "waiting_for_parts"


class SkipReason(str, Enum):
    """Why a tracked file was skipped."""

    DUPLICATE_SAME_SOURCE = "duplicate_same_source"
    DUPLICATE_OTHER_SOURCE = "duplicate_other_source"
    DUPLICATE_EXISTING = "duplicate_existing"
    TOO_OLD = "too_old"
    INVALID_TYPE = "invalid_type"
    VALIDATION_FAILED = "validation_failed"


class ScanStatus(str, Enum):
    """Outcome of the last scan of a source."""

    SUCCESS = "success"
    ERROR = "error"
    RUNNING = "running"


# ----- shared processing config -----


class WatchSourceProcessingBase(BaseModel):
    """Processing/behaviour fields shared by create and update."""

    polling_interval_minutes: int = Field(default=15, ge=1, le=1440)
    use_fs_events: bool = False
    file_extensions: Optional[str] = None  # CSV, e.g. ".mp4,.mp3"
    skip_files_older_than_days: Optional[int] = Field(default=30, ge=0)
    recursive: bool = True
    auto_transcribe: bool = True
    min_speakers: Optional[int] = Field(default=1, ge=1)
    max_speakers: Optional[int] = Field(default=20, ge=1)
    collection_ids: Optional[list[str]] = None
    tag_names: Optional[list[str]] = None
    # multipart
    multipart_enabled: bool = False
    multipart_regex: str = DEFAULT_MULTIPART_REGEX
    multipart_time_window_hours: int = Field(default=24, ge=1)
    multipart_wait_scans: int = Field(default=3, ge=1)
    upload_stitched_to_source: bool = False


class WatchSourceCreate(WatchSourceProcessingBase):
    """Create a watch source. Per-type fields validated by source_type."""

    name: str = Field(..., min_length=1, max_length=200)
    source_type: SourceType
    is_enabled: bool = True

    # local
    local_path: Optional[str] = None
    delete_after_import: bool = False

    # s3
    s3_endpoint_url: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_region: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_key: Optional[str] = None  # plaintext on write; never returned
    s3_use_ssl: bool = True

    # smb
    smb_server: Optional[str] = None
    smb_share: Optional[str] = None
    smb_path: Optional[str] = "/"
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None  # plaintext on write; never returned
    smb_domain: Optional[str] = None
    smb_port: int = Field(default=445, ge=1, le=65535)

    # admin-only: assign imported files to a specific user (by uuid)
    assign_to_user_uuid: Optional[str] = None

    @field_validator("local_path")
    @classmethod
    def _no_traversal(cls, v: Optional[str]) -> Optional[str]:
        if v and ".." in v.split("/"):
            raise ValueError("local_path must not contain '..' traversal segments")
        if v and v.startswith("/"):
            raise ValueError("local_path must be relative to the watch root")
        return v

    @model_validator(mode="after")
    def _validate_per_type(self) -> "WatchSourceCreate":
        if self.source_type == SourceType.S3:
            missing = [
                f
                for f in ("s3_bucket_name", "s3_access_key_id", "s3_secret_key")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(f"S3 source requires: {', '.join(missing)}")
        elif self.source_type == SourceType.SMB:
            missing = [f for f in ("smb_server", "smb_share") if not getattr(self, f)]
            if missing:
                raise ValueError(f"SMB source requires: {', '.join(missing)}")
        # local needs no required field beyond local_path (root is allowed)
        if self.min_speakers and self.max_speakers and self.min_speakers > self.max_speakers:
            raise ValueError("min_speakers cannot exceed max_speakers")
        return self


class WatchSourceUpdate(BaseModel):
    """Update a watch source. All fields optional; source_type is immutable."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    is_enabled: Optional[bool] = None
    local_path: Optional[str] = None
    delete_after_import: Optional[bool] = None
    s3_endpoint_url: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_region: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_use_ssl: Optional[bool] = None
    smb_server: Optional[str] = None
    smb_share: Optional[str] = None
    smb_path: Optional[str] = None
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None
    smb_domain: Optional[str] = None
    smb_port: Optional[int] = Field(default=None, ge=1, le=65535)
    polling_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    use_fs_events: Optional[bool] = None
    file_extensions: Optional[str] = None
    skip_files_older_than_days: Optional[int] = Field(default=None, ge=0)
    recursive: Optional[bool] = None
    auto_transcribe: Optional[bool] = None
    min_speakers: Optional[int] = Field(default=None, ge=1)
    max_speakers: Optional[int] = Field(default=None, ge=1)
    collection_ids: Optional[list[str]] = None
    tag_names: Optional[list[str]] = None
    multipart_enabled: Optional[bool] = None
    multipart_regex: Optional[str] = None
    multipart_time_window_hours: Optional[int] = Field(default=None, ge=1)
    multipart_wait_scans: Optional[int] = Field(default=None, ge=1)
    upload_stitched_to_source: Optional[bool] = None

    @field_validator("local_path")
    @classmethod
    def _no_traversal(cls, v: Optional[str]) -> Optional[str]:
        if v and ".." in v.split("/"):
            raise ValueError("local_path must not contain '..' traversal segments")
        if v and v.startswith("/"):
            raise ValueError("local_path must be relative to the watch root")
        return v


class WatchSourceResponse(BaseModel):
    """Watch source as returned by the API — never includes secrets."""

    uuid: str
    name: str
    source_type: str
    is_enabled: bool
    local_path: Optional[str] = None
    delete_after_import: bool = False
    s3_endpoint_url: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_region: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_use_ssl: bool = True
    has_s3_secret_key: bool = False
    smb_server: Optional[str] = None
    smb_share: Optional[str] = None
    smb_path: Optional[str] = None
    smb_username: Optional[str] = None
    smb_domain: Optional[str] = None
    smb_port: int = 445
    has_smb_password: bool = False
    polling_interval_minutes: int = 15
    use_fs_events: bool = False
    file_extensions: Optional[str] = None
    skip_files_older_than_days: Optional[int] = None
    recursive: bool = True
    auto_transcribe: bool = True
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    collection_ids: Optional[list[str]] = None
    tag_names: Optional[list[str]] = None
    multipart_enabled: bool = False
    multipart_regex: str = DEFAULT_MULTIPART_REGEX
    multipart_time_window_hours: int = 24
    multipart_wait_scans: int = 3
    upload_stitched_to_source: bool = False
    last_scan_at: Optional[datetime] = None
    last_scan_status: Optional[str] = None
    last_scan_message: Optional[str] = None
    last_scan_files_found: int = 0
    last_scan_files_imported: int = 0
    last_scan_files_skipped: int = 0
    last_scan_duration_seconds: Optional[float] = None
    total_files_imported: int = 0
    # ownership context
    owner_name: Optional[str] = None
    owner_uuid: Optional[str] = None
    is_own: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WatchSourceFileResponse(BaseModel):
    """A tracked file within a watch source."""

    uuid: str
    remote_path: str
    filename: str
    file_size: Optional[int] = None
    file_modified_at: Optional[datetime] = None
    imohash: Optional[str] = None
    media_file_uuid: Optional[str] = None
    status: str
    skip_reason: Optional[str] = None
    part_group: Optional[str] = None
    part_number: Optional[int] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    processed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WatchSourceFilesList(BaseModel):
    """Server-paginated list of tracked files for one source."""

    files: list[WatchSourceFileResponse] = []
    total: int = 0
    page: int = 1
    page_size: int = 50


class WatchSourceStats(BaseModel):
    """Aggregated per-status counts for a source's tracked files."""

    total: int = 0
    imported: int = 0
    skipped: int = 0
    error: int = 0
    pending: int = 0
    waiting_for_parts: int = 0


class WatchSourcesList(BaseModel):
    """List of watch sources (own + others when admin)."""

    sources: list[WatchSourceResponse] = []


class ConnectionTestResponse(BaseModel):
    """Result of testing a source connection."""

    success: bool
    message: str
    latency_ms: Optional[float] = None


class ScanResponse(BaseModel):
    """Result of triggering an immediate scan."""

    status: str
    message: str
    task_id: Optional[str] = None


class DirectoryEntry(BaseModel):
    """A single subdirectory in the folder browser."""

    name: str
    path: str  # relative to WATCH_FOLDER_PATH


class DirectoryListResponse(BaseModel):
    """Folder-browser listing under WATCH_FOLDER_PATH."""

    current_path: str = ""
    parent_path: Optional[str] = None
    directories: list[DirectoryEntry] = []


class CapabilitiesResponse(BaseModel):
    """Feature gating flags for the UI."""

    watch_source_enabled: bool = True
    local_enabled: bool = False  # WATCH_FOLDER_PATH configured
    fs_events_enabled: bool = False


class MultipartRegexTestRequest(BaseModel):
    """Request to test a multipart regex against a filename."""

    regex: str = Field(..., min_length=1, max_length=500)
    filename: str = Field(..., min_length=1, max_length=500)


class MultipartRegexTestResponse(BaseModel):
    """Parsed multipart components, or matched=False."""

    matched: bool
    base_name: Optional[str] = None
    part_number: Optional[int] = None
    extension: Optional[str] = None
    error: Optional[str] = None


class EmailLinkCreate(BaseModel):
    """Link an email config to a watch source."""

    email_config_uuid: str
    additional_recipients: Optional[str] = None
    notify_on_success: bool = True
    notify_on_error: bool = True


class EmailLinkResponse(BaseModel):
    """An email config linked to a watch source."""

    email_config_uuid: str
    email_config_name: str
    additional_recipients: Optional[str] = None
    notify_on_success: bool = True
    notify_on_error: bool = True

    model_config = {"from_attributes": True}
