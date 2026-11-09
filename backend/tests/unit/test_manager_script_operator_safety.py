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
from tests.unit._shell_extract import REPO_ROOT
from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit

OPENTR = REPO_ROOT / "opentr.sh"


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


def _watch_chown(tmp_path: Path, target: str) -> str:
    script = "\n".join(
        [
            'chown() { echo "chown $*" >> chown.log; }',
            f"HOME={tmp_path}/home",
            f'mkdir -p "{tmp_path}/home" "{tmp_path}/data/watch"',
            extract_function(OPENTR, "chown_watch_folder"),
            f'chown_watch_folder "{target}"',
        ]
    )
    _bash(script, tmp_path)
    log = tmp_path / "chown.log"
    return log.read_text() if log.exists() else ""


def test_watch_folder_chown_refuses_root_home_and_top_level(tmp_path):
    assert _watch_chown(tmp_path, "/") == ""
    assert _watch_chown(tmp_path, str(tmp_path / "home")) == ""
    assert _watch_chown(tmp_path, "/usr") == ""


def test_watch_folder_chown_applies_to_a_dedicated_folder(tmp_path):
    assert str(tmp_path / "data" / "watch") in _watch_chown(
        tmp_path, str(tmp_path / "data" / "watch")
    )


def test_stack_build_and_up_exit_codes_are_checked():
    needles = (
        'docker compose $COMPOSE_FILES up -d --build --no-deps "$gpu_service"',
        "docker compose $BENCH_COMPOSE up -d --build",
        "docker compose $BENCH_COMPOSE build",
        "docker compose $BENCH_COMPOSE up -d --no-build",
    )
    lines = OPENTR.read_text().splitlines()
    sites = [ln.strip() for ln in lines if any(n in ln for n in needles)]
    unchecked = [ln for ln in sites if "if ! " not in ln]
    assert len(sites) == len(needles)
    assert unchecked == []


def test_migrate_step_runs_even_when_compose_config_gets_sigpipe(tmp_path):
    # A `config --services` that keeps writing after grep -q has its answer dies of SIGPIPE;
    # under pipefail that inverted a match into "no migrate service" and skipped migrations.
    script = "\n".join(
        [
            "set -o pipefail",
            "docker() {",
            '  if [ "$4" = config ]; then echo migrate; exec seq 1 300000; fi',
            '  echo "ran: $*" >> docker.log; return 0',
            "}",
            extract_function(COMMON, "compose_run_migrate"),
            'compose_run_migrate "-f x.yml"',
        ]
    )
    result = _bash(script, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run --rm --no-deps migrate" in (tmp_path / "docker.log").read_text()


# --- SHe: purge scoping, fresh-destroy exact project match, model perms, NLTK status ---

SETUP = REPO_ROOT / "setup-opentranscribe.sh"


def _fake_docker(tmp_path: Path, body: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text("#!/bin/bash\n" + body)
    fake.chmod(0o755)
    return {"PATH": f"{bindir}:/usr/bin:/bin"}


def _run_with_env(script: str, cwd: Path, env: dict[str, str]):
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False, env=env
    )


def test_purge_system_never_removes_fresh_stack_or_foreign_images(tmp_path):
    env = _fake_docker(
        tmp_path,
        'echo "$@" >> docker.log\n'
        'case "$1 $2" in\n'
        '  "compose -f") echo "davidamacey/opentranscribe-backend:latest";;\n'
        '  "images --filter")\n'
        '    case "$*" in\n'
        '      *reference=*) echo "SHOULD-NOT-BE-ASKED-FOR-GLOBS";;\n'
        '      *label=com.docker.compose.project=opentranscribe*) echo "opentranscribe-frontend:otfresh-t1"; echo "opentranscribe-docs:dev";;\n'
        "    esac;;\n"
        "esac\n",
    )
    script = "\n".join(
        [
            "stop_all_containers() { :; }",
            extract_function(OPENTR, "purge_system"),
            "purge_system",
        ]
    )
    _run_with_env(script, tmp_path, env)
    log = (tmp_path / "docker.log").read_text()
    rmi = [ln for ln in log.splitlines() if ln.startswith("rmi")]
    assert rmi, log
    removed = " ".join(rmi)
    assert "davidamacey/opentranscribe-backend:latest" in removed
    assert "opentranscribe-docs:dev" in removed
    assert "otfresh-t1" not in removed
    assert "SHOULD-NOT" not in removed
    assert "reference=" not in log


def test_fresh_destroy_selects_containers_by_exact_compose_project_label():
    src = extract_function(OPENTR, "fresh_destroy")
    assert "name=^${proj}-" not in src
    assert 'docker ps -a --filter "label=com.docker.compose.project=${proj}"' in src


def test_model_cache_perms_are_dirs_755_files_644_and_do_not_abort(tmp_path):
    models = tmp_path / "models"
    for d in (
        "huggingface",
        "torch",
        "nltk_data",
        "sentence-transformers",
        "opensearch-ml",
        "diar-native",
        "password-blocklist",
    ):
        (models / d).mkdir(parents=True)
    f = models / "torch" / "w.bin"
    f.write_text("x")
    f.chmod(0o600)
    script = "\n".join(
        [
            "set -e",
            "print_warning() { echo WARN: $*; }",
            "print_info() { :; }",
            extract_function(SETUP, "prepare_model_cache_dirs"),
            "prepare_model_cache_dirs >/dev/null",
            "echo REACHED",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert "REACHED" in proc.stdout, proc.stdout + proc.stderr
    assert oct(f.stat().st_mode & 0o777) == "0o644"
    assert oct((models / "torch").stat().st_mode & 0o777) == "0o755"


def test_nltk_downloader_failure_is_surfaced_even_if_dir_has_content(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "download-models.py").write_text("")
    env = _fake_docker(
        tmp_path,
        'case "$1" in\n'
        "  run) mkdir -p models/nltk_data/tokenizers/x; echo ERROR boom; exit 3;;\n"
        "esac\n"
        "exit 0\n",
    )
    script = "\n".join(
        [
            "set -eo pipefail",
            "read_env_value() { :; }",
            extract_function(COMMON, "ensure_nltk_corpora"),
            "rc=0; ensure_nltk_corpora || rc=$?",
            'echo "RC=$rc"',
        ]
    )
    proc = _run_with_env(script, tmp_path, env)
    assert "RC=1" in proc.stdout, proc.stdout + proc.stderr
    assert "exited 3" in proc.stdout
