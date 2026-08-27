"""Fixtures for the mock cloud ASR provider (``scripts/mock-asr-server.py``).

Mirrors ``fixtures/mock_llm.py`` exactly, for the same reason: pointing the real
``gladia`` provider at a real HTTP server exercises the genuine path — upload
validation, request-body construction (diarization/language/vocabulary), the
processing->done poll cycle, and error handling — while only the transcript
content is canned, which is the one part that needs a paid vendor account.

Two ways to get a server, tried in order:

1. The ``mock-asr`` container from ``docker-compose.mock-asr.yml``
   (``./opentr.sh start dev --with-mock-asr``). Preferred, because the BACKEND
   can reach it too — required for anything that drives the app over HTTP.
2. A subprocess on a free port, started per session. Enough for tests that call
   the server directly, and keeps the suite runnable with no stack at all.

Tests needing the backend to reach it must use :func:`mock_asr_base_url_for_backend`,
which skips when only the subprocess is available: ``localhost`` inside the
backend container is the container, not the host.

⚠️ ``GladiaProvider._base`` resolves ``GLADIA_API_BASE_URL`` once, at
construction time, from the environment — never from a per-config ``base_url``
(that field is not wired to the real request path; see issue #594). So a test
that only calls :func:`register_mock_gladia_asr_config` has configured the
*app's* record of where to send requests, but the backend/celery process must
ALSO have ``GLADIA_API_BASE_URL`` pointed at the mock (``docker-compose.mock-asr.yml``
sets this on ``backend``/``celery-cloud-asr-worker`` when the stack is started
with ``--with-mock-asr``) for a real pipeline run to actually reach it.

Scenario models (see the server for details):

================  =========================================================
``ok``            normal job: processing -> done, canned 2-speaker transcript
``error``         job status becomes ``error`` with a sanitized-looking message
``malformed``     200 "done" but ``result.transcription`` key is missing
``upload-reject`` ``POST /v2/upload`` always 400s
================  =========================================================
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Never a bare literal — the `readiness-probe-target` audit detector requires
# the probe port to be derived from the env var, not a hardcoded constant.
CONTAINER_PORT = int(os.environ.get("MOCK_ASR_PORT", "5198"))
CONTAINER_HOSTNAME = "mock-asr"
SERVER = Path(__file__).resolve().parents[3] / "scripts" / "mock-asr-server.py"


def _reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_serving(port: int, deadline_s: float = 10.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if _reachable("127.0.0.1", port):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def mock_asr_url() -> Iterator[str]:
    """Base URL of a running mock ASR server, reachable from the TEST process.

    Reuses the compose container when it is up, otherwise starts a subprocess
    for the session. Never skips: one of the two always works.
    """
    if _reachable("127.0.0.1", CONTAINER_PORT):
        yield f"http://127.0.0.1:{CONTAINER_PORT}"
        return

    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SERVER), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_until_serving(port):
            proc.kill()
            pytest.fail(f"mock ASR server did not start on port {port}")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def mock_asr_base_url_for_backend() -> str:
    """Base URL the BACKEND CONTAINER can reach, or skip.

    Deliberately not derived from :func:`mock_asr_url`: a subprocess on the
    host is invisible to the backend container, and a test that configured the
    app with a host ``localhost`` URL would fail confusingly rather than skip.
    """
    if not _reachable("127.0.0.1", CONTAINER_PORT):
        pytest.skip(
            "mock ASR container not running — start with './opentr.sh start dev --with-mock-asr'"
        )
    return f"http://{CONTAINER_HOSTNAME}:{CONTAINER_PORT}"


@pytest.fixture
def register_mock_gladia_asr_config(mock_asr_base_url_for_backend: str):
    """Configure a real ``gladia`` ASR config pointed at the mock, then clean up.

    Takes an authenticated ``requests.Session`` so a test can drive the real API
    surface. Sets the config active, then deletes it AND clears the active-provider
    setting on teardown — the suite must never persist changes to dev data.
    """
    created: list[tuple] = []

    def _register(session, api_base: str, name: str = "Mock Gladia (test)"):
        headers = {"X-CSRF-Token": session.cookies.get("csrf_token")}
        response = session.post(
            f"{api_base}/asr-settings",
            json={
                "name": name,
                "provider": "gladia",
                "model_name": "default",
                "base_url": mock_asr_base_url_for_backend,
                "api_key": "mock-key-not-secret",
                "is_active": True,
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        config = response.json()
        session.post(
            f"{api_base}/asr-settings/set-active",
            json={"config_uuid": config["uuid"]},
            headers=headers,
            timeout=30,
        )
        created.append((session, api_base, config["uuid"], headers))
        return config

    yield _register

    for session, api_base, uuid_str, headers in created:
        # Cleanup must never fail an otherwise-passing test.
        with contextlib.suppress(Exception):
            session.post(f"{api_base}/asr-settings/clear-active", headers=headers, timeout=30)
        with contextlib.suppress(Exception):
            session.delete(
                f"{api_base}/asr-settings/config/{uuid_str}", headers=headers, timeout=30
            )
