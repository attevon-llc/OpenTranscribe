"""Shared fixtures for the ONNX test suite.

Key design choices:

- ``onnx_models_dir`` resolves artifacts via ``PYANNOTE_ONNX_MODELS_DIR`` or
  a default under the project's ``models/onnx/`` directory. Tests skip if the
  artifacts are absent — run ``python -m pyannote.audio.onnx.export ...``
  first.
- ``device`` defaults to CUDA when available, else CPU. An ``--onnx-device``
  pytest option overrides (named to avoid pytest-playwright's ``--device``).
- ``hf_token`` reads ``HF_TOKEN`` or ``HUGGINGFACE_TOKEN`` from the env.
  Tests that need to load a PyTorch reference model skip if absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_addoption(parser):
    # Named --onnx-device because pytest-playwright already claims --device
    parser.addoption(
        "--onnx-device",
        default=None,
        help="Device for ONNX tests (cpu | cuda | cuda:0). Default: cuda if available, else cpu.",
    )


@pytest.fixture(scope="session")
def device(request: pytest.FixtureRequest) -> str:
    override = request.config.getoption("--onnx-device", default=None)
    if override:
        return str(override)
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


@pytest.fixture(scope="session")
def onnx_models_dir() -> Path:
    raw = os.environ.get("PYANNOTE_ONNX_MODELS_DIR")
    if raw:
        p = Path(raw)
    else:
        # Default: repo-relative ./models/onnx
        repo_root = Path(__file__).resolve().parents[3]
        p = repo_root / "models" / "onnx"
    if not (p / "segmentation.onnx").exists() or not (p / "embedding.onnx").exists():
        pytest.skip(
            f"ONNX artifacts missing in {p}. "
            "Run `python -m pyannote.audio.onnx.export --out-dir models/onnx` first."
        )
    return p


@pytest.fixture(scope="session")
def hf_token() -> str:
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not tok:
        pytest.skip("HF_TOKEN / HUGGINGFACE_TOKEN not set")
        raise RuntimeError("unreachable after pytest.skip")  # helps mypy
    return tok
