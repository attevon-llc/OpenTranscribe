"""Pydantic schemas for Watch Sources (auto-import from local / S3 / SMB)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.models.watch_source import DEFAULT_MULTIPART_REGEX


class SourceType(StrEnum):
    """Type discriminator for a watch source."""

    LOCAL = "local"
    S3 = "s3"
    SMB = "smb"


class WatchFileStatus(StrEnum):
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


class SkipReason(StrEnum):
    """Why a tracked file was skipped."""

    DUPLICATE_SAME_SOURCE = "duplicate_same_source"
    DUPLICATE_OTHER_SOURCE = "duplicate_other_source"
    DUPLICATE_EXISTING = "duplicate_existing"
    TOO_OLD = "too_old"
    INVALID_TYPE = "invalid_type"
    VALIDATION_FAILED = "validation_failed"


# ----- shared processing config -----


class WatchSourceProcessingBase(BaseModel):
    """Processing/behaviour fields shared by create and update."""

    polling_interval_minutes: int = Field(default=15, ge=1, le=1440)
    use_fs_events: bool = False
    file_extensions: str | None = None  # CSV, e.g. ".mp4,.mp3"
    skip_files_older_than_days: int | None = Field(default=30, ge=0)
    recursive: bool = True
    auto_transcribe: bool = True
    min_speakers: int | None = Field(default=1, ge=1)
    max_speakers: int | None = Field(default=20, ge=1)
    collection_ids: list[str] | None = None
    tag_names: list[str] | None = None
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
    local_path: str | None = None
    delete_after_import: bool = False

    # s3
    s3_endpoint_url: str | None = None
    s3_bucket_name: str | None = None
    s3_prefix: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_key: str | None = None  # plaintext on write; never returned
    s3_use_ssl: bool = True

    # smb
    smb_server: str | None = None
    smb_share: str | None = None
    smb_path: str | None = "/"
    smb_username: str | None = None
    smb_password: str | None = None  # plaintext on write; never returned
    smb_domain: str | None = None
    smb_port: int = Field(default=445, ge=1, le=65535)

    # admin-only: assign imported files to a specific user (by uuid)
    assign_to_user_uuid: str | None = None

    @field_validator("local_path")
    @classmethod
    def _no_traversal(cls, v: str | None) -> str | None:
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

    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_enabled: bool | None = None
    local_path: str | None = None
    delete_after_import: bool | None = None
    s3_endpoint_url: str | None = None
    s3_bucket_name: str | None = None
    s3_prefix: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_key: str | None = None
    s3_use_ssl: bool | None = None
    smb_server: str | None = None
    smb_share: str | None = None
    smb_path: str | None = None
    smb_username: str | None = None
    smb_password: str | None = None
    smb_domain: str | None = None
    smb_port: int | None = Field(default=None, ge=1, le=65535)
    polling_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    use_fs_events: bool | None = None
    file_extensions: str | None = None
    skip_files_older_than_days: int | None = Field(default=None, ge=0)
    recursive: bool | None = None
    auto_transcribe: bool | None = None
    min_speakers: int | None = Field(default=None, ge=1)
    max_speakers: int | None = Field(default=None, ge=1)
    collection_ids: list[str] | None = None
    tag_names: list[str] | None = None
    multipart_enabled: bool | None = None
    multipart_regex: str | None = None
    multipart_time_window_hours: int | None = Field(default=None, ge=1)
    multipart_wait_scans: int | None = Field(default=None, ge=1)
    upload_stitched_to_source: bool | None = None

    @field_validator("local_path")
    @classmethod
    def _no_traversal(cls, v: str | None) -> str | None:
        if v and ".." in v.split("/"):
            raise ValueError("local_path must not contain '..' traversal segments")
        if v and v.startswith("/"):
            raise ValueError("local_path must be relative to the watch root")
        return v


class FsEventsMode(StrEnum):
    """Observer a source actually ended up with (issue #294)."""

    NATIVE = "native"  # platform observer (inotify), delivery verified
    POLLING = "polling"  # watchdog PollingObserver — network mount / macOS / Windows
    ERROR = "error"  # observer could not be started; Celery polling only
    UNAVAILABLE = "unavailable"  # watchdog not installed in this image


class FsEventsStatus(BaseModel):
    """Live FS-watching status, published by the beat supervisor via Redis.

    ``None`` on a source means nothing is watching it and the Celery poll is the
    only mechanism — which is also what the UI shows if the beat container is
    down, because the status key carries a short TTL.
    """

    mode: FsEventsMode
    active: bool = False
    detail: str | None = None
    fs_type: str | None = None
    debounce_seconds: float | None = None
    poll_seconds: int | None = None
    since: datetime | None = None
    last_event_at: datetime | None = None
    events_seen: int = 0
    scans_dispatched: int = 0
    updated_at: datetime | None = None


class WatchSourceResponse(BaseModel):
    """Watch source as returned by the API — never includes secrets."""

    uuid: str
    name: str
    source_type: str
    is_enabled: bool
    local_path: str | None = None
    delete_after_import: bool = False
    s3_endpoint_url: str | None = None
    s3_bucket_name: str | None = None
    s3_prefix: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_use_ssl: bool = True
    has_s3_secret_key: bool = False
    smb_server: str | None = None
    smb_share: str | None = None
    smb_path: str | None = None
    smb_username: str | None = None
    smb_domain: str | None = None
    smb_port: int = 445
    has_smb_password: bool = False
    polling_interval_minutes: int = 15
    use_fs_events: bool = False
    # Live observer status; None = polling only (see FsEventsStatus).
    fs_events: FsEventsStatus | None = None
    file_extensions: str | None = None
    skip_files_older_than_days: int | None = None
    recursive: bool = True
    auto_transcribe: bool = True
    min_speakers: int | None = None
    max_speakers: int | None = None
    collection_ids: list[str] | None = None
    tag_names: list[str] | None = None
    multipart_enabled: bool = False
    multipart_regex: str = DEFAULT_MULTIPART_REGEX
    multipart_time_window_hours: int = 24
    multipart_wait_scans: int = 3
    upload_stitched_to_source: bool = False
    last_scan_at: datetime | None = None
    last_scan_status: str | None = None
    last_scan_message: str | None = None
    last_scan_files_found: int = 0
    last_scan_files_imported: int = 0
    last_scan_files_skipped: int = 0
    last_scan_duration_seconds: float | None = None
    total_files_imported: int = 0
    # ownership context
    owner_name: str | None = None
    owner_uuid: str | None = None
    is_own: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class WatchSourceFileResponse(BaseModel):
    """A tracked file within a watch source."""

    uuid: str
    remote_path: str
    filename: str
    file_size: int | None = None
    file_modified_at: datetime | None = None
    imohash: str | None = None
    media_file_uuid: str | None = None
    status: str
    skip_reason: str | None = None
    part_group: str | None = None
    part_number: int | None = None
    error_message: str | None = None
    retry_count: int = 0
    processed_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class WatchSourceFilesList(BaseModel):
    """Server-paginated list of tracked files for one source."""

    files: list[WatchSourceFileResponse] = []
    total: int = 0
    page: int = 1
    page_size: int = 50


#: Statuses a tracked file may be re-queued from.
#:
#: ``error`` and every ``skipped_*`` variant. The exclusions are each a different
#: kind of wrong rather than an oversight:
#:
#: * ``imported`` already succeeded — re-importing would duplicate it, so the caller
#:   wants the delete-record action instead;
#: * ``importing``/``downloading`` are in flight and resetting them races
#:   ``_claim_import``;
#: * ``waiting_for_parts`` carries the multipart WAIT counter in ``retry_count``, not
#:   an attempt count, so resetting it corrupts the group's stitch decision;
#: * ``stitched_part`` was consumed into a stitched recording — re-importing it would
#:   add the raw part alongside the whole.
#:
#: Only the ``skipped_*`` half is new capability: ``error`` rows are not terminal and
#: are already retried by the next scan. The skipped ones are terminal, so before this
#: there was no way to retry them at all.
#:
#: Derived from the enum rather than listed, so a ``skipped_*`` member added later is
#: retryable without a second place to remember. (``skipped_too_large`` is written by
#: the document ingest path but is not yet an enum member — see #547; it becomes
#: retryable automatically when that lands.)
RETRYABLE_FILE_STATUSES: frozenset[str] = frozenset(
    {WatchFileStatus.ERROR.value}
    | {s.value for s in WatchFileStatus if s.value.startswith("skipped_")}
)


class WatchSourceFileActionRequest(BaseModel):
    """The tracked files a batch retry or batch delete applies to.

    Batch-shaped even for a single row, because ``scan_single`` holds a Redis lock
    per source: a per-file retry endpoint would dispatch one scan per file and all
    but the first would silently no-op. One reset pass plus one dispatch is both
    correct and what "retry all failed" needs.
    """

    file_uuids: list[str] = Field(..., min_length=1, max_length=500)


class WatchSourceFileActionResult(BaseModel):
    """What happened to one row in a batch. A refusal is not an exception."""

    file_uuid: str
    success: bool
    status: str | None = None
    #: Set when the action was applied but is unlikely to achieve anything — e.g. a
    #: file skipped for age on a source that still has the age cutoff set, which the
    #: next scan will skip again for the same reason.
    warning: str | None = None
    error: str | None = None


class WatchSourceFileActionResponse(BaseModel):
    """Per-row outcomes, so a partial batch reports honestly instead of failing whole."""

    results: list[WatchSourceFileActionResult] = []
    #: False when no row was eligible — the caller should not be told a scan is coming
    #: when nothing was queued.
    scan_dispatched: bool = False


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
    latency_ms: float | None = None


class ScanResponse(BaseModel):
    """Result of triggering an immediate scan."""

    status: str
    message: str
    task_id: str | None = None


class DirectoryEntry(BaseModel):
    """A single subdirectory in the folder browser."""

    name: str
    path: str  # relative to WATCH_FOLDER_PATH


class DirectoryListResponse(BaseModel):
    """Folder-browser listing under WATCH_FOLDER_PATH."""

    current_path: str = ""
    parent_path: str | None = None
    directories: list[DirectoryEntry] = []


class CapabilitiesResponse(BaseModel):
    """Feature gating flags for the UI."""

    watch_source_enabled: bool = True
    local_enabled: bool = False  # WATCH_FOLDER_PATH configured
    fs_events_enabled: bool = False
    fs_events_mode: str = "auto"  # auto | native | polling | off


class MultipartRegexTestRequest(BaseModel):
    """Request to test a multipart regex against a filename."""

    regex: str = Field(..., min_length=1, max_length=500)
    filename: str = Field(..., min_length=1, max_length=500)


class MultipartRegexTestResponse(BaseModel):
    """Parsed multipart components, or matched=False."""

    matched: bool
    base_name: str | None = None
    part_number: int | None = None
    extension: str | None = None
    error: str | None = None


class EmailLinkCreate(BaseModel):
    """Link an email config to a watch source."""

    email_config_uuid: str
    additional_recipients: str | None = None
    notify_on_success: bool = True
    notify_on_error: bool = True

    @field_validator("additional_recipients")
    @classmethod
    def _validate_additional_recipients(cls, value: str | None) -> str | None:
        """Reject a CSV entry that cannot be an address.

        ``_merge_recipients`` (``tasks/watch_source_tasks.py``) splits this on commas
        and hands the parts straight to the mailer, so an unusable entry is dropped
        inside a send whose per-address result nothing inspects — the link goes on
        reporting itself as configured while that person never receives anything.
        Checking at the wire edge is the only place the caller still has a way to be
        told.

        Deliberately a shape check, not an RFC 5322 parser: the goal is to catch the
        typo the admin can fix, not to adjudicate exotic-but-legal addresses. The
        value is stored **verbatim** (whitespace and all) because the send path is
        what splits it, and normalising here would put this function in the business
        of rewriting stored data.
        """
        if value is None or not value.strip():
            return value
        for part in value.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            local, _, domain = candidate.partition("@")
            if not local or not domain or "." not in domain:
                raise ValueError(
                    f"{candidate!r} is not a usable email address; "
                    "give a comma-separated list like 'ops@example.com,oncall@example.com'"
                )
        return value


class EmailConfigOption(BaseModel):
    """An email config a source owner may link, as a MINIMAL projection.

    Deliberately not ``EmailConfigResponse``. Managing the configs themselves is
    super_admin work because they hold mailbox credentials, but any source owner may
    subscribe their own source to one that already exists — so the picker has to be
    readable a tier below the config list, and it must therefore carry nothing an
    ordinary user has no business seeing. No ``from_address``, no ``smtp_host``, no
    ``*_username``, no ``has_*`` secret flags.

    Adding a field here widens what every authenticated user can read about the
    deployment's mail setup; ``test_watch_source_email_routes`` pins the key set
    exactly so that cannot happen by accident.
    """

    uuid: str
    name: str
    provider: str
    #: A disabled config is skipped at send time, so a link to one delivers nothing.
    #: Surfaced so the UI can say that at the moment of linking.
    is_enabled: bool = True
    #: Whether the config carries default recipients — NOT the addresses themselves.
    #: A link whose config has none and which adds none of its own resolves to an
    #: empty recipient list and is silently skipped; the boolean is what lets the UI
    #: warn about that without leaking who the deployment mails.
    has_default_recipients: bool = False


class EmailLinkResponse(BaseModel):
    """An email config linked to a watch source.

    Carries enough of the *config* to explain why a link might deliver nothing. That
    is not redundancy with ``EmailConfigOption``: the picker deliberately excludes
    configs already linked, so a caller that only had the picker could never learn
    these facts about the links it is displaying — and the two states they describe
    (disabled config, no recipients anywhere) are exactly the ones invisible from the
    link's own fields. Same minimal projection rules: a flag, never an address.
    """

    email_config_uuid: str
    email_config_name: str
    email_config_provider: str = "smtp"
    #: A disabled config is skipped by ``send_notification``; the link still looks set.
    config_is_enabled: bool = True
    #: Whether the CONFIG has default recipients — not who they are. With no default
    #: and no ``additional_recipients``, the merged list is empty and nothing is sent.
    config_has_default_recipients: bool = False
    additional_recipients: str | None = None
    notify_on_success: bool = True
    notify_on_error: bool = True

    model_config = {"from_attributes": True}
