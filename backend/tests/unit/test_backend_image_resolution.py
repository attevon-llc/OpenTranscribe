"""resolve_backend_image() must pick the backend repository the deployment actually runs.

It used to always return the CUDA repository, so the OpenSearch model step on a lite or
arm64 install pulled the CUDA image (on arm64 no capability-matched leg exists there, and
the step was skipped). Drives the REAL scripts/common.sh functions against a real .env and
a stubbed `uname -m`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_SH = REPO_ROOT / "scripts" / "common.sh"
OPENTRANSCRIBE = REPO_ROOT / "opentranscribe.sh"

pytestmark = pytest.mark.skipif(not COMMON_SH.exists(), reason="scripts/common.sh not present")

CUDA = "davidamacey/opentranscribe-backend:"
LITE = "davidamacey/opentranscribe-backend-lite:"


def _resolve(tmp_path: Path, *, env_lines: list[str], machine: str, extra_env=None) -> str:
    (tmp_path / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uname = bin_dir / "uname"
    uname.write_text(f'#!/bin/sh\n[ "$1" = "-m" ] && echo {machine} || echo Linux\n')
    uname.chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f"source '{COMMON_SH}'; resolve_backend_image"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", **(extra_env or {})},
        check=True,
    )
    return proc.stdout.strip()


def test_default_cuda_install_resolves_to_cuda_image(tmp_path: Path):
    out = _resolve(tmp_path, env_lines=["OT_IMAGE_TAG=v0.6.1"], machine="x86_64")
    assert out == CUDA + "v0.6.1"


def test_lite_mode_resolves_to_lite_image(tmp_path: Path):
    out = _resolve(
        tmp_path, env_lines=["DEPLOYMENT_MODE=Lite", "OT_IMAGE_TAG=v0.6.1"], machine="x86_64"
    )
    assert out == LITE + "v0.6.1"


def test_arm64_resolves_to_non_cuda_image(tmp_path: Path):
    assert _resolve(tmp_path, env_lines=[], machine="aarch64") == LITE + "latest"


def test_arm64_force_full_override_keeps_cuda_repo(tmp_path: Path):
    out = _resolve(
        tmp_path,
        env_lines=[],
        machine="aarch64",
        extra_env={"OPENTRANSCRIBE_FORCE_FULL_ON_ARM64": "true"},
    )
    assert out == CUDA + "latest"


def test_in_process_lite_export_beats_env_file(tmp_path: Path):
    out = _resolve(
        tmp_path,
        env_lines=["DEPLOYMENT_MODE=full"],
        machine="x86_64",
        extra_env={"DEPLOYMENT_MODE": "lite"},
    )
    assert out == LITE + "latest"


def _body(text: str, name: str, indent: str = "") -> str:
    m = re.search(rf"^{indent}{name}\(\) \{{\n.*?^{indent}\}}\n", text, re.S | re.M)
    assert m, name
    return re.sub(r"^\s+", "", m.group(0), flags=re.M)


@pytest.mark.skipif(not OPENTRANSCRIBE.exists(), reason="opentranscribe.sh not present")
@pytest.mark.parametrize("name", ["host_architecture", "effective_deployment_mode"])
def test_opentranscribe_fallback_matches_common(name: str):
    """opentranscribe.sh keeps a standalone fallback; it must not drift from common.sh."""
    canonical = _body(COMMON_SH.read_text(encoding="utf-8"), name)
    fallback = _body(OPENTRANSCRIBE.read_text(encoding="utf-8"), name, indent="    ")
    assert canonical == fallback
