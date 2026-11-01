"""`ensure_opensearch_models` (scripts/common.sh) must fetch ONE group, from the right image,
and must not hide a failed download.

It used to run all of ``download-models.py`` (no ``--only``) against ``:latest`` while mounting
only ``opensearch-ml``, so every other group was downloaded into a throwaway container, and its
output went through ``grep ... || true`` so a crashed downloader looked like a quiet one.

The real function is sourced and run against a fake ``docker`` on PATH that records its argv.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON = REPO_ROOT / "scripts" / "common.sh"
DL_SCRIPT = REPO_ROOT / "scripts" / "download-models.py"

pytestmark = pytest.mark.skipif(not COMMON.exists(), reason="scripts/common.sh not present")

FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  image) exit 0 ;;
  run) echo "Downloading all-MiniLM-L6-v2"; echo "boom: traceback line"; exit "${FAKE_RUN_RC:-0}" ;;
esac
exit 0
"""


def _run(tmp_path: Path, run_rc: int, env_extra: dict[str, str] | None = None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "download-models.py").write_text("# stub\n")
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_RUN_RC": str(run_rc),
        "MODEL_CACHE_DIR": str(work / "models"),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        ["bash", "-c", f"source {COMMON}; ensure_opensearch_models"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return proc, [ln for ln in lines if ln.startswith("run ")]


def test_runs_only_the_opensearch_group(tmp_path: Path):
    _, runs = _run(tmp_path, 0)
    assert len(runs) == 1
    assert runs[0].endswith("python /app/download-models.py --only opensearch"), runs[0]


def test_the_group_it_names_exists_in_the_downloader():
    assert "'opensearch': download_opensearch_neural_models" in DL_SCRIPT.read_text()


def test_uses_the_image_tag_the_install_uses_not_latest(tmp_path: Path):
    _, runs = _run(tmp_path, 0, {"OT_IMAGE_TAG": "v0.6.0"})
    assert " davidamacey/opentranscribe-backend:v0.6.0 " in runs[0]
    assert ":latest" not in runs[0]


def test_defaults_to_latest_only_when_no_tag_is_configured(tmp_path: Path):
    _, runs = _run(tmp_path, 0)
    assert " davidamacey/opentranscribe-backend:latest " in runs[0]


def test_a_failed_download_is_reported_with_its_exit_status(tmp_path: Path):
    proc, _ = _run(tmp_path, 7)
    assert "exited with status 7" in proc.stdout
    assert "boom: traceback line" in proc.stdout
    assert (
        proc.returncode == 1
    )  # still non-fatal-by-convention: the caller treats 1 as "retry at runtime"
