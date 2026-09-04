"""Shared scratch volume for cross-worker artifact handoff.

The preprocess task produces a 16 kHz mono WAV that the GPU worker, the
waveform task, and the cloud-ASR speaker-embedding task all need to
consume. Routing that through MinIO costs one upload + N downloads per
file. When workers share a host (the default OpenTranscribe deployment)
a plain Docker volume at ``/scratch/opentranscribe`` lets us hand the
WAV off by file link instead — approximately zero I/O beyond the initial
ffmpeg write.

Design properties (Phase 2 PR #4 of the timing audit plan):

- **Always-on when the directory exists** — presence of the mount is the
  feature flag. No "is this enabled" env var to forget.
- **Graceful fallback** — every caller has a MinIO fallback, so
  multi-host deployments and laptops without the mount keep working.
- **Adaptive backing** — the volume can be a regular disk volume
  (default, works everywhere) or tmpfs (opt-in for servers with
  abundant RAM via ``docker-compose`` override). The helper doesn't
  care; both look identical.
- **Same-host fast path** — when the writer and reader share the host,
  ``write_audio`` renames the file (atomic, no copy) into the scratch
  dir; readers just ``stat`` + read.
- **TTL cleanup** — janitor task purges ``{file_uuid}/`` dirs older
  than the TTL so a crashed pipeline doesn't leak forever.

Callers should treat ``is_scratch_available()`` as the single check —
downstream readers try ``read_audio()`` first and MinIO second.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from app.core.constants import PIPELINE_SCRATCH_DEFAULT
from app.core.constants import RESERVED_SCRATCH_NAMESPACES

logger = logging.getLogger(__name__)

# Root of the shared volume inside any container that mounts it. Override
# via ``PIPELINE_SCRATCH_DIR`` for non-standard deployments.
SCRATCH_DIR = Path(os.environ.get("PIPELINE_SCRATCH_DIR", PIPELINE_SCRATCH_DEFAULT))

# Name of the audio artifact inside each per-file directory.
AUDIO_FILENAME = "audio.wav"

# Default TTL for janitor cleanup. 1 hour is enough for the longest
# typical pipeline run (3-hour audio ≈ 12-min wall-clock on A6000).
DEFAULT_TTL_SECONDS = 60 * 60


def is_scratch_available() -> bool:
    """Return True when the scratch volume is mounted, writable, and SHARED.

    Checked on every call — the mount can appear/disappear at runtime
    on systems using systemd-managed bind mounts. Cheap syscall.

    ``PIPELINE_SCRATCH_SHARED=false`` disables the fast path outright (issue #284
    A1.19). The scratch hand-off writes the preprocessed WAV to a local path and skips
    the MinIO upload entirely, which is correct only when every worker sees the SAME
    filesystem. On a multi-node deployment each pod gets its own emptyDir, so the CPU
    pod stages the audio somewhere the GPU pod cannot read and EVERY file fails — with
    a confusing missing-file error rather than anything pointing at the mount. A
    single-host Docker deployment (the default) shares the volume, so this stays on.
    """
    if not _scratch_is_shared():
        return False
    try:
        if not SCRATCH_DIR.is_dir():
            return False
        return os.access(SCRATCH_DIR, os.W_OK | os.X_OK)
    except OSError:
        return False


def _scratch_is_shared() -> bool:
    """Whether the scratch mount is shared by all workers (env-gated kill switch)."""
    return os.getenv("PIPELINE_SCRATCH_SHARED", "true").strip().lower() != "false"


def scratch_dir_for(file_uuid: str) -> Path:
    """Per-file subdirectory inside the scratch volume."""
    return SCRATCH_DIR / str(file_uuid)


def scratch_audio_path(file_uuid: str) -> Path:
    """Canonical path for the preprocessed WAV artifact."""
    return scratch_dir_for(file_uuid) / AUDIO_FILENAME


def write_audio(file_uuid: str, src_path: str) -> Path | None:
    """Hard-link (or copy) ``src_path`` into the scratch volume as the canonical WAV.

    Returns the destination path on success, None when scratch is not
    available or the write fails. ``src_path`` is left intact on every
    success path — callers (``minio_service.upload_temp_audio``'s caller,
    ``tasks/transcription/preprocess.py``) read it again afterward to stage
    the engine shared-volume WAV, so this must never remove the source.

    Uses ``os.link`` when the source sits on the same filesystem (same
    inode, zero data copy, and — unlike ``os.replace`` — the source's
    directory entry is untouched); falls back to a full copy on ``EXDEV``
    (cross-filesystem) or when the source already has another hard link at
    the destination.

    This used to try ``os.replace`` first, which is a MOVE: it deletes the
    source's directory entry as its whole mechanism, contradicting this
    docstring's older "copy + unlink" description (there was no unlink —
    ``os.replace`` needs none, having already removed the source itself)
    and silently breaking every caller that reads ``src_path`` again after
    the call. It only ever worked because the two paths passed in practice
    (a container-local ``/tmp`` temp dir and the ``pipeline_scratch`` named
    volume) sit on different filesystems, so the rename always hit
    ``EXDEV`` and fell through to the (source-preserving) copy branch —
    putting them on one filesystem would have made the rename succeed and
    silently emptied ``local_wav_path`` on every job (issue #661 E1).
    """
    if not is_scratch_available():
        return None
    if not src_path or not os.path.exists(src_path):
        return None

    dest_dir = scratch_dir_for(file_uuid)
    dest = scratch_audio_path(file_uuid)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if os.path.exists(dest):
            os.unlink(dest)
        # Try a hard link first (same inode, zero copy) — never removes the source.
        try:
            os.link(src_path, dest)
        except OSError:
            # Cross-filesystem, or some other reason linking isn't possible — copy instead.
            shutil.copy2(src_path, dest)
        logger.debug(f"staged WAV to scratch: {dest}")
        return dest
    except OSError as e:
        logger.warning(f"scratch write_audio({file_uuid}) failed: {e}")
        return None


def read_audio(file_uuid: str, dest_path: str) -> bool:
    """Copy the scratch WAV to ``dest_path``. Returns True when copied.

    Returns False when scratch isn't available or the file is missing —
    callers should then fall back to MinIO. A hard link is used when
    possible so we don't double the RAM pressure on tmpfs.
    """
    if not is_scratch_available():
        return False
    src = scratch_audio_path(file_uuid)
    if not src.exists():
        return False
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        # Try a hard link first — same inode, zero copy.
        try:
            if os.path.exists(dest_path):
                os.unlink(dest_path)
            os.link(src, dest_path)
        except OSError:
            shutil.copy2(src, dest_path)
        return True
    except OSError as e:
        logger.warning(f"scratch read_audio({file_uuid}) failed: {e}")
        return False


def cleanup(file_uuid: str) -> None:
    """Remove the per-file scratch directory (best-effort)."""
    if not is_scratch_available():
        return
    path = scratch_dir_for(file_uuid)
    if not path.exists():
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.debug(f"cleaned scratch dir: {path}")
    except OSError as e:
        logger.debug(f"scratch cleanup({file_uuid}) failed (non-fatal): {e}")


def sweep_expired(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[int, int]:
    """Remove per-file scratch dirs older than ``ttl_seconds``.

    Returns a ``(removed_count, error_count)`` tuple. Designed for the
    periodic janitor Celery task; safe to run concurrently with the
    pipeline because the janitor only touches dirs whose mtime exceeds
    the TTL (well past the typical pipeline wall-clock).
    """
    if not is_scratch_available():
        return (0, 0)
    cutoff = time.time() - ttl_seconds
    removed = 0
    errors = 0
    try:
        entries = list(SCRATCH_DIR.iterdir())
    except OSError as e:
        logger.warning(f"scratch sweep failed to list dir: {e}")
        return (0, 1)
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if entry.name in RESERVED_SCRATCH_NAMESPACES:
                # Reserved namespace (engine/, diar/) — its own mtime bumps on every file
                # created inside it, so it would never look "expired" as a whole dir, and the
                # one day it did the sweep would rmtree in-flight WAVs wholesale. Sweep the
                # files inside it individually, by each file's own mtime; never remove the
                # namespace directory itself.
                r, e_count = _sweep_reserved_namespace(entry, cutoff)
                removed += r
                errors += e_count
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError as e:
            errors += 1
            logger.debug(f"scratch sweep failed on {entry}: {e}")
    return (removed, errors)


def _sweep_reserved_namespace(namespace_dir: Path, cutoff: float) -> tuple[int, int]:
    """Remove stale files (not subdirectories) inside a reserved namespace by their own mtime."""
    removed = 0
    errors = 0
    try:
        files = list(namespace_dir.iterdir())
    except OSError as e:
        logger.debug(f"scratch sweep failed to list reserved dir {namespace_dir}: {e}")
        return (0, 1)
    for f in files:
        try:
            if not f.is_file():
                continue
            if f.stat().st_mtime > cutoff:
                continue
            f.unlink()
            removed += 1
        except OSError as e:
            errors += 1
            logger.debug(f"scratch sweep failed on {f}: {e}")
    return (removed, errors)
