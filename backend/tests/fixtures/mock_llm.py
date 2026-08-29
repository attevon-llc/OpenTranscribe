"""Fixtures for the mock LLM provider (``scripts/mock-llm-server.py``).

Why a real server rather than patching ``LLMService``: a monkeypatched client
proves the test's own mock behaves, not that the app does. Pointing the real
``custom`` provider at a real HTTP server exercises the genuine path — payload
construction, SSE parsing, citation assembly, redaction masking, usage
recording and error frames — while only token generation is canned, which is
the one part that needs a GPU or an API key.

Two ways to get a server, tried in order:

1. The ``mock-llm`` container from ``docker-compose.mock-llm.yml``
   (``./opentr.sh start dev --with-mock-llm``). Preferred, because the BACKEND
   can reach it too — required for anything that drives the app over HTTP.
2. A subprocess on a free port, started per session. Enough for tests that call
   the server directly, and keeps the suite runnable with no stack at all.

Tests needing the backend to reach it must use :func:`mock_llm_base_url_for_backend`,
which skips when only the subprocess is available: ``localhost`` inside the
backend container is the container, not the host.

Scenario models (see the server for details):

==================  =======================================================
``mock-gpt``        normal reply with ``[1]``/``[2]`` citations and markdown
``mock-echo``       echoes the prompt it received — assert what the app SENT
``mock-empty``      completes with no content
``mock-error``      HTTP 500 before any token
``mock-slow``       stalls past the first-token watchdog
``mock-reasoning``  streams ``delta.reasoning_content`` before the answer
==================  =======================================================
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
import requests

CONTAINER_PORT = int(os.environ.get("MOCK_LLM_PORT", "5199"))
CONTAINER_HOSTNAME = "mock-llm"
SERVER = Path(__file__).resolve().parents[3] / "scripts" / "mock-llm-server.py"


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
def mock_llm_url() -> Iterator[str]:
    """Base URL of a running mock LLM, reachable from the TEST process.

    Reuses the compose container when it is up, otherwise starts a subprocess
    for the session. Never skips: one of the two always works.
    """
    if _reachable("127.0.0.1", CONTAINER_PORT):
        yield f"http://127.0.0.1:{CONTAINER_PORT}/v1"
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
            pytest.fail(f"mock LLM did not start on port {port}")
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def mock_llm_base_url_for_backend() -> str:
    """Base URL the BACKEND CONTAINER can reach, or skip.

    Deliberately not derived from :func:`mock_llm_url`: a subprocess on the host
    is invisible to the backend container, and a test that configured the app
    with a host ``localhost`` URL would fail confusingly rather than skip.
    """
    if not _reachable("127.0.0.1", CONTAINER_PORT):
        pytest.skip(
            "mock LLM container not running — start with './opentr.sh start dev --with-mock-llm'"
        )
    return f"http://{CONTAINER_HOSTNAME}:{CONTAINER_PORT}/v1"


@pytest.fixture
def mock_llm_completion(mock_llm_url: str):
    """Call the mock directly. Returns the parsed JSON response.

    Handy for asserting scenario behaviour without going through the app.
    """

    def _call(prompt: str, model: str = "mock-gpt", stream: bool = False) -> dict:
        response = requests.post(
            f"{mock_llm_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": stream,
            },
            timeout=60,
        )
        return {"status_code": response.status_code, "body": response.json()}

    return _call


@pytest.fixture
def register_mock_llm_provider(mock_llm_base_url_for_backend: str):
    """Configure the app's ``custom`` provider to point at the mock, then clean up.

    Takes an authenticated ``requests.Session`` so a test can drive the real API
    surface. Deletes the config afterwards so the dev database is left as found —
    the suite must never persist changes to dev data.
    """
    created: list[tuple] = []

    def _register(session, api_base: str, model: str = "mock-gpt", name: str = "Mock LLM (test)"):
        headers = {"X-CSRF-Token": session.cookies.get("csrf_token")}
        response = session.post(
            f"{api_base}/llm-settings",
            json={
                "name": name,
                "provider": "custom",
                "model_name": model,
                "base_url": mock_llm_base_url_for_backend,
                "api_key": "mock-key-not-secret",
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        config = response.json()
        created.append((session, api_base, config["uuid"], headers))
        return config

    yield _register

    for session, api_base, uuid, headers in created:
        # Cleanup must never fail an otherwise-passing test.
        with contextlib.suppress(Exception):
            session.delete(f"{api_base}/llm-settings/config/{uuid}", headers=headers, timeout=30)
