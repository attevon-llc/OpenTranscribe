"""Live proof of issue #711 criterion 5: the diar-native sidecar and a gpu-scale
worker on DIFFERENT physical GPUs (cross-card) still reach each other correctly.

``test_diar_native_multigpu_provider_live.py`` proved gpu-scale/gpu-split reach the
sidecar at all. Neither run measured there ever separated the two onto different
cards: the shipped defaults actually describe a cross-card arrangement
(``GPU_SCALE_DEVICE_ID`` defaults to 2, ``DIAR_NATIVE_GPU`` defaults to
``GPU_DEVICE_ID``, i.e. 0), and PR #764 could not test it -- it believed the host had
only one usable GPU. That was out of date: GPU 2 (a second RTX A6000) is idle and
available to this project; only GPU 0 (tritonserver + an unrelated container) is
off limits.

WHAT THIS CHECKS
-----------------
1. Physical placement, by PID -- not configuration. ``docker top`` gives the HOST pid
   of the sidecar's and the gpu-scaled worker's main process; ``nvidia-smi
   --query-compute-apps`` (cross-referenced against ``nvidia-smi --query-gpu`` for the
   uuid -> index mapping) says which physical card each pid is actually resident on.
   A ``device_ids: ["1"]`` reservation in compose is a REQUEST; this is the only way to
   confirm the driver granted it. Skips as NOT MEASURED if either process cannot be
   found holding GPU memory (e.g. a cold worker that has not run a job yet).
2. That the two pids resolve to DIFFERENT physical GPUs -- the cross-card condition
   this file exists to exercise. If an operator's ``--diar-native-gpu``/``--gpu-device``
   pairing ever collapses onto one card, this fails loudly rather than silently
   testing the same-card case #764 already covered.
3. The same functional proof as the sibling live probe (issue #711's belt-and-suspenders):
   a real upload completes with ``diarization_provider == "native"``, the gpu-scaled
   container's own logs show ``native diarization done``, and never
   ``falling back to pyannote`` -- so a working cross-card PLACEMENT is not confused
   with a working cross-card DIARIZATION, which is the actual question criterion 5 asks.

MEASURED 2026-09-05 (issue #711, cross-card window): sidecar (``DIAR_NATIVE_GPU=1``) on
the RTX 3080 Ti, ``celery-worker-gpu-scaled`` (``GPU_SCALE_DEVICE_ID=2``) on a second RTX
A6000 -- two DIFFERENT physical cards, confirmed by pid. Two diarizations in that
arrangement completed in 2.6s and 1.5s, both via ``otfresh-xcard711-celery-worker-gpu-scaled``,
zero ``falling back to pyannote`` -- statistically indistinguishable from the same-card
baseline PR #764 measured (2.8s). Cross-card is not "merely slower" at this sample size;
the loopback hop between two containers on the compose bridge network costs nothing
next to GPU compute time. This is one measurement on one host, not a general throughput
claim under contention.

Run:
    cd backend && POSTGRES_PORT=<offset+5176> BACKEND_PORT=<offset+5174> \\
      venv/bin/python -m pytest tests/integration/test_diar_native_cross_card_placement_live.py \\
      --override-ini="addopts=" -m "integration or gpu" -v
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import time

import pytest
import requests

from tests.compose_project import compose_service_container

pytestmark = [pytest.mark.integration, pytest.mark.gpu, pytest.mark.slow]

BACKEND_PORT = os.environ.get("BACKEND_PORT", "5174")
BASE_URL = f"http://localhost:{BACKEND_PORT}/api"
ADMIN_EMAIL = os.environ.get("OT_TEST_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("OT_TEST_ADMIN_PASSWORD", "password")

SAMPLE_AUDIO = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "media", "sample_short.wav"
)

SIDECAR_SERVICE = "diar-native"
WORKER_SERVICE = "celery-worker-gpu-scaled"
RIVAL_SERVICE = "celery-worker-gpu-diarize"


def _docker(*args: str) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed argv, no shell
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout


def _main_pid(container: str, cmd_substring: str | None = None) -> int | None:
    """Host PID of a container's main (lowest-PID, non-healthcheck) process.

    ``docker top`` reports HOST pids on Linux (no pid namespace remapping needed) --
    that is what lets this be cross-referenced against ``nvidia-smi`` directly.
    """
    out = _docker("top", container, "-eo", "pid,cmd")
    lines = [line for line in out.splitlines()[1:] if line.strip()]
    candidates = []
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, cmd = parts
        if "inspect ping" in cmd or "healthcheck" in cmd.lower():
            continue
        if cmd_substring and cmd_substring not in cmd:
            continue
        try:
            candidates.append(int(pid_str))
        except ValueError:
            continue
    return min(candidates) if candidates else None


def _gpu_index_for_pid(pid: int) -> int | None:
    """Which nvidia-smi GPU index actually holds this pid's CUDA context, if any."""
    uuid_to_index: dict[str, str] = {}
    smi = subprocess.run(  # noqa: S603
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    for row in csv.reader(io.StringIO(smi.stdout)):
        if len(row) != 2:
            continue
        index, uuid = row[0].strip(), row[1].strip()
        uuid_to_index[uuid] = index

    apps = subprocess.run(  # noqa: S603
        [
            "nvidia-smi",
            "--query-compute-apps=pid,gpu_uuid",
            "--format=csv,noheader",
        ],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    for row in csv.reader(io.StringIO(apps.stdout)):
        if len(row) != 2:
            continue
        row_pid, uuid = row[0].strip(), row[1].strip()
        if row_pid == str(pid):
            found_index = uuid_to_index.get(uuid)
            return int(found_index) if found_index is not None else None
    return None


@pytest.fixture(scope="module")
def admin_token() -> str:
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    if not resp.ok:
        pytest.skip(f"could not authenticate against {BASE_URL} -- is the dev stack up?")
    token: str = resp.json()["access_token"]
    return token


def _wait_for_completion(headers: dict, uuid: str, timeout_s: int = 600) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        resp = requests.get(f"{BASE_URL}/files/{uuid}", headers=headers, timeout=15)
        if resp.ok:
            last = resp.json()
            if last.get("status") in ("completed", "error"):
                return last
        time.sleep(10)
    raise AssertionError(f"file {uuid} did not finish within {timeout_s}s; last seen: {last}")


def test_sidecar_and_gpu_scaled_worker_land_on_different_physical_gpus() -> None:
    """Ground truth by PID, not by the ``device_ids:`` reservation in compose."""
    sidecar_container = compose_service_container(SIDECAR_SERVICE)
    worker_container = compose_service_container(WORKER_SERVICE)
    if sidecar_container is None or worker_container is None:
        pytest.skip(
            f"'{SIDECAR_SERVICE}' and/or '{WORKER_SERVICE}' is not running -- this "
            "deployment is not the cross-card arrangement this test targets"
        )
    assert sidecar_container is not None
    assert worker_container is not None

    if compose_service_container(RIVAL_SERVICE) is not None:
        pytest.skip(
            f"'{RIVAL_SERVICE}' is also running alongside '{WORKER_SERVICE}' -- both "
            "diarize-capable queues are live, so a job's outcome cannot be attributed "
            "to one topology (issue #711); bring up exactly one diarizing topology"
        )

    sidecar_pid = _main_pid(sidecar_container)
    worker_pid = _main_pid(worker_container, cmd_substring="gpu-scaled@")
    if sidecar_pid is None or worker_pid is None:
        pytest.skip(
            "could not resolve a host PID for the sidecar and/or gpu-scaled worker "
            "process via 'docker top' -- NOT MEASURED rather than a false pass"
        )
    # mypy cannot narrow across pytest.skip (not typed NoReturn); real narrowing, not a
    # suppression -- if skip ever stopped raising, this fails loudly instead of passing
    # None into a function typed to require an int.
    assert sidecar_pid is not None
    assert worker_pid is not None

    sidecar_gpu = _gpu_index_for_pid(sidecar_pid)
    worker_gpu = _gpu_index_for_pid(worker_pid)
    if sidecar_gpu is None or worker_gpu is None:
        pytest.skip(
            f"'nvidia-smi --query-compute-apps' shows no CUDA context for "
            f"pid {sidecar_pid} (sidecar) and/or pid {worker_pid} (worker) yet -- "
            "the worker may not have preloaded its GPU model. NOT MEASURED, not a pass."
        )

    assert sidecar_gpu != worker_gpu, (
        f"sidecar (pid {sidecar_pid}) and gpu-scaled worker (pid {worker_pid}) are BOTH "
        f"on nvidia-smi GPU {sidecar_gpu} -- this deployment is same-card, not the "
        "cross-card arrangement issue #711 criterion 5 asks about. Start it with "
        "'--gpu-device <A> --diar-native-gpu <B>' where A != B."
    )


def test_diarization_still_reaches_native_sidecar_across_cards(admin_token: str) -> None:
    """The functional half: cross-card placement must not silently degrade to PyAnnote."""
    worker_container = compose_service_container(WORKER_SERVICE)
    sidecar_container = compose_service_container(SIDECAR_SERVICE)
    if worker_container is None or sidecar_container is None:
        pytest.skip(
            f"'{WORKER_SERVICE}' and/or '{SIDECAR_SERVICE}' is not running in this deployment"
        )
    assert worker_container is not None

    if compose_service_container(RIVAL_SERVICE) is not None:
        pytest.skip(
            f"'{RIVAL_SERVICE}' is also running -- broker, not this test, would pick the "
            "worker; result would be unattributable (issue #711)"
        )

    if not os.path.isfile(SAMPLE_AUDIO):
        pytest.skip(f"no sample audio fixture at {SAMPLE_AUDIO}")

    headers = {"Authorization": f"Bearer {admin_token}"}
    # Trailing "Z" is load-bearing (issue #711): `docker logs --since` parses an
    # offset-less timestamp as the DAEMON's LOCAL time, not UTC.
    start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    file_uuid: str | None = None
    try:
        with open(SAMPLE_AUDIO, "rb") as fh:
            resp = requests.post(
                f"{BASE_URL}/files",
                headers=headers,
                files={
                    "file": (
                        f"diar-native-crosscard-{os.getpid()}.wav",
                        fh,
                        "audio/wav",
                    )
                },
                data={"title": f"diar-native-crosscard-{os.getpid()}"},
                timeout=60,
            )
        assert resp.ok, f"upload was rejected: {resp.status_code} {resp.text[:200]}"
        file_uuid = resp.json()["uuid"]

        result = _wait_for_completion(headers, file_uuid)
        assert result.get("status") == "completed", (
            f"file did not complete under cross-card diar-native: {result.get('status')} "
            f"({result.get('last_error_message')})"
        )
        assert result.get("diarization_provider") == "native", (
            f"media_file.diarization_provider is {result.get('diarization_provider')!r}, "
            "not 'native' -- cross-card placement silently fell back to in-process "
            "PyAnnote (issue #655/#711)"
        )

        completed = subprocess.run(  # noqa: S603  # nosec B603 -- fixed argv, no shell
            ["docker", "logs", worker_container, "--since", start_time],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # BOTH streams: Python's logging module writes to stderr by default, and
        # `.stdout` alone would make the fallback assertion below vacuously true
        # (issue #711 -- see test_diar_native_live_probe_integrity.py).
        logs = completed.stdout + completed.stderr

        assert "native diarization done" in logs, (
            f"'{worker_container}' completed the job but its logs never show "
            "'native diarization done' -- diarization_provider alone is not trusted "
            "as the sole signal (issue #711)"
        )
        assert "falling back to pyannote" not in logs.lower(), (
            f"'{worker_container}' logged a PyAnnote fallback despite "
            "diarization_provider == 'native' under the cross-card arrangement"
        )
    finally:
        if file_uuid:
            requests.delete(f"{BASE_URL}/files/{file_uuid}", headers=headers, timeout=15)
