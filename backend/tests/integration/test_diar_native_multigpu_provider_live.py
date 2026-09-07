"""Live proof that gpu-scale and gpu-split actually reach the diar-native sidecar
(issue #711).

``test_diar_native_smoke_live.py`` proves the sidecar itself holds GPU memory --
that is GPU *residency*, not *reachability*. ``test_diar_native_overlay_wiring.py``
proves the compose YAML *says* every diarize-capable worker gets
``DIAR_NATIVE_URL`` -- that is static, over text nobody has to run. Neither can
tell you whether a real job dispatched to ``celery-worker-gpu-scaled`` (--gpu-scale)
or ``celery-worker-gpu-diarize`` (--with-gpu-split) actually used the sidecar, because
the PyAnnote fallback is silent by design (issue #655): a worker that cannot reach
diar-native produces a correct transcript with correct speaker labels and only a
WARNING log line -- "the job completed" is not evidence.

WHAT THIS CHECKS
-----------------
For whichever of the two topologies is actually running (each is independently
skippable -- this file does not require both up at once):

1. Uploads a real recording and waits for it to reach ``completed``.
2. Asserts ``media_file.diarization_provider == "native"`` on the resulting row
   (issue #706's column -- cheap, repeatable, no log parsing needed for this half).
3. ALSO greps the *specific* diarizing container's own logs for
   ``native diarization done`` and asserts the fallback line
   (``falling back to PyAnnote``) is ABSENT -- belt and suspenders, because a
   provider column that itself had a bug could otherwise mark a PyAnnote run as
   native and this test would not notice from (2) alone.
4. Cleans up the file it created (dev-data-safety rule) in a ``finally``.

MEASUREMENT STATUS (2026-09-05, issue #711) -- read before quoting this file as proof:

* ``--gpu-scale``: **VERIFIED on real hardware.** ``otfresh-gpu711-celery-worker-gpu-scaled``
  logged ``native diarization done in 2.8s: 4 segments, 1 speakers`` at 22:09:48Z with
  ``diarization_provider == "native"`` on the row and ZERO ``falling back to pyannote``
  lines in that container.
* ``--with-gpu-split``: **VERIFIED on real hardware**, in an earlier uncontended window.
  ``otfresh-gpu711-celery-worker-gpu-diarize`` logged ``native diarization done in 3.3s:
  4 segments, 1 speakers`` at 21:44:31Z with ``diarization_provider == "native"``, while
  ``celery-worker-gpu-transcribe`` logged NO native diarization at all -- it forwards to the
  diarize queue, which is the correct split behaviour.

Across the full logs of that deployment: ``celery-worker-gpu-scaled`` 5 native diarizations,
``celery-worker-gpu-diarize`` 1, ``celery-worker-gpu-transcribe`` 0, and **zero**
``falling back to pyannote`` lines in any of the three.

⚠️ A LATER run in that same deployment was NOT attributable, and the skip guard below exists
because of it: both diarizing topologies were up at once, so the broker -- not this
parametrisation -- chose the worker, and a job dispatched for the "gpu-split" case was served
by ``celery-worker-gpu-scaled``. Bring up exactly one diarizing topology per run.

Run:
    cd backend && PYTHONPATH=. pytest -m gpu tests/integration/test_diar_native_multigpu_provider_live.py -v
"""

from __future__ import annotations

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

#: (pytest id, compose service that must be running for this topology to apply)
TOPOLOGIES = [
    ("gpu-scale", "celery-worker-gpu-scaled"),
    ("gpu-split", "celery-worker-gpu-diarize"),
]


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


@pytest.mark.parametrize("topology_id,worker_service", TOPOLOGIES)
def test_diarize_capable_worker_actually_reaches_native_sidecar(
    admin_token: str, topology_id: str, worker_service: str
) -> None:
    worker_container = compose_service_container(worker_service)
    if worker_container is None:
        pytest.skip(
            f"no running '{worker_service}' container -- this deployment is not {topology_id}"
        )
    # Real narrowing, not a type suppression: `compose_service_container` returns
    # `str | None` and mypy does not treat `pytest.skip` as NoReturn, so without this the
    # container name reaches `docker logs` as a possible None. If skip ever stopped
    # raising, this fails loudly instead of shelling out with a null argument.
    assert worker_container is not None

    # An unattributable result must skip as NOT MEASURED, never pass (issue #711). If the
    # OTHER topology's diarizing worker is up too, both consume a diarize-capable queue in
    # the same deployment and the broker -- not this parametrisation -- picks the winner.
    # Measured 2026-09-05: with gpu-scaled and gpu-diarize both running, the "gpu-split"
    # case was served by gpu-scaled, and asserting on gpu-diarize's logs described a
    # container that had handled nothing.
    rival_service = next(svc for _id, svc in TOPOLOGIES if svc != worker_service)
    if compose_service_container(rival_service) is not None:
        pytest.skip(
            f"both '{worker_service}' and '{rival_service}' are running in this deployment, "
            f"so queue routing decides which serves a job and this test cannot attribute the "
            f"result to {topology_id} -- bring up exactly one diarizing topology and re-run"
        )

    if not os.path.isfile(SAMPLE_AUDIO):
        pytest.skip(f"no sample audio fixture at {SAMPLE_AUDIO}")

    headers = {"Authorization": f"Bearer {admin_token}"}
    # Trailing "Z" is load-bearing (issue #711): `docker logs --since` parses a
    # timestamp with no offset as the DAEMON's LOCAL time, not UTC. On a host west of
    # UTC this silently shifts the cutoff into the future, so every subsequent
    # `docker logs --since <this>` call returns nothing and the assertions below
    # would pass having grepped an empty string -- measured live against this exact
    # container while writing this test.
    start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    file_uuid: str | None = None
    try:
        with open(SAMPLE_AUDIO, "rb") as fh:
            resp = requests.post(
                f"{BASE_URL}/files",
                headers=headers,
                files={
                    "file": (
                        f"diar-native-{topology_id}-{os.getpid()}.wav",
                        fh,
                        "audio/wav",
                    )
                },
                data={"title": f"diar-native-{topology_id}-{os.getpid()}"},
                timeout=60,
            )
        assert resp.ok, f"upload was rejected: {resp.status_code} {resp.text[:200]}"
        file_uuid = resp.json()["uuid"]

        result = _wait_for_completion(headers, file_uuid)
        assert result.get("status") == "completed", (
            f"file did not complete under {topology_id}: {result.get('status')} "
            f"({result.get('last_error_message')})"
        )

        assert result.get("diarization_provider") == "native", (
            f"media_file.diarization_provider is {result.get('diarization_provider')!r} "
            f"under {topology_id}, not 'native' -- the {worker_service} worker fell back to "
            "in-process PyAnnote (issue #655/#711); check DIAR_NATIVE_URL wiring on that "
            "service in docker-compose.diar-native.yml"
        )

        completed = subprocess.run(  # noqa: S603  # nosec B603 -- fixed argv, no shell
            ["docker", "logs", worker_container, "--since", start_time],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # BOTH streams, and that is load-bearing (issue #711): `docker logs` replays the
        # container's stdout and stderr on the corresponding streams of THIS process, and
        # Python's logging module writes to stderr by default -- so every application log
        # line, including "native diarization done", arrives on .stderr. Reading .stdout
        # alone returned the EMPTY STRING while the container had just written 5 matching
        # lines (measured 2026-09-05 against otfresh-gpu711-celery-worker-gpu-scaled).
        # That is worse than a missed assertion: `"falling back to pyannote" not in ""` is
        # vacuously TRUE, so the fallback check could never have failed on any stack.
        logs = completed.stdout + completed.stderr

        assert "native diarization done" in logs, (
            f"'{worker_container}' ({worker_service}) completed the job but its own logs "
            "never show 'native diarization done' -- diarization_provider alone is not "
            "trusted as the sole signal (issue #711)"
        )
        assert "falling back to pyannote" not in logs.lower(), (
            f"'{worker_container}' ({worker_service}) logged a PyAnnote fallback during "
            f"this {topology_id} run despite diarization_provider == 'native'"
        )
    finally:
        if file_uuid:
            requests.delete(f"{BASE_URL}/files/{file_uuid}", headers=headers, timeout=15)
