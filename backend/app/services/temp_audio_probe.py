"""Existence probe for the preprocessed WAV (scratch volume, then MinIO temp).

Kept apart from ``minio_service`` so the "absent vs unreachable" distinction lives in one
small module. An outage is never reported as "absent".
"""

import logging

from app.core.config import settings
from app.services.minio_service import _temp_audio_object_name
from app.services.minio_service import minio_client
from app.services.storage_absence import is_absent_error


class TempAudioUnavailableError(Exception):
    """Storage could not say whether the preprocessed WAV exists (outage, not absence)."""


def temp_audio_exists(file_uuid: str) -> bool:
    """Return True when the preprocessed WAV is reachable from either source.

    Returns False only when storage definitively answers "not there". Raises
    ``TempAudioUnavailableError`` when it cannot answer, so an outage is never
    mistaken for a missing file.
    """
    from app.utils import scratch_volume

    if scratch_volume.scratch_audio_path(file_uuid).exists():
        return True
    try:
        minio_client.stat_object(settings.MEDIA_BUCKET_NAME, _temp_audio_object_name(file_uuid))
        return True
    except Exception as exc:
        if is_absent_error(exc):
            return False
        logging.getLogger(__name__).warning(
            "temp audio existence check for %s failed (storage unreachable?): %s", file_uuid, exc
        )
        raise TempAudioUnavailableError(
            f"cannot determine whether temp audio exists for {file_uuid}: {exc}"
        ) from exc


def temp_audio_available(file_uuid: str, *, caller: str) -> bool:
    """``temp_audio_exists`` for callers with a full-download fallback.

    An outage returns False (the fallback still runs) but is logged as "storage
    unreachable", so skipping the cheap path is never mistaken for "file absent".
    """
    try:
        return temp_audio_exists(file_uuid)
    except TempAudioUnavailableError as unavailable:
        logging.getLogger(__name__).warning(
            "%s: storage unreachable, skipping preprocessed-WAV reuse (NOT because it is "
            "absent): %s",
            caller,
            unavailable,
        )
        return False
