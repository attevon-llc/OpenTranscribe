"""Shared-volume containment and reuse-decision logging for the diar-native WAV handoff.

Split out of ``diarizer_native.py``: where the pipeline_scratch volume is rooted as seen from
THIS container, whether a staged WAV lives inside it (real path containment, never
``startswith``), and the observable log line that says whether the reuse-WAV optimisation fired.
"""

from __future__ import annotations

import logging
import os

from app.core.scratch_paths import PIPELINE_SCRATCH_DEFAULT
from app.core.scratch_paths import resolve_engine_shared_volume_path

logger = logging.getLogger(__name__)

# The engine's Stage-1 shared-volume WAV mount, as seen from THIS container (the worker, not
# the sidecar). Same env var EngineConfig reads for shared_volume_path — deliberately not a
# second knob, since issue #661 is precisely about reusing the WAV that variable locates.
#
# The default is `ENGINE_SHARED_VOLUME_DEFAULT` (app.core.constants) — the SAME constant
# `engine/config.py` and `tasks/transcription/preprocess.py` fall back to. Before issue #661's
# E0 fix this file alone defaulted to "/tmp/transcription" while the write side
# (preprocess.py) and EngineConfig both defaulted to a bare "/tmp": an install whose `.env`
# predated `ENGINE_SHARED_VOLUME_PATH` wrote the WAV into the writer's container-LOCAL /tmp,
# this reader looked in the real mount, found nothing, and silently fell back to MinIO — the
# exact per-job re-serialization cost this shared-volume path exists to avoid. Unifying all
# three sites on the mount path (not a bare /tmp) fixes that for exactly the install this
# mattered for, with no prefix-collision risk: the writer now targets the same mounted
# directory the sidecar reads, so there is no local-/tmp file for the prefix check below to
# mistake for a shared one. Since "reuse silently never fires" is itself a failure mode
# nothing else would surface, diarize() below logs which case fired on every call — see the
# "reuse-WAV optimisation" log lines.
_ENGINE_SHARED_DIR = resolve_engine_shared_volume_path()

# Containment boundary for the reuse-WAV check, keyed on the pipeline_scratch VOLUME ROOT
# (issue #661 E2 phase 1.3) — NOT `_ENGINE_SHARED_DIR`. `str.startswith` against a subdirectory
# prefix has two independent problems: it is not real path containment (no `..` resolution, no
# symlink resolution — "/scratch/opentranscribe-evil/x.wav" passes a naive prefix test because
# the STRING happens to start with the prefix), and after E2 the engine WAV lives under
# `.../engine/<task>.wav` while rediarize's WAV lives under `.../<uuid>/audio.wav` — both on the
# same volume, but a prefix keyed on the `engine/` subdir would reject the second, silently
# forcing `_post_own_copy` on every rediarize. `PIPELINE_SCRATCH_DIR` mirrors the same env var
# `app.utils.scratch_volume` reads, so both sides of the reuse check agree on one root.
_SHARED_VOLUME_ROOT = os.path.realpath(
    os.environ.get("PIPELINE_SCRATCH_DIR", PIPELINE_SCRATCH_DEFAULT)
)


def _path_is_on_shared_volume(path: str) -> bool:
    """True when ``path`` resolves to somewhere inside the pipeline_scratch volume root.

    Uses ``os.path.realpath`` on both sides (resolves ``..`` and symlinks) plus
    ``os.path.commonpath`` — never ``str.startswith``, which a sibling directory sharing the
    same string prefix (e.g. ``/scratch/opentranscribe-evil``) would pass.
    """
    try:
        real = os.path.realpath(path)
        return os.path.commonpath([real, _SHARED_VOLUME_ROOT]) == _SHARED_VOLUME_ROOT
    except (ValueError, OSError):
        # ValueError: paths on different drives (Windows) or otherwise incomparable.
        return False


def _log_reuse_decision(wav_path: str | None, reused: bool) -> None:
    """Observable signal for issue #661's E0 (make the fast path run, and PROVE it).

    Silence here is exactly the failure mode that let the reuse optimisation never fire on
    an install whose ``.env`` predates ``ENGINE_SHARED_VOLUME_PATH`` — every health check
    stayed green while every diarization quietly took the slower re-encode path.
    """
    if wav_path and not reused:
        if not _path_is_on_shared_volume(wav_path):
            logger.info(
                "diar-native reuse-WAV optimisation NOT used: %s is not under the shared "
                "volume root %s. If this worker and the sidecar should share a volume, set "
                "PIPELINE_SCRATCH_DIR the same way in both (see .env.example).",
                wav_path,
                _SHARED_VOLUME_ROOT,
            )
        else:
            logger.info(
                "diar-native reuse-WAV optimisation NOT used: staged path %s does not "
                "exist on disk.",
                wav_path,
            )
    elif reused:
        logger.info("diar-native reusing staged WAV at %s", wav_path)
