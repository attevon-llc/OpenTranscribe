"""``FORCE_CPU_MODE`` is true in any letter case, in the shell scripts as in the backend.

The backend reports "CPU forced" for ``FORCE_CPU_MODE=TRUE`` (``api/endpoints/system.py``
lower-cases and strips it). The shell scripts compared against the exact string ``true``, so
with ``TRUE`` the UI said the GPU was off while the stack loaded the GPU overlay.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGER = REPO_ROOT / "opentranscribe.sh"
OPENTR = REPO_ROOT / "opentr.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

TRUE_SPELLINGS = ["true", "TRUE", "True", " true ", '"True"']
NOT_TRUE_SPELLINGS = ["false", "FALSE", "", "yes", "1", "truee"]


def _function(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert m, f"{name}() not found in {script}"
    return m.group(0)


def _run(
    body: str, cwd: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"FORCE_CPU_MODE", "OPENTRANSCRIBE_FORCE_CPU"}
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", body],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _manager_says_cpu(tmp_path: Path, value: str) -> bool:
    (tmp_path / ".env").write_text(f"FORCE_CPU_MODE={value}\n", encoding="utf-8")
    body = (
        "set -uo pipefail\n"
        + _function(COMMON, "read_env_value")
        + _function(MANAGER, "force_cpu_mode_requested")
        + "force_cpu_mode_requested && echo CPU || echo GPU\n"
    )
    proc = _run(body, tmp_path)
    assert proc.stdout.strip() in {"CPU", "GPU"}, proc.stdout + proc.stderr
    return proc.stdout.strip() == "CPU"


def _opentr_says_cpu(tmp_path: Path, value: str) -> bool:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for tool in ("docker", "nvidia-smi"):
        stub = stubs / tool
        stub.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
    body = (
        "set -uo pipefail\n"
        + _function(OPENTR, "detect_and_configure_hardware")
        + "detect_and_configure_hardware 2>&1\n"
    )
    proc = _run(body, tmp_path, {"FORCE_CPU_MODE": value, "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert "Hardware Configuration" in proc.stdout, proc.stdout + proc.stderr
    return "CPU-only mode (FORCE_CPU_MODE" in proc.stdout


@pytest.mark.parametrize("value", TRUE_SPELLINGS)
def test_opentranscribe_sh_honours_every_spelling_of_true(tmp_path: Path, value: str):
    assert _manager_says_cpu(tmp_path, value) is True


@pytest.mark.parametrize("value", NOT_TRUE_SPELLINGS)
def test_opentranscribe_sh_does_not_treat_other_values_as_true(tmp_path: Path, value: str):
    assert _manager_says_cpu(tmp_path, value) is False


@pytest.mark.parametrize("value", TRUE_SPELLINGS)
def test_opentr_sh_honours_every_spelling_of_true(tmp_path: Path, value: str):
    assert _opentr_says_cpu(tmp_path, value.strip().strip('"')) is True


@pytest.mark.parametrize("value", NOT_TRUE_SPELLINGS)
def test_opentr_sh_does_not_treat_other_values_as_true(tmp_path: Path, value: str):
    assert _opentr_says_cpu(tmp_path, value) is False


def test_release_test_oracle_reads_force_cpu_mode_case_insensitively():
    lib = (REPO_ROOT / "scripts/release-tests/lib/compose-chain.sh").read_text(encoding="utf-8")
    line = next(ln for ln in lib.splitlines() if "grep -E '^FORCE_CPU_MODE='" in ln)
    assert "tr '[:upper:]' '[:lower:]'" in line, line
