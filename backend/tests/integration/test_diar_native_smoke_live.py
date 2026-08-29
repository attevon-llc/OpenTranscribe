"""Live-stack GPU-residency check for the diar-native sidecar (issue #590).

Converts ``scripts/diar-native-smoke.sh`` (issue #520) into a real pytest test with
falsifiable assertions, per issue #590's second ask: the bash version had zero test
coverage of its own logic, and three real bugs (see ``scripts/gpu-scale-smoke.sh``'s
git history for the sibling case) sat undetected in that family for a long time.

WHY THIS DOES NOT GREP THE LOGS
--------------------------------
``diar-server`` initialises no tracing subscriber (only ``diar-cli`` does), so the
``ort`` crate's registration log line is never emitted and a log-based check can never
fire. ``/healthz`` proves nothing either -- it is ``async fn healthz() -> "ok"`` with no
ORT/session/provider inspection.

WHAT THIS CHECKS INSTEAD
-------------------------
Device-memory residency: the container's own PID must appear in
``nvidia-smi --query-compute-apps`` holding non-zero memory on the GPU the project
configured (``DIAR_NATIVE_GPU`` -> ``GPU_DEVICE_ID`` -> 0, mirroring the overlay's own
precedence). That is strictly stronger than a log line: a CPU-fallback process holds
zero device memory and appears in no compute-apps list, and it also proves GPU
*pinning*, which no log line does. Restart state is checked too -- the overlay carries
``restart: unless-stopped`` because a CUDA load failure crash-loops the container
(``diar-native`` calls ``.error_on_failure()`` at every EP construction site).

The operator-facing CLI (``scripts/diar-native-smoke.sh``, still wired into
``scripts/test-matrix.sh`` Stage 2c and ``run-integration-tests.sh``) is UNCHANGED by
this file -- it stays the fast, dependency-free entry point for a human or another
script to invoke directly. This test exists so the same checks also run under pytest,
with real `assert`s the test-quality gate (`scripts/audit-tests.py`) can evaluate.

Run:
    cd backend && PYTHONPATH=. pytest -m gpu tests/integration/test_diar_native_smoke_live.py -v
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess

import pytest
from dotenv import dotenv_values

pytestmark = [pytest.mark.integration, pytest.mark.gpu]

_REPO_ROOT_ENV = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")


def _repo_env_value(key: str) -> str | None:
    """Read a value out of the repo-root .env via python-dotenv (issue #590) --
    never a hand-rolled grep/cut. Returns None if the file or key is absent."""
    if not os.path.isfile(_REPO_ROOT_ENV):
        return None
    value = dotenv_values(_REPO_ROOT_ENV).get(key)
    return str(value) if value is not None else None


def _diar_native_container() -> str | None:
    """Resolve the running diar-native container by name prefix, not a hardcoded
    name -- compose derives the actual name from the project name, which --fresh
    deployments change."""
    if shutil.which("docker") is None:
        return None
    out = subprocess.run(
        ["docker", "ps", "--filter", "name=diar-native", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    names = [n for n in out.splitlines() if n]
    return names[0] if names else None


def _docker_inspect_state(container: str) -> tuple[bool, int, int]:
    """Return (restarting, restart_count, pid)."""
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Restarting}}|{{.RestartCount}}|{{.State.Pid}}",
            container,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    restarting_s, restart_count_s, pid_s = out.split("|")
    return restarting_s == "true", int(restart_count_s), int(pid_s)


def _nvidia_smi_gpu_index_to_uuid() -> dict[str, str]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    result: dict[str, str] = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) != 2:
            continue
        result[row[0].strip()] = row[1].strip()
    return result


def _nvidia_smi_compute_apps() -> dict[str, tuple[str, int]]:
    """Return {pid: (gpu_uuid, used_mib)} for every process nvidia-smi can see."""
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,gpu_uuid,used_memory",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    result: dict[str, tuple[str, int]] = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) != 3:
            continue
        pid, gpu_uuid, used = (c.strip() for c in row)
        used_mib = int(used.split()[0]) if used.split() else 0
        result[pid] = (gpu_uuid, used_mib)
    return result


@pytest.fixture(scope="module")
def diar_native_state() -> dict:
    """Collect the live sidecar's state once per module. Skips (not fails) when the
    prerequisite hardware/container isn't present -- this is a live-stack check, and
    an absent sidecar means NOT MEASURED, not a failure of the sidecar itself."""
    if shutil.which("nvidia-smi") is None:
        pytest.skip("nvidia-smi not available -- cannot verify GPU residency")

    resolved_container = _diar_native_container()
    if resolved_container is None:
        pytest.skip(
            "no running diar-native container "
            "(start it with ./opentr.sh start dev --with-diar-native)"
        )
        raise AssertionError("unreachable")  # pytest.skip is NoReturn at runtime
    container: str = resolved_container

    restarting, restart_count, pid = _docker_inspect_state(container)

    expected_gpu = (
        os.environ.get("DIAR_NATIVE_GPU")
        or _repo_env_value("DIAR_NATIVE_GPU")
        or os.environ.get("GPU_DEVICE_ID")
        or _repo_env_value("GPU_DEVICE_ID")
        or "0"
    )

    index_to_uuid = _nvidia_smi_gpu_index_to_uuid()
    if expected_gpu not in index_to_uuid:
        pytest.skip(f"configured GPU index {expected_gpu} does not exist on this host")

    return {
        "container": container,
        "restarting": restarting,
        "restart_count": restart_count,
        "pid": pid,
        "expected_gpu_index": expected_gpu,
        "expected_gpu_uuid": index_to_uuid[expected_gpu],
        "compute_apps": _nvidia_smi_compute_apps(),
    }


def test_container_is_not_crash_looping(diar_native_state: dict) -> None:
    """A crash-loop is the real failure mode: the overlay carries
    `restart: unless-stopped`, so a CUDA load failure keeps the container "up" while
    doing nothing -- restart count is the falsifiable signal a plain `docker ps` misses."""
    assert diar_native_state["restarting"] is False, (
        f"{diar_native_state['container']} is restarting -- a CUDA load failure crash-loops it"
    )
    assert diar_native_state["restart_count"] == 0, (
        f"{diar_native_state['container']} has restarted "
        f"{diar_native_state['restart_count']} time(s); expected 0"
    )
    assert diar_native_state["pid"] != 0, f"{diar_native_state['container']} has no running process"


def test_process_holds_device_memory_on_configured_gpu(diar_native_state: dict) -> None:
    """The falsifiable core of this suite: a process that fell back to CPU holds ZERO
    device memory and appears in no compute-apps list at all -- this cannot pass by
    accident the way a log-grep or a bare /healthz 200 can."""
    pid = str(diar_native_state["pid"])
    compute_apps = diar_native_state["compute_apps"]

    assert pid in compute_apps, (
        f"diar-server (pid {pid}) holds NO GPU memory -- the CUDA execution provider "
        "did not register, so it is serving on CPU"
    )

    actual_uuid, used_mib = compute_apps[pid]
    expected_uuid = diar_native_state["expected_gpu_uuid"]

    assert actual_uuid == expected_uuid, (
        f"diar-server is on GPU {actual_uuid} but the project configured index "
        f"{diar_native_state['expected_gpu_index']} ({expected_uuid})"
    )
    assert used_mib > 0, "diar-server holds 0 MiB of device memory"
