"""Run one model download in a child process, report its progress, and stop it on request (#1246).

``huggingface_hub.snapshot_download`` is a blocking call with no cancel hook, so the download runs
in ``runner.py`` as a child process this module supervises. Cancelling terminates that child.
That is a task managing the CPU-only, network-only Python process it spawned itself; it is not
the operator-level "never signal a CUDA process" rule in the root ``CLAUDE.md``, which is about
agents killing GPU workers. Nothing here ever signals anything it did not start.

The supervisor imports no Celery, WebSocket or database code: the caller injects ``notify``, so
it is testable with a real child process and a real Redis.

Two facts the supervisor must not get wrong:

* ``snapshot_download`` on a branch writes ``refs/main`` *before* it downloads, so
  ``cache.is_complete`` can be true while files are missing. The supervisor reports success only
  after every expected blob is on disk at its expected size.
* The child's stderr goes to a temp file, never a pipe: a progress bar writing to an unread pipe
  fills its buffer and blocks the child forever.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.local_whisper_catalog import LocalWhisperModel
from app.services.local_models import cache
from app.services.local_models import downloader
from app.services.local_models.state import DownloadStore

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[3]
#: The soft time limit of a download; the Celery limits on the task sit just above it.
DOWNLOAD_DEADLINE_SECONDS = 4 * 3600
#: Free space kept beyond the bytes still to fetch.
DISK_HEADROOM_BYTES = 512 * 1024 * 1024
TERMINATE_GRACE_SECONDS = 15.0

Notify = Callable[[dict[str, Any]], None]


def child_env(token: str | None, endpoint: str | None) -> dict[str, str]:
    """Build the child's environment. The token travels here and never in argv or a log."""
    env = dict(os.environ)
    env.pop("HF_TOKEN", None)
    if token:
        env["HF_TOKEN"] = token
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    # The child must read the cache this process reads, whatever the image's HF_HOME says.
    env["HF_HUB_CACHE"] = str(cache.hub_cache_dir())
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    elif not env.get("HF_ENDPOINT", "").strip():
        # huggingface_hub reads HF_ENDPOINT with a default, so a key that is set but empty
        # resolves to "" and breaks every request. Unset beats blank.
        env.pop("HF_ENDPOINT", None)
    return env


def _stop(proc: subprocess.Popen) -> None:
    """Terminate our own child; kill it only if it ignored the polite request."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def _blobs_complete(plan: downloader.DownloadPlan) -> bool:
    """True when every expected blob exists at its expected size."""
    blobs = cache.repo_dir(plan.repo_id) / "blobs"
    for name, size in plan.expected.items():
        try:
            if (blobs / name).stat().st_size != size:
                return False
        except OSError:
            return False
    return True


def _read_outcome(proc: subprocess.Popen) -> str | None:
    """Return the failure code the child reported, or None when it reported success."""
    raw = (proc.stdout.read() if proc.stdout else b"") or b""
    try:
        payload = json.loads(raw.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "download_failed"
    if payload.get("ok") is True and proc.returncode == 0:
        return None
    code = payload.get("code")
    return code if code in downloader.ERROR_MESSAGES else "download_failed"


class _Progress:
    """Writes state and notifies, so the supervisor's loop reads as the download's story."""

    def __init__(self, store: DownloadStore, notify: Notify, entry: LocalWhisperModel, ids: dict):
        self._store, self._notify, self._ids = store, notify, ids
        self._model_id = entry.id
        self._last: tuple | None = None

    def publish(
        self,
        status: str,
        done: int = 0,
        total: int | None = None,
        error_code: str | None = None,
        force: bool = False,
    ) -> None:
        percent = round(100 * done / total, 1) if total else None
        key = (status, done, error_code)
        if key == self._last and not force:
            return
        self._last = key
        fields = {
            "model_id": self._model_id,
            "status": status,
            "bytes_done": done,
            "bytes_total": total,
            "percent": percent,
            "error_code": error_code,
            **self._ids,
        }
        try:
            self._store.write_state(**fields)
            self._notify({k: v for k, v in fields.items() if k not in self._ids})
        except Exception as exc:  # progress is a display; it must never fail the download
            logger.warning("Could not publish download progress: %s", type(exc).__name__)


def run_download(
    entry: LocalWhisperModel,
    *,
    task_id: str,
    user_id: int,
    store: DownloadStore,
    token: str | None,
    notify: Notify,
    endpoint: str | None = None,
    poll_seconds: float = 1.0,
    deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
) -> str:
    """Download ``entry`` and return the final status: completed, failed or cancelled.

    The lock is released on every exit. A failed run's ``error_code`` is one of
    ``downloader.ERROR_MESSAGES``.

    Args:
        entry: Catalog model.
        task_id: Celery task id; owns the lock and the cancel flag.
        user_id: The administrator who started it.
        store: Lock, state and cancel flag.
        token: Hugging Face token or None.
        notify: Called with a payload on each change of progress.
        endpoint: Hub endpoint override (tests); None uses the library default.
        poll_seconds: Seconds between progress ticks.
        deadline_seconds: Give up after this long with ``download_timed_out``.
    """
    progress = _Progress(store, notify, entry, {"task_id": task_id, "user_id": user_id})
    try:
        return _run(
            entry,
            task_id,
            user_id,
            store,
            token,
            progress,
            endpoint,
            poll_seconds,
            deadline_seconds,
        )
    finally:
        store.release(task_id)


def _fail(progress: _Progress, code: str, done: int = 0, total: int | None = None) -> str:
    logger.warning("Model download failed: %s", code)
    progress.publish("failed", done, total, error_code=code, force=True)
    return "failed"


def _resolve_plan(entry, token, endpoint, progress) -> downloader.DownloadPlan | None:
    """Ask the hub for the plan; on failure publish the classified code and return None."""
    try:
        return downloader.resolve_plan(entry, token, endpoint=endpoint)
    except Exception as exc:
        logger.warning("Model plan failed: %s", type(exc).__name__)
        _fail(progress, downloader.classify_error(exc))
    return None


def _run(entry, task_id, user_id, store, token, progress, endpoint, poll_seconds, deadline) -> str:
    if store.cancel_requested(task_id):
        progress.publish("cancelled", force=True)
        return "cancelled"
    if not store.adopt(entry.id, task_id, user_id):
        # Another download holds the lock (this one waited so long its own lock expired).
        return _fail(progress, "model_download_in_progress")
    from app.utils.hf_hub_offline import hf_offline_requested

    if hf_offline_requested():
        return _fail(progress, "downloads_disabled_offline")
    if not cache.cache_writable():
        return _fail(progress, "cache_not_writable")
    plan = _resolve_plan(entry, token, endpoint, progress)
    if plan is None:
        return "failed"
    done = downloader.bytes_on_disk(plan.repo_id, plan.expected)
    if cache.free_bytes() < plan.bytes_total - done + DISK_HEADROOM_BYTES:
        return _fail(progress, "insufficient_disk_space", done, plan.bytes_total)
    return _supervise(
        entry, plan, task_id, store, token, progress, endpoint, poll_seconds, deadline
    )


def _supervise(
    entry, plan, task_id, store, token, progress, endpoint, poll_seconds, deadline
) -> str:
    cmd = [sys.executable, "-m", "app.services.local_models.runner", "--model", entry.id]
    cmd += ["--revision", plan.revision]
    started = time.monotonic()
    total = plan.bytes_total
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argv built from the catalog, no shell
            cmd,
            env=child_env(token, endpoint),
            cwd=BACKEND_DIR,
            stdout=subprocess.PIPE,
            stderr=err,
        )
        try:
            while proc.poll() is None:
                done = downloader.bytes_on_disk(plan.repo_id, plan.expected)
                if store.cancel_requested(task_id):
                    _stop(proc)
                    cache.remove_incomplete(plan.repo_id, plan.expected)
                    progress.publish("cancelled", done, total, force=True)
                    return "cancelled"
                if time.monotonic() - started > deadline:
                    _stop(proc)
                    return _fail(progress, "download_timed_out", done, total)
                store.refresh(task_id)
                progress.publish("downloading", done, total)
                time.sleep(poll_seconds)
        finally:
            _stop(proc)
        failure = _read_outcome(proc)
        err.seek(0)
        tail = err.read().decode("utf-8", "replace").splitlines()[-20:]
    if failure:
        if tail:
            logger.warning("Download child stderr: %s", _scrub(" | ".join(tail), token))
        if failure == "disk_full":
            cache.remove_incomplete(plan.repo_id)
        return _fail(
            progress, failure, downloader.bytes_on_disk(plan.repo_id, plan.expected), total
        )
    progress.publish("verifying", total, total)
    if not (_blobs_complete(plan) and cache.is_complete(plan.repo_id)):
        return _fail(
            progress,
            "download_failed",
            downloader.bytes_on_disk(plan.repo_id, plan.expected),
            total,
        )
    progress.publish("completed", total, total, force=True)
    return "completed"
