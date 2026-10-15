"""Build Hugging Face hub cache layouts on disk, and point the library at them.

``make_complete_repo`` writes what ``snapshot_download`` leaves behind (``refs/main`` ->
``snapshots/<sha>/<file>`` symlinks into ``blobs/``); ``make_partial_repo`` writes what an
interrupted one leaves (``blobs/<etag>.incomplete``, no ref, no snapshot). Neither touches the
network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REVISION = "a" * 40


def _repo_root(cache: Path, repo_id: str) -> Path:
    return cache / f"models--{repo_id.replace('/', '--')}"


def make_complete_repo(cache: Path, repo_id: str, *, model_bytes: int = 1024) -> Path:
    """Create a complete cached repository and return its directory."""
    root = _repo_root(cache, repo_id)
    blobs = root / "blobs"
    snapshot = root / "snapshots" / REVISION
    blobs.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(exist_ok=True)
    (root / "refs" / "main").write_text(REVISION, encoding="utf-8")
    for name, size in (("config.json", 64), ("model.bin", model_bytes)):
        blob = blobs / f"etag-{name}"
        blob.write_bytes(b"x" * size)
        link = snapshot / name
        if not link.exists():
            link.symlink_to(Path("..") / ".." / "blobs" / blob.name)
    return root


def make_partial_repo(cache: Path, repo_id: str, *, incomplete_bytes: int = 512) -> Path:
    """Create a repository whose download was interrupted and return its directory."""
    root = _repo_root(cache, repo_id)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / "etag-model.bin.incomplete").write_bytes(b"x" * incomplete_bytes)
    return root


@pytest.fixture
def hf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``huggingface_hub`` and the app at an empty temporary hub cache."""
    from huggingface_hub import constants

    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    return cache
