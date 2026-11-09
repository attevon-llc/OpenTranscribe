"""
Video processing service for embedding subtitles into video files.

**Session-lifetime note** (``app/tasks/CLAUDE.md``): the ffmpeg/MinIO entry
points here — ``process_video_with_subtitles``, ``embed_subtitles_in_video`` and
``extract_audio`` — used to take a caller's ``Session``, query with it, and then
run a MinIO download plus a full ffmpeg transcode with it still open. A
transcode is minutes; the transaction held ACCESS SHARE the whole time (queueing
any ``ALTER TABLE``, i.e. an Alembic upgrade), pinned the vacuum horizon and
consumed a pool connection. They now take plain arguments and open their **own**
short sessions for the two reads they need (the filename, and the transcript for
the SRT), so no transaction spans the slow work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.redis import get_async_redis
from app.db.session_utils import session_scope
from app.services.audio_extraction import AudioExtractionMixin
from app.services.media_revocation_cache import legacy_audio_keys
from app.services.minio_bucket_admin import MinIOService
from app.services.storage_absence import is_absent_error
from app.services.video_subtitle_burn import VideoSubtitleBurnMixin

if TYPE_CHECKING:
    from app.services.storage_versions import RemovalResult

logger = logging.getLogger(__name__)


class VideoProcessingService(VideoSubtitleBurnMixin, AudioExtractionMixin):
    """Service for processing video files, including subtitle embedding."""

    # Regenerable derived assets (subtitle-embedded videos + extracted audio) live under
    # this prefix so one MinIO lifecycle rule can auto-expire them. Originals are the
    # source of truth; these are duplicates re-created on demand.
    DERIVED_CACHE_PREFIX = "derived/"

    def __init__(self, minio_service: MinIOService):
        self.minio_service = minio_service
        self.cache_bucket = settings.CACHE_BUCKET_NAME
        self._ensure_cache_bucket_exists()

    async def _send_download_progress(
        self,
        user_id: int,
        file_id: int,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ) -> None:
        """Send download progress update via WebSocket."""
        try:
            # This publishes straight to the ``websocket_notifications`` Redis
            # channel rather than going through ``send_ws_event``/
            # ``send_ws_event_for_file`` (issue #908, finding A) — but the payload
            # still carries ``file_id``, so a quarantined file's identity must not
            # reach a non-admin over this path either, even though no filename is
            # included: the fact that a download for that id exists and is
            # progressing is itself the disclosure a takedown is supposed to hide.
            from app.services.file_availability import is_notification_suppressed

            if is_notification_suppressed(file_id, user_id):
                logger.debug(f"download_progress notification suppressed for file {file_id}")
                return

            redis_client = get_async_redis()
            notification_data = {
                "user_id": user_id,
                "type": "download_progress",
                "data": {
                    "file_id": str(file_id),
                    "status": status,
                    "progress": progress,
                    "error": error,
                },
            }
            await redis_client.publish("websocket_notifications", json.dumps(notification_data))
            logger.info(
                f"Sent download progress update: user={user_id}, file={file_id}, status={status}"
            )
        except Exception as e:
            logger.exception(f"Failed to send download progress update: {e}")

    def _send_download_progress_sync(
        self,
        user_id: int,
        file_id: int,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ) -> None:
        """Synchronous wrapper for sending download progress updates."""
        try:
            # Create new event loop for this thread if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Run the async function
            loop.run_until_complete(
                self._send_download_progress(user_id, file_id, status, progress, error)
            )
        except Exception as e:
            logger.exception(f"Failed to send download progress update (sync): {e}")

    def _ensure_cache_bucket_exists(self) -> None:
        """Ensure the cache bucket exists."""
        try:
            if not self.minio_service.bucket_exists(self.cache_bucket):
                logger.info(f"Creating cache bucket: {self.cache_bucket}")
                self.minio_service.make_bucket(self.cache_bucket)
                logger.info(f"Cache bucket created successfully: {self.cache_bucket}")
            else:
                logger.info(f"Cache bucket already exists: {self.cache_bucket}")
        except Exception as e:
            logger.error(f"Failed to create cache bucket: {e}")
            raise

        # Auto-expire ephemeral bulk-export ZIPs (presigned URLs are short-lived, so a
        # fresh job_id is generated per request — the objects don't need to persist).
        # Idempotent + best-effort; does not affect long-lived video/audio cache objects.
        self.minio_service.ensure_prefix_expiry(
            self.cache_bucket, prefix="bulk/", days=1, rule_id="expire-bulk-exports"
        )
        # Bootstrap the derived-cache lifecycle rule from the env baseline ONLY if absent.
        # The authoritative value is the admin/DB setting, re-applied on startup and on
        # change via apply_derived_retention(); per-instance construction must never
        # clobber it (this object is created per task/request).
        if settings.DERIVED_CACHE_RETENTION_DAYS and settings.DERIVED_CACHE_RETENTION_DAYS > 0:
            self.minio_service.ensure_prefix_expiry(
                self.cache_bucket,
                prefix=self.DERIVED_CACHE_PREFIX,
                days=int(settings.DERIVED_CACHE_RETENTION_DAYS),
                rule_id="expire-derived-cache",
                update_if_exists=False,
            )

    def apply_derived_retention(self, days: int) -> None:
        """Authoritatively (re)apply the lifecycle rule that expires the derived cache.

        ``days <= 0`` removes the rule (keep derived assets forever). Best-effort.
        """
        if days and days > 0:
            self.minio_service.ensure_prefix_expiry(
                self.cache_bucket,
                prefix=self.DERIVED_CACHE_PREFIX,
                days=int(days),
                rule_id="expire-derived-cache",
                update_if_exists=True,
            )
        else:
            self.minio_service.remove_lifecycle_rule(self.cache_bucket, "expire-derived-cache")

    def generate_cache_key(
        self,
        file_id: int,
        original_filename: str,
        include_speakers: bool = True,
        *,
        redaction_fingerprint: str,
    ) -> str:
        """Generate a cache key for processed video using original filename.

        Lives under the ``derived/`` prefix so a single MinIO lifecycle rule can
        auto-expire the regenerable derived cache (see ``DERIVED_CACHE_PREFIX``).

        ``redaction_fingerprint`` names the policy the subtitles were **rendered
        under** and is keyword-only and required so no call site can omit it by
        accident (issue #85). Burned-in text cannot be masked after the fact, so a
        policy-blind key freezes the first reader's masking for everyone: an admin
        enabling ``force_export_redacted`` would keep serving the already-cached
        unmasked video. It is ``""`` whenever nothing masks, which keeps the key
        byte-identical on a deployment with redaction disabled.
        """
        # Get base filename without extension
        base_name = (
            original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
        )
        speaker_suffix = "_with_speakers" if include_speakers else "_no_speakers"
        redaction_suffix = f"_r{redaction_fingerprint}" if redaction_fingerprint else ""
        # `file_id` FIRST, and it is what makes the key identifying. It was an
        # accepted parameter that the key never used, so every file sharing a
        # filename shared one cache object — in a single bucket with no user in
        # the key, so two people who each uploaded "meeting.mp4" were served each
        # other's derived artifact. Since #85 that artifact can be a burned-in
        # video, i.e. one user's recording rendered for another. The filename is
        # kept after it purely so a human can still read the object listing.
        return (
            f"{self.DERIVED_CACHE_PREFIX}{file_id}_{base_name}"
            f"{speaker_suffix}{redaction_suffix}.mp4"
        )

    def is_video_cached(self, cache_key: str) -> bool:
        """Check if a cached video exists."""
        try:
            # Check if cached video exists
            self.minio_service.stat_object(self.cache_bucket, cache_key)
            return True
        except Exception as exc:
            if not is_absent_error(exc):
                logger.warning("video cache probe failed for %s", cache_key, exc_info=True)
            return False

    @staticmethod
    def _media_filename(file_id: int) -> str:
        """Read the file's original name in a short session of its own.

        Raises:
            Exception: If the media file does not exist.
        """
        from app.models.media import MediaFile

        with session_scope() as db:
            row = db.query(MediaFile.filename).filter(MediaFile.id == file_id).first()
            if not row:
                raise Exception(f"Media file {file_id} not found")
            return str(row[0])

    @staticmethod
    def _media_audio_stream_index(file_id: int) -> int | None:
        """The audio track the user chose for this file (None = the first), in its own session."""
        from app.models.media import MediaFile

        with session_scope() as db:
            index: int | None = (
                db.query(MediaFile.audio_stream_index).filter(MediaFile.id == file_id).scalar()
            )
            return index

    def _upload_to_cache(
        self,
        output_path: Path,
        cache_key: str,
        output_format: str,
        content_type: str | None = None,
    ) -> None:
        """Upload a processed file to the cache bucket.

        Args:
            output_path: Local path of the processed file.
            cache_key: Object name to store it under.
            output_format: Container/format used to derive a default content type.
            content_type: Explicit MIME type; falls back to ``video/{output_format}``.
        """
        logger.info(f"Uploading processed file to cache bucket: {self.cache_bucket}/{cache_key}")
        self.minio_service.upload_file(
            file_path=str(output_path),
            bucket_name=self.cache_bucket,
            object_name=cache_key,
            content_type=content_type or f"video/{output_format}",
        )
        logger.info("Upload complete, processing finished")

    def derived_cache_keys(self, file_id: int, filename: str) -> list[str]:
        """Every derived-cache key a media file can own **at a known name**. Pure.

        Both subtitle-embedded video variants, all three audio-extract variants,
        and the three pre-hardening audio keys that carry no file id
        (``media_revocation_cache.legacy_audio_keys``): a file deleted after an
        upgrade must not leave its old extracted audio behind. Shared by the delete
        paths so the list cannot drift between them.

        ⚠️ This list is no longer exhaustive on its own. Since #85 a burned-in
        render is keyed by the redaction policy it was rendered under, so one file
        can own one video per distinct policy and no pure function can enumerate
        them. The two keys here are the *unmasked* renders (fingerprint ``""``);
        :meth:`clear_derived_cache` sweeps the masked variants by listing. Anything
        that only reads this list deletes less than it thinks it does.

        Args:
            file_id: Internal media file id.
            filename: The file's ORIGINAL filename, which the keys derive from.

        Returns:
            Cache keys under ``DERIVED_CACHE_PREFIX``.
        """
        return [
            self.generate_cache_key(
                file_id, filename, include_speakers=True, redaction_fingerprint=""
            ),
            self.generate_cache_key(
                file_id, filename, include_speakers=False, redaction_fingerprint=""
            ),
            self.audio_cache_key(file_id, filename, "mp3"),
            self.audio_cache_key(file_id, filename, "wav"),
            self.audio_cache_key(file_id, filename, "original"),
            *legacy_audio_keys(filename),
        ]

    def _masked_video_cache_keys(self, file_id: int, filename: str) -> list[str]:
        """List the policy-specific burned-in renders this file owns, from storage.

        A deleted file must not leave a video of its transcript behind, and the
        masked variants cannot be named without knowing every policy that ever
        rendered one — so they are listed rather than derived. The regex is not
        decoration: a bare prefix sweep on ``derived/{file_id}_{base}_`` would also
        match the neighbouring file ``{base}_2`` owned by the same id.

        ``file_id`` leads, matching :meth:`generate_cache_key`. It must: the key
        gained the id to stop two files sharing a filename from sharing one cache
        object, and a sweep that still matched on filename alone would delete the
        OTHER file's renders — turning a fixed collision into a destructive one.

        Raises:
            StorageDeleteIncompleteError: The listing failed, so whether burned-in renders
                of this file survive is unknown. Returning ``[]`` here made an outage look
                like "there are none" and an erasure audited complete over a surviving video.
        """
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        owned = f"{self.DERIVED_CACHE_PREFIX}{file_id}_{base}"
        pattern = re.compile(rf"^{re.escape(owned)}" r"_(with|no)_speakers_r[0-9a-f]+\.mp4$")
        try:
            objects = self.minio_service.list_objects(
                self.cache_bucket,
                prefix=f"{owned}_",
                recursive=True,
            )
            return [
                obj.object_name
                for obj in objects
                if obj.object_name and pattern.match(obj.object_name)
            ]
        except Exception as e:
            logger.exception(f"Could not list masked derived renders for {filename}: {e}")
            from app.services.storage_versions import StorageDeleteIncompleteError

            raise StorageDeleteIncompleteError(
                self.cache_bucket, [f"{owned}_*"], "could not list masked derived renders"
            ) from e

    def clear_derived_cache(self, file_id: int, filename: str) -> RemovalResult:
        """Delete a file's derived-cache objects. **Takes no DB session.**

        Five MinIO round trips plus one listing, so callers must not be holding a
        transaction: take the filename in the read phase and call this afterwards.
        Absent objects are not an error — these deletes are idempotent. The listing
        finds the policy-specific burned-in renders, which cannot be named without
        it (see :meth:`_masked_video_cache_keys`).

        Args:
            file_id: Internal media file id.
            filename: The file's original filename.

        Returns:
            The removal count; ``unverified`` is True when old versions could not be checked.

        Raises:
            storage_versions.StorageDeleteIncompleteError: A cached copy survived (named). This
                used to be logged at DEBUG and dropped, so an erasure that left a burned-in
                video of the transcript in storage still audited as complete.
        """
        keys = self.derived_cache_keys(file_id, filename) + self._masked_video_cache_keys(
            file_id, filename
        )
        removed = self.minio_service.delete_objects(self.cache_bucket, keys)
        logger.info(f"Cleared {removed} derived cache object(s) for file {file_id}")
        return removed
