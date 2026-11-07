"""Turn off third-party telemetry and update checks before any library that reads them loads.

A self-hosted install must send nothing to third parties on its own. Several dependencies
phone home by default and read their opt-out switch from the environment at IMPORT time, so
the switch only works if it is set before the library is first imported:

* ``pyannote.audio`` (the pinned fork) builds an OTLP exporter to ``otel.pyannote.ai`` when
  ``pyannote.audio.telemetry`` is imported, and if ``PYANNOTE_METRICS_ENABLED`` is unset it
  writes ``"true"`` into the environment from its bundled ``config.yaml`` — after which a
  ``setdefault`` here would be a no-op. Model/pipeline loads and every diarization run are
  then reported (model origin, file duration, speaker counts, a per-process session id).
* ``huggingface_hub`` snapshots ``HF_HUB_DISABLE_TELEMETRY`` / ``DO_NOT_TRACK`` into module
  constants at import; they control ``send_telemetry`` and the library/torch versions it
  appends to the User-Agent of model downloads.
* ``deno`` (spawned by yt-dlp for YouTube challenges) inherits this environment and checks
  ``dl.deno.land`` for a newer release once a day unless ``DENO_NO_UPDATE_CHECK`` is set.

The images and compose files set the same values; this module is the backstop for any
process started some other way (a bare ``docker run``, a script, a custom deployment).
``setdefault`` keeps an operator's explicit choice. It must stay stdlib-only and be the FIRST
import of every process entry point (``app.main``, ``app.core.celery``) —
``tests/unit/test_telemetry_disabled.py`` enforces both.
"""

import os

TELEMETRY_OFF_DEFAULTS: dict[str, str] = {
    "PYANNOTE_METRICS_ENABLED": "false",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "DENO_NO_UPDATE_CHECK": "1",
}

for _name, _value in TELEMETRY_OFF_DEFAULTS.items():
    os.environ.setdefault(_name, _value)
