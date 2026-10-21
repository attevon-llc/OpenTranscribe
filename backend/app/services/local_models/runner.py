"""Child process that downloads one catalog model: ``python -m app.services.local_models.runner``.

A model download is a blocking ``huggingface_hub`` call with no cancel hook, so the supervising
task runs it in a child process it can stop. That child is this module. It imports nothing from
``app.db``, ``app.core.celery``, torch or faster-whisper, so it starts in about a second and
holds no database connection.

Contract with the supervisor:

* arguments: ``--model <catalog id or alias>`` and optionally ``--revision <sha>``;
* the Hugging Face token arrives in the ``HF_TOKEN`` environment variable, never in argv;
* standard output carries exactly one JSON line, ``{"ok": true}`` or
  ``{"ok": false, "code": "<code>"}``; standard error is for the supervisor's log file;
* exit status 0 on success, 1 on a download failure, 2 on a usage error.

Failures are logged by exception class and HTTP status only, never by message, so a token that
an upstream library echoes into an error cannot reach a log.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from app.core import local_whisper_catalog
from app.services.local_models import downloader

logger = logging.getLogger("local_model_runner")


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    """Run one download and report the outcome as a single JSON line.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(prog="local_models.runner")
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        _emit({"ok": False, "code": "download_failed"})
        return 2

    model_id = local_whisper_catalog.canonical_id(args.model)
    entry = local_whisper_catalog.get(model_id) if model_id else None
    if entry is None:
        _emit({"ok": False, "code": "unknown_local_model"})
        return 2

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    token = os.environ.get("HF_TOKEN") or None
    try:
        downloader.download_blocking(entry, token, args.revision)
    except Exception as exc:  # a child must always report, whatever the failure
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("download failed: %s status=%s", type(exc).__name__, status)
        _emit({"ok": False, "code": downloader.classify_error(exc)})
        return 1
    _emit({"ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
