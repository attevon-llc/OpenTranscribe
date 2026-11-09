"""Operator-safety behaviours of opentranscribe.sh and scripts/common.sh.

* ``config`` validated ``docker compose config`` against the default file set, not the overlay
  chain ``start`` uses.
* ``update_download_artifact`` accepted an empty 200 response and replaced a working file.
* Plain ``backup`` wrote plaintext transcripts with the caller's umask (world-readable).
* ``fix_pipeline_scratch_permissions`` (and its standalone twin) chowned the first
  ``*_pipeline_scratch`` volume of ANY compose project.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_config_validates_the_start_overlay_chain(tmp_path):
    (tmp_path / ".env").write_text("BACKEND_PORT=5174\n")
    script = "\n".join(
        [
            "BLUE=; NC=",
            "check_environment() { :; }",
            'docker() { echo "$@" >> docker.log; return 0; }',
            "apply_deployment_pins() { :; }",
            "get_compose_files() { echo '-f docker-compose.yml -f docker-compose.gpu.yml'; }",
            extract_function(MANAGER, "cmd_config"),
            "cmd_config",
        ]
    )
    _bash(script, tmp_path)
    assert (
        "compose -f docker-compose.yml -f docker-compose.gpu.yml config"
        in (tmp_path / "docker.log").read_text()
    )


def _download(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "keep.txt").write_text("working\n")
    script = "\n".join(
        [
            "GREEN=; YELLOW=; RED=; NC=; MANIFEST_BORROWED=0",
            f'curl() {{ while [ "$1" != -o ]; do shift; done; printf %s "{body}" > "$2"; }}',
            extract_function(MANAGER, "update_download_artifact"),
            'update_download_artifact keep.txt "" https://x main',
        ]
    )
    return _bash(script, tmp_path)


def test_empty_download_body_does_not_replace_working_file(tmp_path):
    result = _download(tmp_path, "")
    assert result.returncode == 1, result.stdout
    assert (tmp_path / "keep.txt").read_text() == "working\n"
    assert not (tmp_path / "keep.txt.new").exists()


def test_nonempty_download_body_replaces_file(tmp_path):
    result = _download(tmp_path, "fresh")
    assert result.returncode == 0, result.stdout
    assert (tmp_path / "keep.txt").read_text() == "fresh"


def test_plain_backup_dump_is_owner_only(tmp_path):
    script = "\n".join(
        [
            "umask 022",
            'docker() { echo "-- dump"; return 0; }',
            'os_export_speaker_indices() { echo v > "$2"; }',
            'voiceprint_artifact_path() { echo "$1.voiceprints.json"; }',
            extract_function(COMMON, "backup_database"),
            'backup_database "-f x.yml" "./opentranscribe.sh"',
        ]
    )
    result = _bash(script, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    files = list((tmp_path / "backups").iterdir())
    assert len(files) == 2
    for f in files:
        assert f.stat().st_mode & 0o077 == 0, f"{f.name} is group/world accessible"


def _scratch_docker_stub() -> str:
    return (
        'docker() { echo "$@" >> docker.log; '
        'if [ "$1 $2" = "volume ls" ]; then echo proj_pipeline_scratch; fi; '
        'if [ "$1" = run ]; then echo 1000; fi; return 0; }'
    )


def test_common_scratch_fix_filters_by_this_projects_labels(tmp_path):
    script = "\n".join(
        [
            _scratch_docker_stub(),
            "ot_compose_project() { echo proj; }",
            extract_function(COMMON, "fix_pipeline_scratch_permissions"),
            "fix_pipeline_scratch_permissions",
        ]
    )
    _bash(script, tmp_path)
    log = (tmp_path / "docker.log").read_text()
    assert "label=com.docker.compose.project=proj" in log
    assert "label=com.docker.compose.volume=pipeline_scratch" in log


def test_common_scratch_fix_does_nothing_without_a_known_project(tmp_path):
    script = "\n".join(
        [
            _scratch_docker_stub(),
            extract_function(COMMON, "fix_pipeline_scratch_permissions"),
            "fix_pipeline_scratch_permissions",
        ]
    )
    _bash(script, tmp_path)
    assert not (tmp_path / "docker.log").exists()


def test_standalone_scratch_fallback_uses_the_same_project_scoping(tmp_path):
    script = "\n".join(
        [
            "BLUE=; YELLOW=; NC=; CONTAINER_UID_GID=1000:999",
            _scratch_docker_stub(),
            "COMPOSE_PROJECT_NAME=Other_Proj",
            extract_function(MANAGER, "preflight_fix_scratch_permissions"),
            "preflight_fix_scratch_permissions",
        ]
    )
    _bash(script, tmp_path)
    log = (tmp_path / "docker.log").read_text()
    assert "label=com.docker.compose.project=other_proj" in log
    assert "label=com.docker.compose.volume=pipeline_scratch" in log
