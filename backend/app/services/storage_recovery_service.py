"""In-place re-ingestion of orphaned MinIO media objects (Feature B).

After a DB-loss event the MinIO bucket still holds every original media object
(``media/<user_id>/<uuid>.<ext>``) plus YouTube thumbnail prefixes
(``user_<user_id>/youtube_<VIDEOID>/thumbnail.jpg``), but the PostgreSQL rows
that pointed at them are gone. This service re-registers each surviving object
as a ``MediaFile`` row **without copying, moving, or re-uploading a single
byte**: the new row's ``storage_path`` is set to the EXISTING object key, so
the whole pipeline (which reads media exclusively via ``storage_path``) finds
the object exactly where it already lives. Re-ingestion is therefore a pure
metadata operation.

Idempotency: discovery skips any object key already referenced by a
``MediaFile.storage_path``, so the CLI / task is safe to re-run — a second pass
registers only objects that weren't registered the first time.

No duplication: every registered row points at the pre-existing object; nothing
is written to object storage during registration.

The yt-dlp metadata enrichment is deliberately decoupled. The thumbnail prefix
exposes the YouTube id but NOT which media uuid it belongs to (that linkage died
with the DB), so titles cannot be restored by key alone. Instead we fetch
title/duration per YouTube id (rate-limited, metadata-only) into a MinIO JSON
sidecar, then match those records to processed rows by **duration** once ffprobe
has populated ``MediaFile.duration`` during transcription.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User

logger = logging.getLogger(__name__)

# Media object key prefix laid down by every ingest path: media/<user_id>/<uuid>.<ext>
MEDIA_PREFIX = "media"

# Object key of the resumable YouTube-metadata sidecar inside the media bucket.
YOUTUBE_METADATA_SIDECAR = "recovery/youtube_metadata.json"

# Thumbnail key shape: user_<user_id>/youtube_<VIDEOID>/thumbnail.jpg
THUMBNAIL_PREFIX_TMPL = "user_{user_id}/"

# Media container/audio extensions we re-ingest. Anything else under media/ (a
# stray thumbnail, a temp artifact) is ignored.
MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mp3",
        ".wav",
        ".m4a",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".flac",
        ".ogg",
        ".aac",
        ".wmv",
        ".flv",
        ".opus",
    }
)

# Extension → MIME fallback when mimetypes can't resolve it.
_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}

# Duration-match tolerance (seconds) and uniqueness rule are documented on
# match_metadata_by_duration().
DURATION_MATCH_TOLERANCE_SECONDS = 2.0


@dataclass
class ReingestSummary:
    """Counts returned by a re-ingestion run (also printed to stdout by the CLI)."""

    discovered: int = 0
    registered: int = 0
    skipped_existing: int = 0
    skipped_non_media: int = 0
    dispatched: int = 0
    errors: int = 0
    registered_uuids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "registered": self.registered,
            "skipped_existing": self.skipped_existing,
            "skipped_non_media": self.skipped_non_media,
            "dispatched": self.dispatched,
            "errors": self.errors,
        }


def _object_extension(object_name: str) -> str:
    """Return the lowercase extension (with dot) of an object key, or ''."""
    dot = object_name.rfind(".")
    slash = object_name.rfind("/")
    if dot <= slash:
        return ""
    return object_name[dot:].lower()


def content_type_for(object_name: str) -> str:
    """Resolve a MIME type from the object key's extension.

    Pure function — no I/O, no magic-byte read. The objects were written by
    OpenTranscribe's own ingest paths with a correct extension, so the
    extension is authoritative here.
    """
    ext = _object_extension(object_name)
    if ext in _CONTENT_TYPE_BY_EXT:
        return _CONTENT_TYPE_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(object_name)
    return guessed or "application/octet-stream"


def resolve_user(db: Session, user_email: str) -> User:
    """Resolve the owner user by email, raising if it doesn't exist.

    Re-ingestion assigns every recovered file to a single owner (the original
    per-file owner mapping died with the DB; recovery defaults to the admin).
    """
    user = db.query(User).filter(User.email == user_email).first()
    if user is None:
        raise ValueError(f"No user found with email {user_email!r}")
    return user


def iter_media_objects(minio_client: Any, user_id: int | None = None) -> Iterator[tuple[str, int]]:
    """Yield ``(object_name, size)`` for media objects under ``media/``.

    Args:
        minio_client: A minio ``Minio`` client (injected so unit tests mock at
            this boundary and never touch the live bucket).
        user_id: When given, scope discovery to ``media/<user_id>/``; otherwise
            list every user's media.

    Yields:
        ``(object_name, size_bytes)`` for each object whose extension is a known
        media/audio type. Non-media keys are silently skipped.
    """
    prefix = f"{MEDIA_PREFIX}/{user_id}/" if user_id is not None else f"{MEDIA_PREFIX}/"
    for obj in minio_client.list_objects(settings.MEDIA_BUCKET_NAME, prefix=prefix, recursive=True):
        name = obj.object_name
        if not name:
            continue
        if _object_extension(name) not in MEDIA_EXTENSIONS:
            continue
        yield name, int(obj.size or 0)


def existing_storage_paths(db: Session) -> set[str]:
    """Return the set of every ``storage_path`` already referenced by a row.

    Used for O(1) idempotency checks during discovery — one query instead of a
    per-object SELECT.
    """
    rows = db.query(MediaFile.storage_path).filter(MediaFile.storage_path != "").all()
    return {r[0] for r in rows if r[0]}


def register_object(
    db: Session,
    *,
    object_name: str,
    size: int,
    user: User,
) -> MediaFile:
    """Create a PENDING ``MediaFile`` row pointing at the EXISTING object key.

    The row's ``storage_path`` is the object key as-is — this is the entire
    no-duplication trick. ``filename``/``title`` are placeholders (the basename)
    until metadata enrichment fills them in. Flushes but does not commit; the
    caller owns the transaction boundary.
    """
    basename = object_name.rsplit("/", 1)[-1]
    media_file = MediaFile(
        user_id=user.id,
        filename=basename,
        title=basename,
        storage_path=object_name,  # <- existing key; no copy/move
        file_size=int(size),
        content_type=content_type_for(object_name),
        status=FileStatus.PENDING,
        is_public=False,
    )
    db.add(media_file)
    db.flush()
    return media_file


def fingerprint_object(media_file: MediaFile) -> None:
    """Compute and set ``imohash`` from the in-place MinIO object (best-effort).

    Uses the shared ``compute_from_minio`` (ranged reads, ~3 sample windows) so
    the recovered row's fingerprint matches every other ingest path's and the
    dedup layers keep working. Failures are non-fatal.
    """
    from app.services.imohash_service import compute_from_minio

    try:
        fingerprint = compute_from_minio(str(media_file.storage_path), size=media_file.file_size)
        if fingerprint:
            media_file.imohash = fingerprint  # type: ignore[assignment]
    except Exception as exc:  # pragma: no cover - defensive, network path
        logger.debug("imohash compute failed for %s (non-fatal): %s", media_file.storage_path, exc)


def reingest_objects(
    db: Session,
    *,
    minio_client: Any,
    user: User,
    dry_run: bool = False,
    limit: int | None = None,
    user_scope_id: int | None = None,
    dispatch: bool = True,
) -> ReingestSummary:
    """Discover, register, fingerprint, and dispatch in-place media objects.

    Args:
        db: Database session (committed per registered file).
        minio_client: minio client (injected for testability).
        user: Owner for every recovered row.
        dry_run: When True, no rows are created and nothing is dispatched — the
            run only counts what *would* be registered.
        limit: Stop after registering this many NEW files (None = all).
        user_scope_id: Restrict discovery to ``media/<id>/`` (None = all users).
        dispatch: When True, fire the standard upload pipeline per registered
            file. Ignored under ``dry_run``.

    Returns:
        A :class:`ReingestSummary` with discovered/registered/skipped/dispatched
        counts. Also logged per-file.
    """
    summary = ReingestSummary()
    seen_paths = existing_storage_paths(db)

    for object_name, size in iter_media_objects(minio_client, user_id=user_scope_id):
        summary.discovered += 1

        if object_name in seen_paths:
            summary.skipped_existing += 1
            logger.info("skip (already registered): %s", object_name)
            continue

        if limit is not None and summary.registered >= limit:
            logger.info("reached --limit %d; stopping discovery", limit)
            break

        if dry_run:
            summary.registered += 1
            seen_paths.add(object_name)
            logger.info("[dry-run] would register: %s (%d bytes)", object_name, size)
            continue

        try:
            media_file = register_object(db, object_name=object_name, size=size, user=user)
            fingerprint_object(media_file)
            db.commit()
            db.refresh(media_file)
            seen_paths.add(object_name)
            summary.registered += 1
            summary.registered_uuids.append(str(media_file.uuid))
            logger.info(
                "registered media_file id=%s uuid=%s path=%s",
                media_file.id,
                media_file.uuid,
                object_name,
            )
        except Exception as exc:
            db.rollback()
            summary.errors += 1
            logger.error("failed to register %s: %s", object_name, exc)
            continue

        if dispatch:
            try:
                from app.api.endpoints.files.upload import dispatch_upload_pipeline

                dispatch_upload_pipeline(
                    media_file,
                    user_id=user.id,
                    whisper_model=None,
                    min_speakers=None,
                    max_speakers=None,
                    num_speakers=None,
                    task_id=None,
                )
                summary.dispatched += 1
                logger.info("dispatched pipeline for media_file id=%s", media_file.id)
            except Exception as exc:
                summary.errors += 1
                logger.error("dispatch failed for media_file id=%s: %s", media_file.id, exc)

    return summary


# ---------------------------------------------------------------------------
# YouTube metadata enrichment (rate-limited, metadata-only — never re-downloads)
# ---------------------------------------------------------------------------


def discover_youtube_ids(minio_client: Any, user_id: int) -> list[str]:
    """List the YouTube ids surfaced by surviving thumbnail prefixes.

    Thumbnail keys look like ``user_<user_id>/youtube_<VIDEOID>/thumbnail.jpg``.
    We return the unique ``<VIDEOID>`` values — the id is real, but it carries
    no link to a media uuid (that link died with the DB), so the ids are only
    useful for the metadata-then-duration-match flow.
    """
    prefix = THUMBNAIL_PREFIX_TMPL.format(user_id=user_id)
    ids: set[str] = set()
    for obj in minio_client.list_objects(settings.MEDIA_BUCKET_NAME, prefix=prefix, recursive=True):
        name = obj.object_name or ""
        # .../youtube_<ID>/thumbnail...
        marker = "/youtube_"
        idx = name.find(marker)
        if idx == -1:
            continue
        rest = name[idx + len(marker) :]
        vid = rest.split("/", 1)[0]
        if vid:
            ids.add(vid)
    return sorted(ids)


def load_metadata_sidecar(minio_service: Any) -> dict[str, dict[str, Any]]:
    """Load the resumable YouTube-metadata sidecar from MinIO (``{}`` if absent)."""
    try:
        response = minio_service.get_object(settings.MEDIA_BUCKET_NAME, YOUTUBE_METADATA_SIDECAR)
        try:
            raw = response.read()
        finally:
            with contextlib.suppress(Exception):
                response.close()
                response.release_conn()
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("metadata sidecar not present / unreadable (%s); starting fresh", exc)
        return {}


def save_metadata_sidecar(minio_service: Any, data: dict[str, dict[str, Any]]) -> None:
    """Persist the YouTube-metadata sidecar back to MinIO (overwrite)."""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    minio_service.upload_bytes(
        bucket_name=settings.MEDIA_BUCKET_NAME,
        object_name=YOUTUBE_METADATA_SIDECAR,
        data=payload,
        content_type="application/json",
    )


def fetch_youtube_metadata(video_id: str) -> dict[str, Any] | None:
    """Fetch title/duration for one YouTube id, metadata-only (no media download).

    Uses yt-dlp with ``skip_download`` / ``extract_flat`` so only the info JSON
    is fetched — never the video bytes (re-downloading is forbidden). Returns
    ``None`` on any error so the caller can record a miss and continue.
    """
    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        return {
            "youtube_id": video_id,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "source_url": url,
        }
    except Exception as exc:
        logger.warning("yt-dlp metadata fetch failed for %s: %s", video_id, exc)
        return None


class _DurationRecord(Protocol):
    """Structural type for duration matching — anything with an id and duration.

    The matcher only reads these two attributes; typing it structurally (rather
    than as the concrete MediaFile) keeps the pure function honestly testable
    with lightweight fakes.
    """

    id: int
    duration: float | None


def match_metadata_by_duration(
    rows: Sequence[_DurationRecord],
    metadata: dict[str, dict[str, Any]],
    *,
    tolerance: float = DURATION_MATCH_TOLERANCE_SECONDS,
) -> dict[int, dict[str, Any]]:
    """Match recovered rows to YouTube metadata by duration (unique-match only).

    A row is matched to a metadata record only when EXACTLY ONE record falls
    within ``±tolerance`` seconds of the row's ffprobe-derived ``duration`` AND
    exactly one row is within tolerance of that record. Ambiguous matches (two
    candidates either way) are left unmatched — better an empty title than a
    wrong one.

    Pure function: takes rows + metadata, returns ``{media_file_id: record}``.

    Args:
        rows: MediaFile rows that already have a ``duration`` (post-ffprobe) and
            no title (or a placeholder title).
        metadata: ``{youtube_id: {"title", "duration", ...}}`` from the sidecar.
        tolerance: ± seconds window for a duration match.

    Returns:
        Mapping of ``media_file.id`` → the matched metadata record.
    """
    # Candidate metadata records with a usable numeric duration.
    records = [
        rec
        for rec in metadata.values()
        if isinstance(rec.get("duration"), (int, float)) and rec.get("duration")
    ]

    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.duration is None:
            continue
        row_dur = float(row.duration)
        near = [rec for rec in records if abs(float(rec["duration"]) - row_dur) <= tolerance]
        if len(near) != 1:
            continue
        rec = near[0]
        # Reverse-uniqueness: ensure no OTHER row is also within tolerance of rec.
        rec_dur = float(rec["duration"])
        contenders = [
            r
            for r in rows
            if r.duration is not None and abs(float(r.duration) - rec_dur) <= tolerance
        ]
        if len(contenders) != 1:
            continue
        result[row.id] = rec
    return result


def apply_metadata_matches(db: Session, matches: dict[int, dict[str, Any]]) -> int:
    """Fill title/source_url on rows from duration matches. Returns rows updated."""
    updated = 0
    for media_file_id, rec in matches.items():
        row = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if row is None:
            continue
        title = rec.get("title")
        if title:
            row.title = str(title)[:255]  # type: ignore[assignment]
            row.filename = str(title)[:255]  # type: ignore[assignment]
        if rec.get("source_url"):
            row.source_url = str(rec["source_url"])  # type: ignore[assignment]
        if rec.get("uploader"):
            row.author = str(rec["uploader"])  # type: ignore[assignment]
        updated += 1
    if updated:
        db.commit()
    return updated
