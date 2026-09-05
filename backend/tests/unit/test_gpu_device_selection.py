"""Runtime GPU device selection (issue #719 runtime half).

The test-harness half of #719 (PR #730) stopped *pytest* opening CUDA contexts on
cards this project does not own. This file covers the **runtime** half: the API, the
Celery workers and the diar-native sidecar, which all run from the same image.

Two measured facts drive every assertion here (both re-derivable with
`scripts/gpu-device-order-probe.py`):

1. Docker's `device_ids: ['N']` uses the **nvidia-smi / PCI** index, and the container
   then sees exactly ONE device, at ordinal 0. So "always use ordinal 0" is CORRECT for
   every service with a single-device reservation, and a naive
   `CUDA_VISIBLE_DEVICES=<host index>` there makes CUDA vanish entirely
   (`cuInit` -> 100, `device_count() == 0`).
2. A service reserved with `count: all` (`celery-cpu-worker`) sees EVERY card, and
   CUDA's default `FASTEST_FIRST` ordering does not match nvidia-smi. There, ordinal 0
   is an arbitrary card the deployment may not own.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_cuda_device_index(**kwargs):
    """Import inside the call so the static-file tests below stay independent.

    A module-level import would turn a missing resolver into one collection ERROR for
    the whole file, hiding whether the Dockerfile / env-template / NVML assertions pass
    or fail on their own.
    """
    from app.utils.hardware_detection import resolve_cuda_device_index as _resolve

    return _resolve(**kwargs)


class _FakeCuda:
    """Minimal stand-in for torch.cuda — no driver, no context, no VRAM."""

    def __init__(self, count: int):
        self._count = count

    def device_count(self) -> int:
        return self._count


@pytest.fixture
def clean_gpu_env(monkeypatch):
    monkeypatch.delenv("GPU_DEVICE_ID", raising=False)
    return monkeypatch


# --- resolve_cuda_device_index ------------------------------------------------


def test_single_visible_device_resolves_to_ordinal_zero_whatever_gpu_device_id_says(
    clean_gpu_env,
):
    """The Docker-remap invariant: one reserved card is ALWAYS ordinal 0.

    This is the case that a naive "honour GPU_DEVICE_ID everywhere" fix breaks, and
    breaking it disables CUDA on every GPU worker in the product. `device_ids: ['1']`
    hands the container one device; selecting ordinal 1 inside it selects nothing.
    """
    clean_gpu_env.setenv("GPU_DEVICE_ID", "2")
    assert resolve_cuda_device_index(cuda=_FakeCuda(1)) == 0


def test_multiple_visible_devices_honour_gpu_device_id(clean_gpu_env):
    """`count: all` (celery-cpu-worker) — ordinal 0 is an arbitrary card, so ask env."""
    clean_gpu_env.setenv("GPU_DEVICE_ID", "1")
    assert resolve_cuda_device_index(cuda=_FakeCuda(3)) == 1


def test_multiple_visible_devices_without_gpu_device_id_fall_back_to_zero(clean_gpu_env):
    assert resolve_cuda_device_index(cuda=_FakeCuda(3)) == 0


@pytest.mark.parametrize("bad", ["", "   ", "not-an-int", "1.5", "-1"])
def test_unparseable_or_negative_gpu_device_id_falls_back_to_zero(clean_gpu_env, bad):
    clean_gpu_env.setenv("GPU_DEVICE_ID", bad)
    assert resolve_cuda_device_index(cuda=_FakeCuda(3)) == 0


def test_out_of_range_gpu_device_id_falls_back_to_zero(clean_gpu_env):
    """An index past what this container can see must not raise from a device query."""
    clean_gpu_env.setenv("GPU_DEVICE_ID", "7")
    assert resolve_cuda_device_index(cuda=_FakeCuda(3)) == 0


def test_no_visible_devices_resolves_to_zero(clean_gpu_env):
    clean_gpu_env.setenv("GPU_DEVICE_ID", "2")
    assert resolve_cuda_device_index(cuda=_FakeCuda(0)) == 0


def test_gpu_scale_device_id_is_honoured_when_it_is_the_active_card(clean_gpu_env):
    """--gpu-scale points the workers at GPU_SCALE_DEVICE_ID via Docker.

    The scaled worker still gets a single-device reservation, so it stays ordinal 0;
    this pins that the resolver does not try to second-guess that with the host index.
    """
    clean_gpu_env.setenv("GPU_DEVICE_ID", "0")
    clean_gpu_env.setenv("GPU_SCALE_DEVICE_ID", "2")
    assert resolve_cuda_device_index(cuda=_FakeCuda(1)) == 0


# --- CUDA_DEVICE_ORDER must be pinned in the images ---------------------------

DOCKERFILES = [
    "backend/Dockerfile.prod",
    "backend/Dockerfile.lite",
    "backend/Dockerfile.blackwell",
    "backend/Dockerfile.test",
]


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_backend_image_pins_pci_bus_id_ordering(dockerfile):
    """Ordinals must agree with nvidia-smi, NVML and Docker `device_ids`.

    Set as image ENV rather than compose `environment:` because it must hold before
    the process starts (there is no safe point to set it after the first CUDA call)
    and because the API, every Celery worker and the diar-native sidecar all run from
    this one image with only their CMD replaced — a per-service compose entry would
    have to be repeated on a dozen services and would silently miss the next one.
    """
    path = REPO_ROOT / dockerfile
    assert path.exists(), f"{dockerfile} missing — update this list if it was renamed"
    text = path.read_text()
    assert re.search(r"^ENV\s+CUDA_DEVICE_ORDER=PCI_BUS_ID\s*$", text, re.MULTILINE), (
        f"{dockerfile} does not pin CUDA_DEVICE_ORDER=PCI_BUS_ID"
    )


# --- the generated release-test .env must not kill CUDA -----------------------


def test_release_test_env_template_does_not_set_cuda_visible_devices():
    """Measured: `device_ids: ['1']` + `CUDA_VISIBLE_DEVICES=1` => device_count() == 0.

    Every service in docker-compose.yml reads `env_file: .env`, so a host GPU index
    written into the generated .env reaches containers that Docker has ALREADY narrowed
    to a single card at ordinal 0. `cuInit` then fails with 100 (CUDA_ERROR_NO_DEVICE)
    and every GPU worker silently falls back to CPU while still reporting healthy.
    """
    template = REPO_ROOT / "scripts/release-tests/lib/env-template.sh"
    assert template.exists()
    offending = [
        line
        for line in template.read_text().splitlines()
        if re.match(r"^\s*CUDA_VISIBLE_DEVICES=", line)
    ]
    assert not offending, (
        "env-template.sh writes CUDA_VISIBLE_DEVICES into the generated .env, which "
        f"disables CUDA in every GPU-reserved container: {offending}"
    )


# --- NVML handle cache must not pin the first device forever ------------------


def test_nvml_monitor_does_not_reuse_a_handle_for_a_different_device():
    """`get_gpu_memory(2)` after `get_gpu_memory(0)` must not answer for device 0.

    The module caches a single global handle and early-returns on it, so the first
    caller's index won every later query — which silently reports the wrong card's
    memory on the one worker that legitimately queries several (`update_gpu_stats`).
    """
    import app.utils.nvml_monitor as nvml

    src = Path(nvml.__file__).read_text()
    assert "_handles" in src, (
        "nvml_monitor still caches one global handle; a per-device cache is required "
        "for update_gpu_stats to report GPU_SCALE_DEVICE_ID correctly"
    )


# --- the resolver must actually be used by the selection sites ----------------


def test_hardware_detection_has_no_hardcoded_cuda_zero_selection():
    """The selection + sizing sites must go through the resolved index.

    Batch sizing and hybrid-mode both read total VRAM; reading device 0's VRAM while
    computing on device 1 sizes batches against the wrong card. Both are the same bug,
    so both are pinned here.
    """
    src = (REPO_ROOT / "backend/app/utils/hardware_detection.py").read_text()
    assert 'torch.device("cuda:0")' not in src
    assert "torch.cuda.set_device(0)" not in src
