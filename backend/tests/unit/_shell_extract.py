"""Helpers for the shell-script tests: run bash snippets and lift functions/case arms out of the user-facing scripts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


INSTALLER = REPO_ROOT / "setup-opentranscribe.sh"


MANAGER = REPO_ROOT / "opentranscribe.sh"


DOWNLOADER = REPO_ROOT / "scripts" / "download-models.sh"


DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"


COMMON = REPO_ROOT / "scripts" / "common.sh"


def extract_function(script: Path, name: str) -> str:
    """Pull one shell function out of a script so it can be run in isolation.

    Two definition shapes exist. opentranscribe.sh ships standalone to end users, so
    read_env_value/resolve_default_branch are defined inside an
    ``if ! declare -F <name>; then ... fi`` guard (common.sh's copy wins when present) —
    they are indented, and a ``^name()`` match cannot see them. Extracting the whole
    guard block yields valid bash either way.
    """
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not out.strip():
        out = subprocess.run(
            ["sed", "-n", rf"/^if ! declare -F {name}\b/,/^fi$/p", str(script)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


def extract_functions(script: Path, names: list[str]) -> str:
    """Pull several shell functions out of a script, in the order given.

    A command that was decomposed into named steps (``cmd_update`` and its ``update_*``
    helpers) needs the whole set to run in isolation; naming them here keeps each test
    explicit about which pieces of the script it exercises, and lets it stub the rest.
    """
    return "\n".join(extract_function(script, name) for name in names)


def run_shell(snippet: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cwd=str(cwd) if cwd else None,
    )
    return (proc.stdout + proc.stderr).strip()


requires_install_scripts = pytest.mark.skipif(
    not INSTALLER.exists(), reason="install scripts not present in this checkout"
)
