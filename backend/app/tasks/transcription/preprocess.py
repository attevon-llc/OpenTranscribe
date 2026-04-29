"""CPU preprocessing task for the transcription pipeline.

Downloads media from MinIO, extracts audio via FFmpeg, and stages
the normalized audio.wav in MinIO temp storage for the GPU worker.

Part of the 3-stage chain: preprocess (CPU) → transcribe (GPU) → postprocess (CPU)
"""

import contextlib
import logging
import os
import re
import shutil
import tempfile
import time

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils import benchmark_timing
from app.utils.error_classification import categorize_error
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

from .audio_processor import extract_audio_from_video
from .audio_processor import get_audio_file_extension
from .audio_processor import prepare_audio_for_transcription
from .metadata_extractor import extract_media_metadata
from .metadata_extractor import extract_media_metadata_from_url
from .metadata_extractor import update_media_file_metadata
from .notifications import send_error_notification
from .notifications import send_progress_notification

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="transcription.preprocess",
    priority=CPUPriority.PIPELINE_CRITICAL,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError, IOError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def preprocess_for_transcription(
    self,
    file_uuid: str,
    task_id: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    disable_diarization: bool | None = None,
    diarization_source: str | None = None,
    whisper_model: str | None = None,
) -> dict:
    """Download media, extract audio, upload to MinIO temp for GPU worker.

    Returns context dict consumed by the GPU transcription task via Celery chain.
    """
    from app.services.minio_service import upload_temp_audio
    from app.utils.uuid_helpers import get_file_by_uuid

    step_start = time.perf_counter()

    # Record task pickup time + cold-start status for queue-wait analysis.
    benchmark_timing.mark(task_id, "preprocess_task_prerun")
    benchmark_timing.mark_cold_start(task_id, "cpu")

    try:
        # Resolve file from DB
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                raise ValueError(f"Media file {file_uuid} not found")

            file_id = int(media_file.id)
            user_id = int(media_file.user_id)
            storage_path = str(media_file.storage_path)
            file_name = str(media_file.filename)
            content_type = str(media_file.content_type)

            update_task_status(db, task_id, "in_progress", progress=0.05)

        send_progress_notification(user_id, file_id, 0.05, "Preparing media file")

        file_ext = get_audio_file_extension(content_type, file_name)
        is_video = content_type.startswith("video/")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_audio_path = os.path.join(temp_dir, "audio.wav")

            if is_video:
                _preprocess_video(
                    storage_path,
                    file_ext,
                    temp_dir,
                    temp_audio_path,
                    file_id,
                    user_id,
                    content_type,
                    task_id,
                )
            else:
                _preprocess_audio(
                    storage_path,
                    file_ext,
                    temp_dir,
                    temp_audio_path,
                    file_id,
                    user_id,
                    content_type,
                    task_id,
                )

            # Upload preprocessed audio to MinIO temp for GPU worker
            send_progress_notification(user_id, file_id, 0.18, "Staging audio for transcription")
            audio_size_mb = os.path.getsize(temp_audio_path) / (1024 * 1024)
            with benchmark_timing.stage(task_id, "temp_upload"):
                audio_temp_path = upload_temp_audio(file_uuid, temp_audio_path)

            # Phase 1b Opt-3A: copy WAV to shared volume so GPU task can mmap-load (sub-second)
            # instead of downloading from MinIO (~2–5 s). Falls back to MinIO if write fails.
            local_wav_path = ""
            try:
                _shared_vol = os.environ.get(
                    "ENGINE_SHARED_VOLUME_PATH",
                    "/tmp",  # noqa: S108  # nosec B108
                )
                os.makedirs(_shared_vol, exist_ok=True)
                _safe_task_id = re.sub(r"[^a-zA-Z0-9\-]", "_", task_id)
                _wav_dest = os.path.join(_shared_vol, f"{_safe_task_id}.wav")
                shutil.copy2(temp_audio_path, _wav_dest)
                local_wav_path = _wav_dest
                logger.info(
                    "Engine shared-volume WAV: %s (%d bytes)",
                    _wav_dest,
                    os.path.getsize(_wav_dest),
                )
            except Exception as _shv_err:
                logger.warning(
                    "Shared-volume WAV write failed, GPU task will use MinIO: %s", _shv_err
                )

            # Dispatch waveform generation in parallel with the GPU task.
            # Previously fired from upload.py, which forced a full re-download
            # of the original media (Phase 2 PR #3 fix). Now it consumes the
            # preprocessed 16 kHz WAV we've just staged — ~10x less I/O.
            # Only runs for files that don't already have waveform_data
            # (skips reprocess runs).
            _dispatch_waveform_if_missing(
                file_id, file_uuid, task_id, local_wav_path=local_wav_path
            )

        # Resolve diarization_source: explicit arg > legacy bool > user DB setting
        if diarization_source is None:
            if disable_diarization is not None:
                # Legacy callers: convert bool to diarization_source
                diarization_source = "off" if disable_diarization else "provider"
            else:
                from .core import _get_user_transcription_settings

                with session_scope() as db_settings:
                    user_ts = _get_user_transcription_settings(db_settings, user_id)
                    diarization_source = user_ts.get("diarization_source", "provider")

        # Compute disable_diarization from diarization_source for backward compat
        disable_diarization = diarization_source == "off"

        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.20)

        elapsed = time.perf_counter() - step_start
        logger.info(
            f"TIMING: preprocess completed in {elapsed:.3f}s for file {file_id} "
            f"(audio: {audio_size_mb:.1f}MB)"
        )

        # Record preprocess end timestamp for inter-stage gap measurement
        benchmark_timing.mark(task_id, "preprocess_end")

        send_progress_notification(user_id, file_id, 0.20, "Audio ready for transcription")

        return {
            "file_uuid": file_uuid,
            "file_id": file_id,
            "user_id": user_id,
            "task_id": task_id,
            "audio_temp_path": audio_temp_path,
            "content_type": content_type,
            "file_name": file_name,
            "storage_path": storage_path,
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "num_speakers": num_speakers,
            "downstream_tasks": downstream_tasks,
            "source_language": source_language,
            "translate_to_english": translate_to_english,
            "disable_diarization": disable_diarization,
            "diarization_source": diarization_source,
            "whisper_model": whisper_model,
            "local_wav_path": local_wav_path,
        }

    except Exception as e:
        logger.error(f"Preprocess failed for file {file_uuid}: {e}")
        _mark_pipeline_error(file_uuid, task_id, f"Audio preprocessing failed: {e}")
        raise


def _preprocess_video(
    storage_path: str,
    file_ext: str,
    temp_dir: str,
    temp_audio_path: str,
    file_id: int,
    user_id: int,
    content_type: str,
    task_id: str,
) -> None:
    """Extract audio + metadata from a video in parallel, with presigned-URL fallback.

    Phase 2 PR #9 (item D11): FFmpeg audio extraction and ffprobe metadata
    reads are independent subprocess calls against the same source (presigned
    MinIO URL or downloaded file). They now run concurrently on a
    ``ThreadPoolExecutor`` — subprocesses release the GIL, so threading buys
    real parallelism. Eliminates 1-3 s of serial latency per video on the
    critical path.

    If the presigned URL path fails (rare — typically only in offline
    deployments), we fall back to a single local download and then run the
    same two operations concurrently against the on-disk file.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.minio_service import download_file_to_path
    from app.services.minio_service import get_internal_presigned_url

    send_progress_notification(user_id, file_id, 0.08, "Extracting audio from video")

    # Resolve the source once. The presigned URL lets FFmpeg + ffprobe read
    # only the byte ranges they need (no full download); we only fall back
    # to a local copy if the presigned route actually fails at runtime.
    presigned_url: str | None = None
    try:
        presigned_url = get_internal_presigned_url(storage_path, expires=3600)
    except Exception as url_err:
        logger.warning(f"Could not mint presigned URL, will fall back: {url_err}")

    local_video_path: str | None = None

    def _run_ffmpeg_against(source: str) -> None:
        with benchmark_timing.stage(task_id, "ffmpeg"):
            extract_audio_from_video(source, temp_audio_path)

    def _run_metadata_against(source_url_or_path: str, is_url: bool) -> None:
        # Reuses the best-effort metadata helper but hands in the exact
        # source we want; no second MinIO download happens inside.
        _extract_metadata_best_effort(
            storage_path,
            file_ext,
            temp_dir,
            file_id,
            content_type,
            existing_local_path=None if is_url else source_url_or_path,
            presigned_url=source_url_or_path if is_url else None,
            task_id=task_id,
        )

    # Fast path: both stages run in parallel against the presigned URL.
    if presigned_url is not None:
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="preproc-video") as pool:
                ffmpeg_future = pool.submit(_run_ffmpeg_against, presigned_url)
                metadata_future = pool.submit(_run_metadata_against, presigned_url, True)
                # Propagate the FFmpeg error (pipeline-fatal); the metadata
                # extraction is best-effort and already swallows its own
                # exceptions inside ``_extract_metadata_best_effort``.
                ffmpeg_future.result()
                # Wait for metadata so its wall-clock joins the preprocess
                # window rather than racing into the next stage's markers.
                metadata_future.result()
            return
        except Exception as url_err:
            logger.warning(
                f"Parallel presigned-URL preprocess failed, falling back to download: {url_err}"
            )

    # Fallback path: single download, then parallel local FFmpeg + metadata.
    temp_video_path = os.path.join(temp_dir, f"input{file_ext}")
    with benchmark_timing.stage(task_id, "media_download"):
        download_file_to_path(storage_path, temp_video_path)
    local_video_path = temp_video_path

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="preproc-video") as pool:
        ffmpeg_future = pool.submit(_run_ffmpeg_against, local_video_path)
        metadata_future = pool.submit(_run_metadata_against, local_video_path, False)
        ffmpeg_future.result()
        metadata_future.result()


def _preprocess_audio(
    storage_path: str,
    file_ext: str,
    temp_dir: str,
    temp_audio_path: str,
    file_id: int,
    user_id: int,
    content_type: str,
    task_id: str,
) -> None:
    """Download audio file and convert to WAV."""
    from app.services.minio_service import download_file_to_path

    send_progress_notification(user_id, file_id, 0.08, "Processing audio file")
    temp_input_path = os.path.join(temp_dir, f"input{file_ext}")
    with benchmark_timing.stage(task_id, "media_download"):
        download_file_to_path(storage_path, temp_input_path)

    # Metadata extraction
    _extract_metadata_best_effort(
        storage_path,
        file_ext,
        temp_dir,
        file_id,
        content_type,
        existing_local_path=temp_input_path,
        task_id=task_id,
    )

    # Convert to WAV (modifies temp_audio_path in-place via prepare_audio_for_transcription)
    with benchmark_timing.stage(task_id, "ffmpeg"):
        result_path = prepare_audio_for_transcription(temp_input_path, content_type, temp_dir)

    # If prepare returned a different path (e.g., input was already .wav), copy it
    if result_path != temp_audio_path:
        shutil.copy2(result_path, temp_audio_path)


def _extract_metadata_best_effort(
    storage_path: str,
    file_ext: str,
    temp_dir: str,
    file_id: int,
    content_type: str,
    existing_local_path: str | None = None,
    presigned_url: str | None = None,
    task_id: str | None = None,
) -> None:
    """Extract media metadata. Best-effort — failures are logged but don't stop pipeline.

    Ordered preference:
      1. ``existing_local_path`` — file is already on disk (fallback path).
      2. ``presigned_url`` — run ffprobe against MinIO directly, no download.
      3. Download the original (legacy behaviour; only triggered when
         neither of the above is supplied).
    """
    from app.services.minio_service import download_file_to_path

    with benchmark_timing.stage(task_id, "metadata"):
        try:
            metadata: dict | None = None
            local_path_for_raw: str | None = existing_local_path

            if existing_local_path and os.path.exists(existing_local_path):
                metadata = extract_media_metadata(existing_local_path)
            elif presigned_url:
                metadata = extract_media_metadata_from_url(presigned_url)
            else:
                fallback_local = os.path.join(temp_dir, f"meta_input{file_ext}")
                download_file_to_path(storage_path, fallback_local)
                metadata = extract_media_metadata(fallback_local)
                local_path_for_raw = fallback_local

            if metadata:
                with session_scope() as db:
                    mf = get_refreshed_object(db, MediaFile, file_id)
                    if mf:
                        update_media_file_metadata(
                            mf, metadata, content_type, local_path_for_raw or ""
                        )
                        # Persist audio duration into benchmark context when known
                        duration_val = metadata.get("Duration") or metadata.get("duration")
                        if duration_val is not None:
                            with contextlib.suppress(TypeError, ValueError):
                                benchmark_timing.set_context(
                                    task_id,
                                    {"audio_duration_s": float(duration_val)},
                                )
                        db.commit()
        except Exception as e:
            logger.warning(f"Metadata extraction failed for file {file_id} (non-fatal): {e}")


def _dispatch_waveform_if_missing(
    file_id: int,
    file_uuid: str,
    task_id: str,
    local_wav_path: str = "",
) -> None:
    """Fire waveform generation for files that don't have it yet.

    Fresh uploads skip this if ``MediaFile.waveform_data`` is populated;
    reprocess runs hit that condition. When ``local_wav_path`` is provided
    the waveform task uses the already-decoded 16 kHz WAV directly (numpy
    resample, no FFmpeg), avoiding a second audio decode.
    """
    try:
        with session_scope() as db:
            media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if media_file is None or media_file.waveform_data:
                return
        from app.tasks.waveform import generate_waveform_task

        generate_waveform_task.delay(
            file_id=file_id,
            file_uuid=file_uuid,
            task_id=task_id,
            prefer_temp_audio=True,
            local_wav_path=local_wav_path or None,
        )
        logger.info(f"Dispatched waveform task for file {file_id} from preprocess")
    except Exception as e:
        logger.warning(f"Waveform dispatch from preprocess failed (non-fatal): {e}")


def _mark_pipeline_error(file_uuid: str, task_id: str, error_msg: str) -> None:
    """Mark file and task as failed."""
    from app.utils.uuid_helpers import get_file_by_uuid

    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            if media_file:
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                media_file.last_error_message = error_msg
                media_file.error_category = categorize_error(error_msg).value
                db.commit()
                send_error_notification(int(media_file.user_id), int(media_file.id), error_msg)
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
    except Exception as status_err:
        logger.error(f"Failed to update error status: {status_err}")
