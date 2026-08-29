"""NVML memory accounting that survives a GPU shared with other processes.

Imported as ``from tests.gpu_memory import ...`` — ``backend/`` is on ``sys.path`` via the
root conftest, and this file is not named ``test_*`` so pytest never collects it.

WHY THIS EXISTS
---------------
The diarization VRAM gates (``test_diarizer_lifecycle``, ``test_diarization_perf_gates``)
each carried a private ``_nvml_used_mb()`` that returned ``nvmlDeviceGetMemoryInfo().used``
— **whole-device** memory, every process on the card included. On a workstation where the
dev stack's Celery workers hold the same GPU that is not a measurement of our process, and
it fails accordingly: measured 2026-08-28, ``test_handoff_residue_within_gate`` reported a
5363 MB "handoff residue" against a 350 MB gate while `nvidia-smi` showed the three
co-tenant worker PIDs growing 2412 MB -> 7632 MB (Δ5220 MB) over the same window. Nothing
had regressed; the test attributed another container's model load to
``Transcriber.unload_model``. It passes or fails by coincidence of timing.

WHY NOT ``torch.cuda.memory_reserved()``, which is already per-process
-------------------------------------------------------------------
Because it would not see the thing these gates exist to catch. ``Transcriber`` runs
faster-whisper on **CTranslate2**, which allocates through its own allocator, and cuDNN /
cuBLAS workspaces live outside the torch caching allocator too. NVML counts all of it.

WHY NOT MATCH ON ``os.getpid()``
--------------------------------
NVML reports **host-namespace** PIDs. Inside the benchmark container ``os.getpid()`` is 1
while NVML lists this same process as e.g. 1437579 (verified, not assumed). So a process
cannot find its own row. What it *can* do is record the set of PIDs already on the device
before it allocates anything, and subtract those: everything left is ours.

RESIDUAL LIMITATION, stated rather than hidden: a *new* foreign process that starts after
the baseline is captured is still attributed to us. `assert_self_is_measured` catches the
opposite error (a baseline taken too late, which silently reports ~0 and passes), and the
failure messages carry the foreign totals so a contaminated run is diagnosable instead of
merely red.
"""

from __future__ import annotations

import ctypes
from collections.abc import Mapping
from collections.abc import Set
from dataclasses import dataclass

_MB = float(1024**2)
_NVML_SUCCESS = 0
_NVML_ERROR_INSUFFICIENT_SIZE = 7
#: NVML's sentinel for "this process's usage is not available to you".
_NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF


class NvmlUnavailableError(RuntimeError):
    """NVML could not be loaded or queried.

    Raised, never swallowed. The predecessor helper returned ``0.0`` from a bare
    ``except Exception``, which turned "no NVML" into "0 MB used" — and a residue computed
    as ``0.0 - 0.0`` is 0, so the gate PASSED on a machine where it had measured nothing.
    """


class VacuousMeasurementError(RuntimeError):
    """The baseline already included this process, so the delta means nothing."""


class _Memory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _ProcessInfoV2(ctypes.Structure):
    """``nvmlProcessInfo_v2_t`` — used by ``...ComputeRunningProcesses_v2/_v3``."""

    _fields_ = [
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    ]


class _ProcessInfoV1(ctypes.Structure):
    """``nvmlProcessInfo_t`` — the 2-field legacy layout.

    Passing this struct to the ``_v2``/``_v3`` entry points (or vice versa) returns
    garbage rather than an error, which is why the layout is chosen per entry point below
    instead of one struct being used for all three.
    """

    _fields_ = [
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class GpuMemory:
    """One NVML sample of a device.

    Attributes:
        device_used_mb: ``nvmlDeviceGetMemoryInfo().used``, every process included.
        per_pid_mb: Per-compute-process usage, keyed by **host-namespace** PID.
    """

    device_used_mb: float
    per_pid_mb: Mapping[int, float]


def _load() -> ctypes.CDLL:
    try:
        lib = ctypes.CDLL("libnvidia-ml.so.1")
    except OSError as exc:  # pragma: no cover - depends on the host driver
        raise NvmlUnavailableError(f"cannot load libnvidia-ml.so.1: {exc}") from exc
    if lib.nvmlInit_v2() != _NVML_SUCCESS:
        raise NvmlUnavailableError("nvmlInit_v2 failed")
    return lib


def _handle(lib: ctypes.CDLL, device_index: int) -> ctypes.c_void_p:
    handle = ctypes.c_void_p()
    if lib.nvmlDeviceGetHandleByIndex(device_index, ctypes.byref(handle)) != _NVML_SUCCESS:
        raise NvmlUnavailableError(f"nvmlDeviceGetHandleByIndex({device_index}) failed")
    return handle


def _compute_processes(lib: ctypes.CDLL, handle: ctypes.c_void_p) -> dict[int, float]:
    """``{host_pid: used_mb}`` for the compute processes on ``handle``."""
    last_error = "no nvmlDeviceGetComputeRunningProcesses entry point"
    for symbol, struct in (
        ("nvmlDeviceGetComputeRunningProcesses_v3", _ProcessInfoV2),
        ("nvmlDeviceGetComputeRunningProcesses_v2", _ProcessInfoV2),
        ("nvmlDeviceGetComputeRunningProcesses", _ProcessInfoV1),
    ):
        try:
            fn = getattr(lib, symbol)
        except AttributeError:
            continue

        # Size probe: NVML answers INSUFFICIENT_SIZE and writes the required count.
        count = ctypes.c_uint(0)
        rc = fn(handle, ctypes.byref(count), None)
        if rc not in (_NVML_SUCCESS, _NVML_ERROR_INSUFFICIENT_SIZE):
            last_error = f"{symbol} size probe returned {rc}"
            continue

        # Headroom, because a process can start between the probe and the read.
        capacity = count.value + 8
        buffer = (struct * capacity)()
        count = ctypes.c_uint(capacity)
        rc = fn(handle, ctypes.byref(count), buffer)
        if rc != _NVML_SUCCESS:
            last_error = f"{symbol} returned {rc}"
            continue

        return {
            buffer[i].pid: (
                0.0
                if buffer[i].usedGpuMemory == _NVML_VALUE_NOT_AVAILABLE
                else buffer[i].usedGpuMemory / _MB
            )
            for i in range(count.value)
        }

    raise NvmlUnavailableError(last_error)


def read(device_index: int = 0) -> GpuMemory:
    """Sample device ``device_index``.

    Index 0 is correct inside the benchmark container: compose reserves exactly one card
    via ``device_ids``, so the container's NVML index 0 IS the reserved physical GPU.

    Raises:
        NvmlUnavailableError: if NVML cannot be loaded or any query fails.
    """
    lib = _load()
    handle = _handle(lib, device_index)
    memory = _Memory()
    if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) != _NVML_SUCCESS:
        raise NvmlUnavailableError("nvmlDeviceGetMemoryInfo failed")
    return GpuMemory(
        device_used_mb=memory.used / _MB,
        per_pid_mb=_compute_processes(lib, handle),
    )


#: Answer to `co_tenant_pids`, memoised per device for the life of the process.
_CO_TENANTS: dict[int, frozenset[int]] = {}


def co_tenant_pids(device_index: int = 0) -> frozenset[int]:
    """The PIDs that were on the device before THIS process touched it.

    Memoised on first call, and that is the whole point rather than an optimisation: the
    question "who else is on this card" only has a correct answer before we allocate, so
    the first caller's view is the one every later caller must reuse. Recomputing it later
    returns a set that includes *us*, `usage_excluding` then subtracts our own memory, and
    every delta collapses to ~0 — a pass that measured nothing.

    That is not hypothetical. A module-scoped fixture recomputing it per module gave
    `test_soak_no_vram_creep` a co-tenant set containing its own PID, because
    `test_diarizer_lifecycle` had already run in the same pytest process and left a CUDA
    context behind. `assert_self_is_measured` caught it; the memo is what prevents it.

    Call it before the first CUDA allocation in the process.
    """
    if device_index not in _CO_TENANTS:
        _CO_TENANTS[device_index] = frozenset(read(device_index).per_pid_mb)
    return _CO_TENANTS[device_index]


def usage_excluding(sample: GpuMemory, others: Set[int]) -> float:
    """Device memory in ``sample`` not accounted for by ``others``, in MB.

    A PID from ``others`` that has since exited contributes 0, which is correct: its
    memory is genuinely gone from ``device_used_mb`` too.
    """
    return sample.device_used_mb - sum(sample.per_pid_mb.get(pid, 0.0) for pid in others)


def assert_self_is_measured(sample: GpuMemory, others: Set[int]) -> None:
    """Fail unless this process shows up as a PID outside ``others``.

    Guards the one way `usage_excluding` lies quietly: if the baseline was captured after
    this process already had a CUDA context, its own PID lands in ``others``, every later
    reading subtracts our own memory, and the delta comes out near zero — a pass that
    measured nothing.

    Raises:
        VacuousMeasurementError: if no PID outside ``others`` is present.
    """
    if not set(sample.per_pid_mb) - set(others):
        raise VacuousMeasurementError(
            "no GPU process outside the recorded co-tenant set "
            f"{sorted(others)}: the baseline was taken after this process already held a "
            "CUDA context, so its own usage is being subtracted out. Capture the baseline "
            "before any CUDA allocation."
        )


def describe(sample: GpuMemory, others: Set[int]) -> str:
    """One-line breakdown for a failure message — device, ours, and each co-tenant."""
    tenants = ", ".join(f"{pid}={sample.per_pid_mb.get(pid, 0.0):.0f}MB" for pid in sorted(others))
    return (
        f"device={sample.device_used_mb:.0f}MB "
        f"ours={usage_excluding(sample, others):.0f}MB "
        f"co-tenants[{tenants or 'none'}]"
    )
