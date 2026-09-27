"""The full default model download must work on a CPU-only host (issue #1003).

Baking models in a ``docker build`` stage (no GPU) failed three ways, all reproduced
against the unmodified downloader inside the real backend image:

1. The pinned pyannote fork's ``_gpu_empty_cache()`` calls ``torch.mps.empty_cache()``
   whenever CUDA is absent; with no MPS backend that raises ``RuntimeError: Cannot
   execute emptyCache() without MPS backend`` and aborts the diarization step.
2. ``USE_GPU`` defaults to ``true``, so the WhisperX step asked for ``cuda``/``float16``
   on a host that has neither.
3. ``DIAR_MODELS_DIR`` (``/models``) sits at the filesystem root and the image never
   created it, so the non-root ``appuser`` could not provision diar-native models.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.transcription.native_provision import DEFAULT_MODELS_DIR

REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
DOCKERFILES = [
    REPO_ROOT / "backend" / "Dockerfile.prod",
    REPO_ROOT / "backend" / "Dockerfile.lite",
]

pytestmark = pytest.mark.skipif(
    not DOWNLOADER_PY.exists(), reason="scripts/download-models.py not present in this checkout"
)


@pytest.fixture
def real_mps_empty_cache():
    """The real ``torch.mps.empty_cache``, restored (with ``torch.load``) after the test.

    Importing ``download-models.py`` patches ``torch.load`` and (on a host without MPS)
    ``torch.mps.empty_cache`` process-wide; both are put back so no other test in this
    worker inherits them.
    """
    torch = pytest.importorskip("torch")
    saved_load = torch.load
    saved_empty_cache = torch.mps.empty_cache
    try:
        yield saved_empty_cache
    finally:
        torch.load = saved_load
        torch.mps.empty_cache = saved_empty_cache


@pytest.fixture
def downloader(real_mps_empty_cache):
    """``download-models.py`` loaded as a module (its filename is not importable)."""
    spec = importlib.util.spec_from_file_location("download_models_under_test", DOWNLOADER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_torch(*, mps_available: bool):
    calls: list[str] = []
    mps = SimpleNamespace(empty_cache=lambda: calls.append("real empty_cache"))
    backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps_available))
    return SimpleNamespace(mps=mps, backends=backends), calls


def test_guard_neutralises_mps_empty_cache_without_mps(downloader):
    fake, calls = _fake_torch(mps_available=False)

    assert downloader.guard_mps_empty_cache(fake) is True
    fake.mps.empty_cache()
    assert calls == [], "the real torch.mps.empty_cache must not run when MPS is unavailable"


def test_guard_leaves_mps_empty_cache_alone_when_mps_is_available(downloader):
    fake, calls = _fake_torch(mps_available=True)

    assert downloader.guard_mps_empty_cache(fake) is False
    fake.mps.empty_cache()
    assert calls == ["real empty_cache"], "an Apple-silicon host must keep its real cache release"


def test_guard_is_a_no_op_on_a_torch_without_mps(downloader):
    fake = SimpleNamespace(backends=SimpleNamespace())

    assert downloader.guard_mps_empty_cache(fake) is False


def test_real_torch_mps_empty_cache_is_safe_after_guard(downloader, real_mps_empty_cache):
    """Against the REAL torch: the call pyannote makes raises without the guard, not with it."""
    import torch

    if torch.backends.mps.is_available():
        pytest.skip("host has an MPS backend; the CPU-only failure cannot occur here")

    # Loading the downloader already applied the guard; restore the real function to
    # prove the premise, then re-apply the guard to prove the fix.
    torch.mps.empty_cache = real_mps_empty_cache
    with pytest.raises(RuntimeError, match="MPS"):
        torch.mps.empty_cache()

    assert downloader.guard_mps_empty_cache(torch) is True
    torch.mps.empty_cache()


@pytest.mark.parametrize(
    ("use_gpu", "cuda_available", "compute_type", "expected"),
    [
        # The #1003 case: defaults (USE_GPU=true, float16) on a host with no GPU.
        ("true", False, "float16", ("cpu", "int8")),
        ("TRUE", False, "float16", ("cpu", "int8")),
        ("false", False, "float16", ("cpu", "int8")),
        ("false", True, "float16", ("cpu", "int8")),
        ("true", False, "float32", ("cpu", "float32")),
        ("true", False, "int8", ("cpu", "int8")),
        # A GPU host keeps exactly what it asked for.
        ("true", True, "float16", ("cuda", "float16")),
        ("true", True, "int8_float16", ("cuda", "int8_float16")),
    ],
)
def test_resolve_whisper_device(downloader, use_gpu, cuda_available, compute_type, expected):
    assert downloader.resolve_whisper_device(use_gpu, cuda_available, compute_type) == expected


def test_whisperx_step_uses_the_resolver(downloader):
    """The step itself must route through the resolver, not re-derive the device inline."""
    source = inspect.getsource(downloader.download_whisperx_models)
    assert "resolve_whisper_device(" in source
    assert "torch.cuda.is_available()" in source


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_image_creates_diar_models_dir_owned_by_appuser(dockerfile):
    """``DIAR_MODELS_DIR``'s default must exist in the image and be writable by appuser."""
    text = dockerfile.read_text()
    runs = re.split(r"^RUN ", text, flags=re.MULTILINE)
    user_run = next((r for r in runs if "useradd" in r), None)
    assert user_run is not None, f"{dockerfile.name}: no RUN step creates appuser"
    # The RUN step ends at the first line without a trailing continuation.
    step = re.split(r"(?<!\\)\n", user_run, maxsplit=1)[0]

    models = re.escape(DEFAULT_MODELS_DIR)
    assert re.search(rf"mkdir -p [^&]*{models}(\s|$)", step), (
        f"{dockerfile.name} does not create {DEFAULT_MODELS_DIR} (DIAR_MODELS_DIR's default)"
    )
    assert re.search(rf"chown (-R )?appuser:appuser [^&]*{models}(\s|$)", step), (
        f"{dockerfile.name} creates {DEFAULT_MODELS_DIR} but does not chown it to appuser"
    )
