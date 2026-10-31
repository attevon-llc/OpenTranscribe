"""Attributes and cross-module calls the ``VideoProcessingService`` mixins rely on.

``video_subtitle_burn`` and ``audio_extraction`` are mixed into
:class:`~app.services.video_processing_service.VideoProcessingService`, which sets
``minio_service`` / ``cache_bucket`` in ``__init__`` and owns the cache-key, cache-probe and
upload helpers. Declaring them here lets the type checker verify each mixin against the same
contract; nothing in this class exists at runtime beyond the annotations.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from app.services.minio_bucket_admin import MinIOService


class VideoProcessingHost:
    """Contract a mixin may assume of the ``VideoProcessingService`` it is mixed into."""

    DERIVED_CACHE_PREFIX: str
    minio_service: MinIOService
    cache_bucket: str

    if TYPE_CHECKING:

        @staticmethod
        def _media_filename(file_id: int) -> str: ...

        def generate_cache_key(
            self,
            file_id: int,
            original_filename: str,
            include_speakers: bool = True,
            *,
            redaction_fingerprint: str,
        ) -> str: ...

        def is_video_cached(self, cache_key: str) -> bool: ...

        def _send_download_progress_sync(
            self,
            user_id: int,
            file_id: int,
            status: str,
            progress: int | None = None,
            error: str | None = None,
        ) -> None: ...

        def _upload_to_cache(
            self,
            output_path: Path,
            cache_key: str,
            output_format: str,
            content_type: str | None = None,
        ) -> None: ...
