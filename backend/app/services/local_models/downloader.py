"""Download a catalog Whisper model from the Hugging Face hub, and measure how far it got.

Used by the runner child process (``runner.py``), by the supervising Celery task and by
``scripts/download-models.py``. Importing this module loads ``huggingface_hub`` only: no
database, no Celery, no torch and no faster-whisper, so it can run in a bare subprocess.

Progress is measured in bytes on disk. ``snapshot_download`` counts files, not bytes, and every
in-flight file is written to ``blobs/<etag>.incomplete`` before being renamed to ``blobs/<etag>``,
so ``bytes_on_disk`` can report the true position, including that of a resumed download. The blob
name is the file's etag: the sha256 for an LFS file, the git blob id for any other file.
"""

from __future__ import annotations

import errno
import fnmatch
from pathlib import Path
from typing import NamedTuple

from app.core.local_whisper_catalog import ALLOW_PATTERNS
from app.core.local_whisper_catalog import LocalWhisperModel
from app.services.local_models import cache

# One fixed sentence per outcome. The UI renders its own translated text from the code; the
# exception text (which can quote URLs and headers) never leaves the server log.
ERROR_MESSAGES: dict[str, str] = {
    "hf_auth_failed": "Hugging Face refused the request. Check the saved Hugging Face token.",
    "hf_repo_not_found": "The model was not found on Hugging Face.",
    "hf_unreachable": "Hugging Face could not be reached. Try again later.",
    "disk_full": "There is not enough free disk space for this model.",
    "cache_not_writable": (
        "The model cache is not writable. Run ./scripts/fix-model-permissions.sh on the host."
    ),
    "downloads_disabled_offline": "Downloads are disabled because this deployment is offline.",
    "download_timed_out": "The download took too long and was stopped.",
    "download_failed": "The download failed.",
    "insufficient_disk_space": "There is not enough free disk space for this model.",
    "unknown_local_model": "This model is not in the list of supported local models.",
}


class DownloadPlan(NamedTuple):
    """What a download will fetch, resolved before any file is downloaded.

    Attributes:
        repo_id: Hugging Face repository.
        revision: Commit sha the files are fetched at.
        expected: Blob name (etag) -> size, for every file the catalog downloads.
        bytes_total: Sum of ``expected``.
    """

    repo_id: str
    revision: str
    expected: dict[str, int]
    bytes_total: int


def resolve_plan(
    entry: LocalWhisperModel,
    token: str | None,
    *,
    timeout: float = 30.0,
    endpoint: str | None = None,
) -> DownloadPlan:
    """Ask the hub what files, sizes and commit a download of ``entry`` will fetch.

    Args:
        entry: Catalog model.
        token: Hugging Face token, or ``None`` for anonymous access.
        timeout: Seconds to wait for the hub.
        endpoint: Hub endpoint; ``None`` uses the library default (``HF_ENDPOINT``).

    Returns:
        The plan. Raises whatever ``huggingface_hub`` raises; pass it to ``classify_error``.
    """
    from huggingface_hub import HfApi

    info = HfApi(endpoint=endpoint).model_info(
        entry.repo_id, files_metadata=True, token=token or None, timeout=timeout
    )
    if not info.sha:
        raise ValueError("the hub returned no commit sha")
    expected: dict[str, int] = {}
    for sibling in info.siblings or []:
        if not any(fnmatch.fnmatch(sibling.rfilename, pat) for pat in ALLOW_PATTERNS):
            continue
        blob = sibling.lfs.sha256 if sibling.lfs else sibling.blob_id
        size = sibling.lfs.size if sibling.lfs else sibling.size
        if blob and size is not None:
            expected[blob] = int(size)
    return DownloadPlan(entry.repo_id, info.sha, expected, sum(expected.values()))


def bytes_on_disk(repo_id: str, expected: dict[str, int]) -> int:
    """Return how many bytes of ``expected`` are on disk, counting in-flight files.

    A completed blob counts in full, an ``.incomplete`` one by its current size; a file not
    started counts as zero. Capped at the total so a mis-sized blob cannot show over 100%.
    """
    blobs: Path = cache.repo_dir(repo_id) / "blobs"
    done = 0
    for name, size in expected.items():
        for candidate, full in ((blobs / name, True), (blobs / f"{name}.incomplete", False)):
            try:
                on_disk = candidate.stat().st_size
            except OSError:
                continue
            done += min(on_disk, size) if not full else size
            break
    return min(done, sum(expected.values()))


def download_blocking(
    entry: LocalWhisperModel,
    token: str | None,
    revision: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Download the catalog files of ``entry`` into the hub cache and return the snapshot path.

    Args:
        entry: Catalog model.
        token: Hugging Face token, or ``None``.
        revision: Commit sha to fetch at, so the bytes counted are the bytes fetched.
        endpoint: Hub endpoint; ``None`` uses the library default.
    """
    from huggingface_hub import snapshot_download

    snapshot = snapshot_download(
        entry.repo_id,
        revision=revision,
        allow_patterns=list(ALLOW_PATTERNS),
        token=token or None,
        endpoint=endpoint,
    )
    if revision and revision != "main":
        # snapshot_download writes refs/<branch> only when asked for a branch. faster-whisper
        # finds an offline model through refs/main, so a download pinned to a commit must set
        # it, and only now that every file is in place.
        _point_main_at(entry.repo_id, revision)
    return str(snapshot)


def _point_main_at(repo_id: str, revision: str) -> None:
    ref = cache.repo_dir(repo_id) / "refs" / "main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    tmp = ref.with_name("main.tmp")
    tmp.write_text(revision, encoding="utf-8")
    tmp.replace(ref)


def _status_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_error(exc: BaseException) -> str:
    """Map a download exception to one of the ``ERROR_MESSAGES`` codes.

    Args:
        exc: The exception raised by ``resolve_plan`` or ``download_blocking``.
    """
    import requests
    from huggingface_hub import errors as hf_errors

    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        chain.append(cur)
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    for err in chain:
        if isinstance(err, hf_errors.OfflineModeIsEnabled):
            return "downloads_disabled_offline"
    for err in chain:
        if isinstance(err, OSError) and err.errno == errno.ENOSPC:
            return "disk_full"
        if isinstance(err, OSError) and err.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "cache_not_writable"
    for err in chain:
        status = _status_of(err)
        if isinstance(err, hf_errors.GatedRepoError) or status in (401, 403):
            return "hf_auth_failed"
        if (
            isinstance(err, hf_errors.RepositoryNotFoundError | hf_errors.RevisionNotFoundError)
            or status == 404
        ):
            return "hf_repo_not_found"
    for err in chain:
        status = _status_of(err)
        if isinstance(
            err,
            requests.exceptions.ConnectionError
            | requests.exceptions.Timeout
            | hf_errors.LocalEntryNotFoundError,
        ) or (status is not None and status >= 500):
            return "hf_unreachable"
    return "download_failed"
