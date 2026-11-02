"""Pipeline scratch volume and engine shared-volume path resolution.

The env-reading resolvers live here with the default paths they fall back to.
"""

import logging
import os

# =============================================================================
# Engine shared-volume WAV handoff (issue #661 E0, consolidated onto pipeline_scratch in E2)
# =============================================================================
# ``ENGINE_SHARED_VOLUME_PATH`` names the container path of the ``engine/`` namespace inside
# the single ``pipeline_scratch`` volume (mounted at ``PIPELINE_SCRATCH_DEFAULT`` by every
# compose service that reads or writes the handoff WAV: docker-compose.yml's
# celery-worker*/gpu-transcribe*/gpu-diarize* services, docker-compose.diar-native.yml's
# diar-native sidecar — read-only there) and set to the same value in .env.example. This is
# the ONE default all readers/writers must agree on. E2 folded what used to be a SEPARATE
# ``transcription-temp`` volume/filesystem into a namespace of ``pipeline_scratch`` so the
# preprocess → engine handoff (``preprocess.py``) can ``os.link`` instead of cross-filesystem
# copying — before that, a bare "/tmp" default (or a stale volume name) meant an install whose
# .env predated the variable wrote into the writer's container-local /tmp, the reader looked in
# the real mount, found nothing, and silently fell back to MinIO with no log distinguishing the
# two cases — the exact per-job re-serialization cost this shared-volume path exists to avoid.
ENGINE_SHARED_VOLUME_DEFAULT = "/scratch/opentranscribe/engine"


def pipeline_scratch_shared() -> bool:
    """Whether every worker shares the scratch mount (``PIPELINE_SCRATCH_SHARED``, default on).

    Read at call time so a test can flip it. Only the literal ``false`` turns it off, so a typo
    keeps the fast path instead of silently disabling it.
    """
    return os.environ.get("PIPELINE_SCRATCH_SHARED", "true").strip().lower() != "false"


def engine_shared_volume_enabled() -> bool:
    """Whether preprocess hands the WAV to the GPU task through the shared volume (#1151).

    ``ENGINE_SHARED_VOLUME_ENABLED`` decides when set. Unset, it follows
    ``PIPELINE_SCRATCH_SHARED``: a deployment that has declared its workers do not share a
    filesystem cannot share this directory either. Both default to on, so a single-host
    deployment keeps the fast path. Off, the GPU task always downloads the WAV from object
    storage — the right behaviour when preprocess and GPU workers run on different nodes,
    where a written WAV is never readable by the GPU worker and never cleaned up by anyone.
    """
    explicit = os.environ.get("ENGINE_SHARED_VOLUME_ENABLED", "").strip().lower()
    if explicit:
        return explicit != "false"
    return pipeline_scratch_shared()


def resolve_engine_shared_volume_path() -> str:
    """The handoff directory, ignoring a configured path that no longer exists.

    Every reader and writer of the engine handoff WAV must call this rather than reading
    ``ENGINE_SHARED_VOLUME_PATH`` directly, because an explicit value silently outlives the
    volume it named.

    E2 folded ``transcription-temp`` into a namespace of ``pipeline_scratch`` and moved the
    coded default here. But ``.env`` beats a coded default, and every install created before
    that change carries ``ENGINE_SHARED_VOLUME_PATH=/tmp/transcription`` from the old
    ``.env.example`` — a path that is no longer mounted anywhere. ``os.makedirs`` then
    cheerfully recreates it *inside the writer's container*, the GPU worker looks in the real
    mount, finds nothing, and falls back to MinIO. Measured on a live upgraded stack: the
    handoff silently degraded on every job, which is precisely the regression E0 was written
    to make visible.

    So: if the configured directory does not exist but the coded default does, the configured
    value is stale — prefer the default and say so loudly. On a correctly configured install
    the configured path exists (the image pre-creates it and the volume mounts over it) —
    **but existing is NOT sufficient proof it is the real mount.** ``preprocess.py`` used to do
    ``os.makedirs(os.environ.get("ENGINE_SHARED_VOLUME_PATH", "/tmp"))`` unconditionally, which
    recreates a stale configured path *inside the writer's own writable layer* the moment
    anything tries to use it — so a present-but-stale directory is exactly the failure mode
    this function exists to catch, not a case where the check is inert. It deliberately does
    NOT rewrite ``.env``; this repo never edits an operator's ``.env`` without confirmation.
    """
    configured = os.environ.get("ENGINE_SHARED_VOLUME_PATH")
    if not configured or configured == ENGINE_SHARED_VOLUME_DEFAULT:
        return ENGINE_SHARED_VOLUME_DEFAULT

    configured_exists = os.path.isdir(configured)
    default_exists = os.path.isdir(ENGINE_SHARED_VOLUME_DEFAULT)

    if not configured_exists:
        if not default_exists:
            # Neither exists. Honour the operator's value rather than inventing one — the
            # caller's own mkdir//not-found logging is then the accurate signal.
            return configured
        logging.getLogger(__name__).warning(
            "ENGINE_SHARED_VOLUME_PATH=%s does not exist in this container, but %s does — "
            "using the latter. This usually means .env predates the pipeline_scratch volume "
            "consolidation (issue #661 E2), which removed the transcription-temp volume that "
            "path named. Update ENGINE_SHARED_VOLUME_PATH in .env to silence this.",
            configured,
            ENGINE_SHARED_VOLUME_DEFAULT,
        )
        return ENGINE_SHARED_VOLUME_DEFAULT

    if not default_exists:
        # This container never got the shared mount at all (e.g. a reader that predates it)
        # — nothing to compare against, so honour the operator's value.
        return configured

    # Both paths "exist" — but a directory recreated by a stale os.makedirs call in this
    # container's writable layer also passes os.path.isdir. Discriminate by filesystem
    # device: the real shared mount and the coded default live on the same mounted volume,
    # a stale local directory does not.
    configured_dev = _stat_dev(configured)
    default_dev = _stat_dev(ENGINE_SHARED_VOLUME_DEFAULT)
    if configured_dev is not None and configured_dev == default_dev:
        return configured

    logging.getLogger(__name__).warning(
        "ENGINE_SHARED_VOLUME_PATH=%s exists but is not on the same filesystem as the "
        "shared %s mount — treating it as a stale directory left behind in this "
        "container's writable layer (os.makedirs recreates a missing configured path "
        "there silently) rather than the real shared volume, and using the default "
        "instead. This usually means .env predates the pipeline_scratch volume "
        "consolidation (issue #661 E2), which removed the transcription-temp volume that "
        "path named. Update ENGINE_SHARED_VOLUME_PATH in .env to silence this.",
        configured,
        ENGINE_SHARED_VOLUME_DEFAULT,
    )
    return ENGINE_SHARED_VOLUME_DEFAULT


def _stat_dev(path: str) -> int | None:
    """``os.stat(path).st_dev``, or ``None`` if it cannot be stat'd.

    Broken out so tests can simulate two directories that exist but live on different
    filesystems — every real container does (a bind-mounted named volume vs. the image's
    writable layer), but a pytest ``tmp_path`` fixture puts everything on one device, so a
    filesystem-only test can't exercise the real comparison. Tests monkeypatch this function
    directly instead.
    """
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


# Root of the consolidated pipeline scratch volume (issue #661 E2) — one named volume,
# ``pipeline_scratch``, three namespaces: ``<file_uuid>/`` (scratch_volume.py, per-file),
# ``engine/`` (ENGINE_SHARED_VOLUME_DEFAULT above, per-task), and ``diar/``
# (DIAR_NATIVE_SHARED_DIR_DEFAULT below, the sidecar's own-copy fallback). Same filesystem is
# what makes the engine handoff an ``os.link`` instead of a cross-filesystem copy.
PIPELINE_SCRATCH_DEFAULT = "/scratch/opentranscribe"

# Sidecar's own-copy fallback directory when it cannot reuse a hard-linked WAV (diarizer_native.py).
DIAR_NATIVE_SHARED_DIR_DEFAULT = "/scratch/opentranscribe/diar"

# Top-level directory names inside PIPELINE_SCRATCH_DEFAULT that are NOT per-file UUID dirs.
# The janitor (scratch_volume.sweep_expired) must sweep files INSIDE these by their own mtime
# and never rmtree the namespace directory itself — unlike an ordinary <file_uuid>/ dir, these
# directories' own mtime bumps on every file created inside them, so treating them like an
# ordinary per-file dir would eventually rmtree an in-flight handoff WAV out from under a
# running job (issue #661 E2 phase 1.2).
RESERVED_SCRATCH_NAMESPACES = frozenset({"engine", "diar"})
