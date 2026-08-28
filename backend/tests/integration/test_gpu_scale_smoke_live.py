"""Live-stack smoke check for `--gpu-scale` dev deployments (issue #590).

Converts ``scripts/gpu-scale-smoke.sh`` (full-test-matrix.md Stage 2 Cycle 2B) into a
real pytest test with falsifiable assertions -- the bash version had zero test
coverage of its own logic, and three real bugs (inline-.env-comment corruption,
an ARG_MAX crash, and a stale worker-hostname assumption) sat undetected in it for a
long time (see the script's own git history / issue #590's body for the full account).

WHAT THIS CHECKS AND WHY IT IS NOT "N WORKERS REGISTERED"
-----------------------------------------------------------
``docker-compose.gpu-scale.yml`` runs exactly ONE celery process, ``gpu-scaled@%h``,
with ``--pool=threads --concurrency=$GPU_SCALE_WORKERS``. Flower's ``/api/workers``
never shows N processes -- it shows one worker whose pool max-concurrency equals
``GPU_SCALE_WORKERS``. This checks the concurrency value on that one process, plus the
optional default worker (``gpu-transcription@%h``, NOT ``celery@%h`` -- that stale
assumption made the bash version's dual-GPU check fail unconditionally, even against a
correctly running deployment) when ``GPU_SCALE_DEFAULT_WORKER=1``.

Flower's ``/api/workers`` is a one-shot startup snapshot (issue #609), not a live
roster, so every read here passes ``?refresh=1`` and retries -- see
``backend/tests/unit/test_flower_worker_discovery.py`` for the mechanism.

The operator-facing CLI (``scripts/gpu-scale-smoke.sh``, still wired into
``scripts/test-matrix.sh`` Stage 2b) is UNCHANGED by this file. This test exists so the
same live-stack checks also run under pytest with real assertions.

Marked ``slow`` in addition to ``gpu``/``integration``: the concurrent-upload leg drives
a real transcription pipeline and can take several minutes.

Run:
    cd backend && PYTHONPATH=. pytest -m gpu tests/integration/test_gpu_scale_smoke_live.py -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests
from dotenv import dotenv_values

pytestmark = [pytest.mark.integration, pytest.mark.gpu, pytest.mark.slow]

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
_REPO_ROOT_ENV = os.path.join(_REPO_ROOT, ".env")


def _repo_env_value(key: str, default: str) -> str:
    """Read a value out of the repo-root .env via python-dotenv (issue #590) --
    never a hand-rolled grep/cut."""
    if os.environ.get(key):
        return os.environ[key]
    if os.path.isfile(_REPO_ROOT_ENV):
        value = dotenv_values(_REPO_ROOT_ENV).get(key)
        if value:
            return str(value)
    return default


GPU_SCALE_WORKERS = int(_repo_env_value("GPU_SCALE_WORKERS", "4"))
GPU_SCALE_DEFAULT_WORKER = _repo_env_value("GPU_SCALE_DEFAULT_WORKER", "0")
FLOWER_PORT = _repo_env_value("FLOWER_PORT", "5175")
FLOWER_URL_PREFIX = _repo_env_value("FLOWER_URL_PREFIX", "flower")
FLOWER_USER = _repo_env_value("FLOWER_USER", "admin")
FLOWER_PASSWORD = _repo_env_value("FLOWER_PASSWORD", "flower")
FLOWER_BASE = f"http://127.0.0.1:{FLOWER_PORT}/{FLOWER_URL_PREFIX}"
FLOWER_MAX_ENTRY_AGE = float(os.environ.get("FLOWER_MAX_ENTRY_AGE", "120"))
FLOWER_REFRESH_ATTEMPTS = int(os.environ.get("FLOWER_REFRESH_ATTEMPTS", "6"))
FLOWER_REFRESH_INTERVAL = float(os.environ.get("FLOWER_REFRESH_INTERVAL", "10"))

BACKEND_PORT = _repo_env_value("BACKEND_PORT", "5174")
BASE_URL = f"http://localhost:{BACKEND_PORT}/api"


def _flower_workers_refreshed() -> dict | None:
    """Fetch /api/workers?refresh=1, retrying because a refresh landing while the
    boot-time inspect is still in flight de-duplicates onto that stale task, and a
    threads-pool GPU worker mid preload/mid-transcription can hold the GIL long
    enough to miss even a widened reply deadline."""
    for _ in range(FLOWER_REFRESH_ATTEMPTS):
        try:
            resp = requests.get(
                f"{FLOWER_BASE}/api/workers",
                params={"refresh": 1},
                auth=(FLOWER_USER, FLOWER_PASSWORD),
                timeout=40,
            )
            if resp.ok:
                data: dict = resp.json()
                if any(name.startswith("gpu-scaled@") for name in data):
                    return data
        except requests.RequestException:
            pass
        time.sleep(FLOWER_REFRESH_INTERVAL)
    # One last attempt's data (possibly without a gpu-scaled@ entry) beats none,
    # so the failing assertion can name what WAS registered.
    try:
        resp = requests.get(
            f"{FLOWER_BASE}/api/workers",
            params={"refresh": 1},
            auth=(FLOWER_USER, FLOWER_PASSWORD),
            timeout=40,
        )
        last_data: dict | None = resp.json() if resp.ok else None
        return last_data
    except requests.RequestException:
        return None


def _worker_pool_concurrency_and_freshness(
    data: dict, prefix: str
) -> tuple[int | None, bool | None]:
    """Mirrors gpu-scale-smoke.sh's IFS='|' parser (also unit-tested in isolation by
    test_gpu_scale_smoke_worker_parsing.py against the SHELL script's own snippet) --
    this is the equivalent real assertion, exercised via a real HTTP call instead."""
    now = time.time()
    for name, info in data.items():
        if not name.startswith(prefix):
            continue
        stats = info.get("stats", {}) if isinstance(info, dict) else {}
        pool = stats.get("pool", {}) if isinstance(stats, dict) else {}
        age = now - float(info.get("timestamp", 0) or 0)
        fresh = age <= FLOWER_MAX_ENTRY_AGE
        concurrency = pool.get("max-concurrency")
        if concurrency in (None, ""):
            return None, fresh
        # `data` is untyped JSON (dict[Any, Any]); the `in (None, "")` check above already
        # rules out both non-int-convertible cases at runtime, so int() here is safe --
        # mypy just can't narrow a dict[Any, Any].get() result through a tuple-membership test.
        return int(concurrency), fresh  # type: ignore[arg-type]
    return None, None


def _flower_port_open() -> bool:
    """TCP probe, same shape as root conftest's `_service_reachable` -- a plain
    connect/close, no HTTP round trip and no broad except to mask a real failure."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        return sock.connect_ex(("127.0.0.1", int(FLOWER_PORT))) == 0


@pytest.fixture(scope="module")
def flower_workers() -> dict:
    if not _flower_port_open():
        pytest.skip(f"Flower not reachable at {FLOWER_BASE} -- is --gpu-scale up?")

    data = _flower_workers_refreshed()
    if not data:
        pytest.skip(f"Flower returned no worker data at {FLOWER_BASE}")
        raise AssertionError("unreachable")  # pytest.skip is NoReturn at runtime
    return data


def test_gpu_scaled_worker_is_registered_with_expected_concurrency(flower_workers: dict) -> None:
    concurrency, fresh = _worker_pool_concurrency_and_freshness(flower_workers, "gpu-scaled@")

    assert concurrency is not None or fresh is not None, (
        f"no gpu-scaled@* worker registered in Flower. Known workers: "
        f"{sorted(flower_workers) or '(none)'}. Cross-check with "
        "`./opentr.sh shell celery-worker -> celery -A app.core.celery inspect ping`."
    )
    assert fresh, (
        "gpu-scaled@* worker's Flower entry is stale "
        f"(older than {FLOWER_MAX_ENTRY_AGE}s) -- not proof the worker is alive now (issue #609)"
    )
    assert concurrency is not None, (
        "gpu-scaled@* is registered but reported no stats -- its inspect broadcast timed out"
    )
    assert concurrency == GPU_SCALE_WORKERS, (
        f"gpu-scaled worker pool concurrency is {concurrency}, "
        f"expected GPU_SCALE_WORKERS={GPU_SCALE_WORKERS}"
    )


@pytest.mark.skipif(
    GPU_SCALE_DEFAULT_WORKER != "1", reason="GPU_SCALE_DEFAULT_WORKER != 1 (single-GPU mode)"
)
def test_default_worker_is_registered_in_dual_gpu_mode(flower_workers: dict) -> None:
    """The default single-GPU worker's real Celery hostname is `gpu-transcription@%h`
    (docker-compose.yml's celery-worker service) -- it has never been `celery@%h`;
    that stale assumption made the bash script's version of this check fail
    unconditionally, even against a correctly running deployment."""
    concurrency, fresh = _worker_pool_concurrency_and_freshness(
        flower_workers, "gpu-transcription@"
    )

    assert concurrency is not None or fresh is not None, (
        "GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode) but no gpu-transcription@* "
        "default worker is registered"
    )
    assert fresh, (
        "GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode) but the gpu-transcription@* entry "
        f"in Flower is stale (older than {FLOWER_MAX_ENTRY_AGE}s, issue #609)"
    )


@pytest.fixture
def admin_token() -> str:
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        data={"username": "admin@example.com", "password": "password"},
        timeout=15,
    )
    if not resp.ok:
        pytest.skip(f"could not authenticate against {BASE_URL} -- is the dev stack up?")
    token: str = resp.json()["access_token"]
    return token


def test_concurrent_uploads_reach_completed_with_no_gpu_oom(admin_token: str) -> None:
    """Drives real concurrency: N uploads dispatched together must all reach
    `completed`, and the GPU worker's logs must show no CUDA OOM during the run.

    Not `e2e/fixtures/sample_audio.wav` -- that fixture is a deliberately silent
    440 Hz sine tone built for UI/upload-flow tests that never touch ASR; fed through
    the real pipeline it produces zero segments and lands in status=error, never
    completed.
    """
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker not available -- cannot inspect the GPU worker container")

    gpu_container_names = (
        subprocess.run(
            ["docker", "ps", "--filter", "name=celery-worker-gpu-scaled", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        .stdout.strip()
        .splitlines()
    )
    if not gpu_container_names:
        pytest.skip("no running celery-worker-gpu-scaled container")
    gpu_container: str = gpu_container_names[0]

    sample_audio = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "media", "sample_short.wav"
    )
    if not os.path.isfile(sample_audio):
        pytest.skip(f"no sample audio fixture at {sample_audio}")

    n_uploads = max(GPU_SCALE_WORKERS, 3)
    headers = {"Authorization": f"Bearer {admin_token}"}
    start_time = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    file_uuids: list[str] = []
    try:
        for i in range(n_uploads):
            with open(sample_audio, "rb") as fh:
                resp = requests.post(
                    f"{BASE_URL}/files",
                    headers=headers,
                    files={"file": (f"gpu-scale-smoke-{i}-{os.getpid()}.wav", fh, "audio/wav")},
                    data={"title": f"gpu-scale-smoke-{i}-{os.getpid()}"},
                    timeout=60,
                )
            assert resp.ok, f"upload {i} was rejected: {resp.status_code} {resp.text[:200]}"
            file_uuids.append(resp.json()["uuid"])

        assert len(file_uuids) == n_uploads, (
            f"only {len(file_uuids)} of {n_uploads} uploads were accepted"
        )

        deadline = time.time() + 900
        completed: set[str] = set()
        while time.time() < deadline:
            for uuid in file_uuids:
                if uuid in completed:
                    continue
                resp = requests.get(f"{BASE_URL}/files/{uuid}", headers=headers, timeout=15)
                if resp.ok and resp.json().get("status") == "completed":
                    completed.add(uuid)
            if len(completed) == len(file_uuids):
                break
            time.sleep(10)

        assert len(completed) == len(file_uuids), (
            f"{len(completed)} of {len(file_uuids)} concurrent uploads reached "
            "completed within 15 min"
        )

        logs = subprocess.run(
            ["docker", "logs", gpu_container, "--since", start_time],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.lower()
        oom_hits = logs.count("cuda out of memory") + logs.count("cuda error: out of memory")
        assert oom_hits == 0, (
            f"{oom_hits} CUDA OOM occurrence(s) in {gpu_container} logs during the "
            "concurrent-upload run"
        )
    finally:
        for uuid in file_uuids:
            requests.delete(f"{BASE_URL}/files/{uuid}", headers=headers, timeout=15)
