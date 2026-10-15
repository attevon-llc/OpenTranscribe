"""The model downloader against a mock Hugging Face hub (#1246).

The real ``huggingface_hub`` client runs against ``fixtures.mock_hf_hub`` on a loopback port.
Every catalog id is a real repository id, so the only thing between these tests and
huggingface.co is the endpoint: an autouse fixture points the library default at a closed
loopback port, so a call that forgets to pass the mock endpoint fails to connect instead of
downloading a model.
"""

from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fixtures.mock_hf_hub import COMMIT
from fixtures.mock_hf_hub import default_repo
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.errors import LocalEntryNotFoundError

from app.core import local_whisper_catalog as catalog
from app.services.local_models import cache
from app.services.local_models import downloader

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _entry(model_id: str) -> catalog.LocalWhisperModel:
    entry = catalog.get(model_id)
    assert entry is not None
    return entry


TINY = _entry("tiny")
CLOSED_PORT_ENDPOINT = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _never_reach_the_real_hub(monkeypatch, hf_cache):
    """Refuse any socket or DNS lookup that is not loopback; turn off Xet and use small chunks.

    Every catalog id is a real repository id, so a call that forgets to pass the mock endpoint
    would otherwise talk to huggingface.co. Name resolution is blocked too, so such a call fails
    before it sends a byte. (Patching ``constants.ENDPOINT`` instead does not work: the library
    builds file URLs from a template computed at import and only substitutes ``endpoint=`` when
    the URL starts with the current ``ENDPOINT``, so a patched value silently sends the request
    to the real hub.)
    """
    from huggingface_hub import constants

    loopback = {"127.0.0.1", "::1", "localhost"}
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in loopback:
            raise OSError(f"test blocked a lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and address[0] not in loopback:
            raise OSError(f"test blocked a connection to {address[0]!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_XET", True)
    monkeypatch.setattr(constants, "DOWNLOAD_CHUNK_SIZE", 64 * 1024)


def test_the_guard_refuses_the_real_hub():
    with pytest.raises(OSError, match="blocked"):
        socket.getaddrinfo("huggingface.co", 443)


@pytest.fixture
def hub(mock_hf_hub):
    assert mock_hf_hub.url.startswith("http://127.0.0.1:")
    mock_hf_hub.add_repo(default_repo(TINY.repo_id))
    return mock_hf_hub


def test_resolve_plan_matches_the_hubs_files_and_skips_unlisted_ones(hub):
    repo = hub.repos[TINY.repo_id]

    plan = downloader.resolve_plan(TINY, None, endpoint=hub.url)

    wanted = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json"]
    wanted.append("vocabulary.txt")
    assert plan.revision == COMMIT
    assert plan.repo_id == TINY.repo_id
    assert plan.expected == {repo.blob_name(f): len(repo.files[f]) for f in wanted}
    assert plan.bytes_total == sum(len(repo.files[f]) for f in wanted)
    # An LFS file is named by its sha256, anything else by its git blob id.
    assert repo.blob_name("model.bin") in plan.expected
    assert len(repo.blob_name("model.bin")) == 64
    assert len(repo.blob_name("config.json")) == 40


def test_download_produces_a_layout_the_cache_calls_complete(hub):
    plan = downloader.resolve_plan(TINY, None, endpoint=hub.url)

    snapshot = downloader.download_blocking(TINY, None, plan.revision, endpoint=hub.url)

    assert cache.is_complete(TINY.repo_id) is True
    assert (Path(snapshot) / "model.bin").stat().st_size == 2 * 1024 * 1024
    assert not (Path(snapshot) / "README.md").exists()
    assert downloader.bytes_on_disk(TINY.repo_id, plan.expected) == plan.bytes_total
    assert cache.has_partial(TINY.repo_id) is False


def test_a_download_pinned_to_a_commit_still_points_refs_main_at_it(hub):
    """faster-whisper resolves an offline model through refs/main, so it must exist."""
    fw_utils = pytest.importorskip("faster_whisper.utils")
    plan = downloader.resolve_plan(TINY, None, endpoint=hub.url)
    snapshot = downloader.download_blocking(TINY, None, plan.revision, endpoint=hub.url)

    ref = cache.repo_dir(TINY.repo_id) / "refs" / "main"
    assert ref.read_text(encoding="utf-8") == COMMIT
    requests_before = len(hub.requests)
    found = fw_utils.download_model(
        "tiny", local_files_only=True, cache_dir=str(cache.hub_cache_dir())
    )
    assert Path(found) == Path(snapshot)
    assert len(hub.requests) == requests_before


def test_bytes_on_disk_counts_a_partial_file_while_it_downloads(hub):
    hub.mode = "slow"
    plan = downloader.resolve_plan(TINY, None, endpoint=hub.url)
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(downloader.download_blocking(TINY, None, plan.revision, hub.url))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    mid_download = 0
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            done = downloader.bytes_on_disk(TINY.repo_id, plan.expected)
            if 0 < done < plan.bytes_total and cache.has_partial(TINY.repo_id):
                mid_download = done
                break
            time.sleep(0.02)
    finally:
        hub.release.set()
        worker.join(timeout=30)

    assert mid_download > 0, "never observed a partial download"
    assert outcome and isinstance(outcome[0], str), outcome
    assert downloader.bytes_on_disk(TINY.repo_id, plan.expected) == plan.bytes_total


def test_a_completed_blob_counts_in_full_and_an_incomplete_one_by_its_size(hf_cache):
    expected = {"done": 100, "partial": 400, "absent": 50}
    blobs = hf_cache / f"models--{TINY.repo_id.replace('/', '--')}" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "done").write_bytes(b"x" * 100)
    (blobs / "partial.incomplete").write_bytes(b"x" * 150)

    assert downloader.bytes_on_disk(TINY.repo_id, expected) == 250


def test_bytes_on_disk_never_exceeds_the_total(hf_cache):
    blobs = hf_cache / f"models--{TINY.repo_id.replace('/', '--')}" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "partial.incomplete").write_bytes(b"x" * 900)

    assert downloader.bytes_on_disk(TINY.repo_id, {"partial": 400}) == 400


def test_a_second_download_resumes_from_the_incomplete_file(hub):
    plan = downloader.resolve_plan(TINY, None, endpoint=hub.url)
    downloader.download_blocking(TINY, None, plan.revision, endpoint=hub.url)
    repo = hub.repos[TINY.repo_id]
    model_blob = repo.blob_name("model.bin")
    blobs = cache.repo_dir(TINY.repo_id) / "blobs"
    half = len(repo.files["model.bin"]) // 2
    (blobs / f"{model_blob}.incomplete").write_bytes(repo.files["model.bin"][:half])
    (blobs / model_blob).unlink()
    hub.requests.clear()

    downloader.download_blocking(TINY, None, plan.revision, endpoint=hub.url)

    assert (blobs / model_blob).read_bytes() == repo.files["model.bin"]
    assert cache.is_complete(TINY.repo_id) is True


@pytest.mark.parametrize(
    ("mode", "code"),
    [("401", "hf_auth_failed"), ("404", "hf_repo_not_found"), ("500", "hf_unreachable")],
)
def test_hub_failures_are_classified(hub, mode, code):
    hub.mode = mode

    with pytest.raises((HfHubHTTPError, LocalEntryNotFoundError)) as caught:
        downloader.download_blocking(TINY, None, None, endpoint=hub.url)

    assert downloader.classify_error(caught.value) == code


def test_an_unreachable_hub_is_classified():
    with pytest.raises(LocalEntryNotFoundError) as caught:
        downloader.download_blocking(TINY, None, None, endpoint=CLOSED_PORT_ENDPOINT)

    assert downloader.classify_error(caught.value) == "hf_unreachable"


def test_offline_mode_is_classified(hub, monkeypatch):
    from huggingface_hub import constants
    from huggingface_hub.utils import reset_sessions

    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True)
    # The offline adapter is mounted when a session is created, and sessions are cached.
    reset_sessions()
    try:
        with pytest.raises(LocalEntryNotFoundError) as caught:
            downloader.download_blocking(TINY, None, None, endpoint=hub.url)
    finally:
        monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
        reset_sessions()

    assert downloader.classify_error(caught.value) == "downloads_disabled_offline"
    assert hub.requests == []


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (OSError(errno.ENOSPC, "no space"), "disk_full"),
        (OSError(errno.EACCES, "denied"), "cache_not_writable"),
        (OSError(errno.EPERM, "not permitted"), "cache_not_writable"),
        (RuntimeError("anything else"), "download_failed"),
    ],
)
def test_local_failures_are_classified(exc, code):
    assert downloader.classify_error(exc) == code


def test_a_disk_error_wrapped_by_a_library_is_still_a_disk_error():
    try:
        try:
            raise OSError(errno.ENOSPC, "no space")
        except OSError as inner:
            raise RuntimeError("write failed") from inner
    except RuntimeError as outer:
        assert downloader.classify_error(outer) == "disk_full"


def test_every_code_has_one_fixed_sentence():
    for code in (
        "hf_auth_failed",
        "hf_repo_not_found",
        "hf_unreachable",
        "disk_full",
        "cache_not_writable",
        "downloads_disabled_offline",
        "download_timed_out",
        "download_failed",
    ):
        message = downloader.ERROR_MESSAGES[code]
        assert message.endswith(".")
        assert "Traceback" not in message


def test_the_token_is_sent_as_a_bearer_credential_only_when_given(hub):
    downloader.resolve_plan(TINY, None, endpoint=hub.url)
    assert set(hub.authorizations()) == {None}

    hub.requests.clear()
    downloader.resolve_plan(TINY, "hf_mock_token_value", endpoint=hub.url)
    assert set(hub.authorizations()) == {"Bearer hf_mock_token_value"}


# --- the runner child, as a real process -----------------------------------------------


def _run_runner(hub, cache_dir: Path, *args: str, token: str | None = None):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cache_dir),
        "PYTHONPATH": str(BACKEND_DIR),
        "HF_ENDPOINT": hub.url,
        "HF_HUB_CACHE": str(cache_dir),
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    if token:
        env["HF_TOKEN"] = token
    cmd = [sys.executable, "-m", "app.services.local_models.runner", *args]
    done = subprocess.run(
        cmd, env=env, cwd=BACKEND_DIR, capture_output=True, text=True, timeout=120, check=False
    )
    return cmd, done


def test_the_runner_downloads_and_reports_one_json_line(hub, hf_cache):
    token = "hf_runner_secret_token"

    cmd, done = _run_runner(hub, hf_cache, "--model", "tiny", token=token)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"ok": True}
    assert len(done.stdout.strip().splitlines()) == 1
    assert cache.is_complete(TINY.repo_id) is True
    assert "Bearer " + token in hub.authorizations()
    assert token not in " ".join(cmd)
    assert token not in done.stdout + done.stderr


def test_the_runner_accepts_a_catalog_alias(hub, hf_cache):
    crisper = _entry("nyrahealth/faster_CrisperWhisper")
    hub.add_repo(default_repo(crisper.repo_id))

    _cmd, done = _run_runner(hub, hf_cache, "--model", "crisperwhisper")

    assert done.returncode == 0, done.stderr
    assert cache.is_complete(crisper.repo_id) is True


def test_the_runner_refuses_a_model_outside_the_catalog(hub, hf_cache):
    _cmd, done = _run_runner(hub, hf_cache, "--model", "someone/else-ct2")

    assert done.returncode == 2
    assert json.loads(done.stdout) == {"ok": False, "code": "unknown_local_model"}
    assert hub.requests == []


def test_the_runner_reports_a_classified_failure_without_leaking_the_token(hub, hf_cache):
    hub.mode = "401"
    token = "hf_runner_secret_token"

    _cmd, done = _run_runner(hub, hf_cache, "--model", "tiny", token=token)

    assert done.returncode == 1
    assert json.loads(done.stdout) == {"ok": False, "code": "hf_auth_failed"}
    assert token not in done.stdout + done.stderr
