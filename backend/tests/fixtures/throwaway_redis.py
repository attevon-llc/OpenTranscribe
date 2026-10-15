"""A throwaway Redis for suites whose subject is Redis itself (#1246).

The model-download lock is a ``SET NX`` plus Lua compare-and-delete, which a mock cannot prove.
This starts an unauthenticated ``redis:7-alpine`` on a free loopback port - never the dev
stack's Redis, never its credentials or keys - once per xdist worker, and removes it afterwards.
Tests isolate themselves with a unique key namespace instead of flushing.
"""

from __future__ import annotations

import shutil
import socket
import time
import uuid

import pytest
import redis

from tests.docker_ops import remove_container
from tests.docker_ops import run_container

READY_TIMEOUT_SECONDS = 20


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def local_model_redis():
    """A client for a throwaway Redis container, valid for the whole worker session."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH - cannot start a throwaway Redis for this suite")
    port = _free_port()
    name = f"ot-test-redis-lm-{uuid.uuid4().hex[:10]}"
    client: redis.Redis | None = None
    try:
        # The try opens before run_container: a client-side timeout plus its retry can leave a
        # running container even though run_container raised, so teardown removes it by name.
        run_container(
            ["run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine"]
        )
        client = redis.Redis(host="127.0.0.1", port=port, db=0)
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                if client.ping():
                    break
            except redis.exceptions.ConnectionError:
                time.sleep(0.1)
        else:
            pytest.fail("throwaway redis container did not become ready in time")
        yield client
    finally:
        if client is not None:
            client.close()
        remove_container(name)


@pytest.fixture
def lm_namespace() -> str:
    """A key namespace no other test shares."""
    return f"test:lm:{uuid.uuid4().hex[:12]}"
