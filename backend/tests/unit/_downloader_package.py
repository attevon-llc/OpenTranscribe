"""Helpers for tests of ``scripts/download-models.py`` and its ``scripts/model_download`` package.

The downloader's logic lives in ``scripts/model_download/*.py``; the hyphenated entry script keeps
only ``main()``. Tests that exercise a function import the submodule that owns it (so a
``monkeypatch`` lands where the function actually looks the name up); tests that assert on the
downloader's source read it through :func:`downloader_source`, which spans the whole thing.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DOWNLOADER_PY = SCRIPTS_DIR / "download-models.py"
PACKAGE_DIR = SCRIPTS_DIR / "model_download"


def package_files() -> list[Path]:
    """Every module of the downloader package, in a stable order."""
    return sorted(PACKAGE_DIR.rglob("*.py"))


def downloader_source() -> str:
    """The entry script plus every package module, as one string."""
    return "\n".join(p.read_text(encoding="utf-8") for p in [DOWNLOADER_PY, *package_files()])


def package_module(name: str) -> ModuleType:
    """Import ``scripts/model_download/<name>.py`` (``scripts/`` is not on the test path)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(f"model_download.{name}")
