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

THE PRECONDITION, AND WHY IT IS NOT A PLAIN SKIP
------------------------------------------------
This suite used to assert GPU residency without ever asking what mode the sidecar
was *configured* for. That is wrong in both directions:

* A sidecar deliberately in ``DIAR_MODE=cpu`` -- legitimate on a CPU-only host,
  under ``--lite``, or under an explicit ``DIAR_NATIVE_MODE=cpu`` -- **failed**,
  reporting "the CUDA execution provider did not register" about a container that
  was never asked to load it.
* Adding a bare "skip when not cuda" would have been worse. On 2026-09-07 the
  sidecar was on CPU *because ``docker-compose.diar-native-gpu.yml`` was never
  loaded on a GPU host* -- a real defect (see
  ``backend/tests/unit/test_opentr_docker_probe_sigpipe.py``), and a skip keyed on
  the symptom would have made the whole gate report NOT MEASURED and moved on.

So the precondition is resolved from THREE signals rather than one, and it can
return a defect verdict (see ``classify_gpu_residency_precondition``):

======================  ===========================  ==========================
sidecar ``DIAR_MODE``   host / container evidence     verdict
======================  ===========================  ==========================
``cuda``                --                            MEASURE (assert residency)
not ``cuda``            no nvidia container runtime   SKIP (genuinely CPU-only)
not ``cuda``            GPU reservation present       SKIP (operator forced CPU
                                                      *with* the overlay loaded)
not ``cuda``            nvidia runtime, NO reservation DEFECT -- fail, naming the
                                                      missing GPU overlay
======================  ===========================  ==========================

The container's ``HostConfig.DeviceRequests`` is what separates the last two: it
is direct evidence of whether the GPU overlay made it into the compose chain,
which no environment variable can tell you. ``FORCE_CPU_MODE=true`` in ``.env``
and ``OT_DIAR_NATIVE_EXPECT_CPU=1`` are the two *explicit* opt-outs; nothing
else downgrades the defect verdict, because "could not check" and "deliberately
CPU" must not be the same answer.

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
import json
import os
import shutil
import subprocess

import pytest
from dotenv import dotenv_values

from tests.compose_project import compose_service_container

pytestmark = [pytest.mark.integration, pytest.mark.gpu]

_REPO_ROOT_ENV = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")

#: Verdicts ``classify_gpu_residency_precondition`` can return. Named constants so the
#: unit guard (``tests/unit/test_diar_native_gpu_precondition.py``) drives the real
#: function rather than string-matching a reimplementation of it.
MEASURE = "measure"
SKIP = "skip"
DEFECT = "defect"

#: The one explicit operator opt-out. Set it when a GPU host is *deliberately* running
#: a CPU sidecar for reasons this test cannot observe (a ``--lite`` or ``--cpu`` stack,
#: for instance -- those are per-invocation flags that leave no trace in ``.env`` or on
#: the container). Deliberately an opt-out with a name, not an inference: an inferred
#: exemption is how a real misconfiguration goes quiet.
EXPECT_CPU_ENV = "OT_DIAR_NATIVE_EXPECT_CPU"


def _repo_env_value(key: str) -> str | None:
    """Read a value out of the repo-root .env via python-dotenv (issue #590) --
    never a hand-rolled grep/cut. Returns None if the file or key is absent."""
    if not os.path.isfile(_REPO_ROOT_ENV):
        return None
    value = dotenv_values(_REPO_ROOT_ENV).get(key)
    return str(value) if value is not None else None


def _diar_native_container() -> str | None:
    """Resolve the running diar-native container IN THE PROJECT UNDER TEST.

    Not a bare ``--filter name=diar-native``: that filter is not scoped to a deployment, and
    several stacks routinely run on one host (the dev stack plus any ``--fresh`` deployment),
    each with a container matching it. Taking the first match selects by creation time, so
    this test would silently measure whichever diar-native sidecar was restarted most
    recently -- and because deployments are pinned to different GPUs, it could report a PASS
    about a stack nobody pointed it at. See ``tests/compose_project.py``.
    """
    return compose_service_container("diar-native")


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


def _sidecar_configured_mode(container: str) -> str:
    """``DIAR_MODE`` as the RUNNING container was actually configured with.

    Read off the container rather than out of ``.env``/compose, because that is the
    only thing that reflects **which compose files were merged**: ``DIAR_MODE``
    defaults to ``cpu`` in ``docker-compose.diar-native.yml`` and is overridden to
    ``cuda`` only by ``docker-compose.diar-native-gpu.yml``. Returns ``""`` when the
    key is absent entirely.
    """
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "DIAR_MODE":
            return value.strip()
    return ""


def _sidecar_has_device_reservation(container: str) -> bool:
    """True when the container was created with an nvidia device request.

    This is the direct, unforgeable evidence that
    ``docker-compose.diar-native-gpu.yml`` was in the compose chain: that file is
    the ONLY place the sidecar's ``deploy.resources.reservations.devices`` block
    lives. ``HostConfig.DeviceRequests`` renders as the JSON literal ``null`` when
    there is none.
    """
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{json .HostConfig.DeviceRequests}}", container],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return False
    return bool(parsed)


def _host_has_nvidia_runtime() -> bool:
    """Does this host's Docker daemon expose the nvidia container runtime?

    ``--format '{{json .Runtimes}}'`` rather than scanning ``docker info``'s prose:
    no pipe, no substring ambiguity, and immune to the SIGPIPE inversion that put
    the sidecar on CPU in the first place (``opentr.sh``'s
    ``docker_runtime_has_nvidia``). A daemon that cannot answer reads as False --
    which only ever *softens* this suite's verdict, never hardens it.
    """
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        return "nvidia" in json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        return False


def classify_gpu_residency_precondition(
    *,
    configured_mode: str,
    has_device_reservation: bool,
    host_nvidia_runtime: bool,
    force_cpu_mode: bool,
    expect_cpu_optout: bool,
) -> tuple[str, str]:
    """Decide whether GPU residency is measurable, legitimately absent, or a defect.

    Pure -- no docker, no filesystem -- so the guard in
    ``tests/unit/test_diar_native_gpu_precondition.py`` can drive the REAL decision
    across every combination instead of re-deriving it. See this module's docstring
    for the table.

    Returns ``(verdict, reason)`` where verdict is ``MEASURE``/``SKIP``/``DEFECT``.
    """
    if configured_mode == "cuda":
        return MEASURE, ""

    described = configured_mode or "<unset>"

    if force_cpu_mode:
        return SKIP, (
            f"sidecar configured DIAR_MODE={described} and FORCE_CPU_MODE=true in .env -- "
            "this deployment opted out of GPU entirely"
        )
    if expect_cpu_optout:
        return SKIP, (
            f"sidecar configured DIAR_MODE={described} and {EXPECT_CPU_ENV} is set -- "
            "operator declared this deployment deliberately CPU-only"
        )
    if not host_nvidia_runtime:
        return SKIP, (
            f"sidecar configured DIAR_MODE={described} and this host's Docker daemon "
            "exposes no nvidia runtime -- CPU is the correct configuration here"
        )
    if has_device_reservation:
        return SKIP, (
            f"sidecar configured DIAR_MODE={described} while holding an nvidia device "
            "reservation -- docker-compose.diar-native-gpu.yml IS loaded and an explicit "
            "DIAR_NATIVE_MODE override chose CPU"
        )
    return DEFECT, (
        f"sidecar is configured DIAR_MODE={described} with NO nvidia device reservation, "
        "on a host whose Docker daemon DOES expose the nvidia runtime. "
        "docker-compose.diar-native-gpu.yml was not in the compose chain that created this "
        "container, so every /diarize call is being served on CPU. Re-start the stack with "
        "`./opentr.sh start dev` and check its output says 'diar-server on GPU <n>' rather "
        "than 'diar-server on CPU (no nvidia runtime detected)'. "
        f"If this deployment is deliberately CPU-only (--lite / --cpu), set {EXPECT_CPU_ENV}=1."
    )


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
    """Collect the live sidecar's state once per module, and resolve the precondition.

    Skips when the prerequisite hardware/container isn't present -- this is a
    live-stack check, and an absent sidecar means NOT MEASURED, not a failure of the
    sidecar itself.

    It does NOT skip for a CPU-configured sidecar: see this module's docstring, and
    ``classify_gpu_residency_precondition`` for the three-signal decision. A GPU host
    whose sidecar came up with no device reservation is the defect, and it FAILS here
    rather than disappearing into the skip count.
    """
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

    # Precondition, resolved before any residency claim is made. `pytest.fail` here
    # rather than in a test body on purpose: this is the one question that decides
    # whether a residency measurement means anything at all, so a single accurate
    # verdict beats two tests failing with a message about an execution provider that
    # was never asked to load. See this module's docstring for the table.
    configured_mode = _sidecar_configured_mode(container)
    verdict, reason = classify_gpu_residency_precondition(
        configured_mode=configured_mode,
        has_device_reservation=_sidecar_has_device_reservation(container),
        host_nvidia_runtime=_host_has_nvidia_runtime(),
        force_cpu_mode=(_repo_env_value("FORCE_CPU_MODE") or "").strip().lower() == "true",
        expect_cpu_optout=bool(os.environ.get(EXPECT_CPU_ENV, "").strip()),
    )
    if verdict == DEFECT:
        pytest.fail(f"{container}: {reason}")
    if verdict == SKIP:
        pytest.skip(f"{container}: {reason}")

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
        "configured_mode": configured_mode,
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
