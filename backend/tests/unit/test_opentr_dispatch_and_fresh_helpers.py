"""opentr.sh dispatch exit status, flag strictness and the fresh-deployment helpers.

* The script ended in an unconditional ``exit 0`` so a failing ``healthcheck-all`` or
  ``restart-*`` (whose functions return non-zero) still reported success to CI.
* An unrecognised flag (a typo of ``--fresh``) was only warned about, so ``start``/``reset``
  carried on against the LIVE stack.
* ``shell`` fell through to ``/bin/sh`` whenever the user left bash with a non-zero status.
* ``fresh_prepare_diar_native_models_dir`` handed docker a relative bind source (docker treats
  that as a volume name and refuses it), so the ownership fix never ran.
* ``fresh_generate_baked_overlay`` captured compose's stderr into the JSON it parses, so one
  compose warning silently produced an overlay with no services.
* The Authentik banner printed the operator's real bootstrap password.

opentr.sh is run from a scratch directory (``scripts`` symlinked in) so its worktree
``.env`` auto-link never fires.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import REPO_ROOT
from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit

OPENTR = REPO_ROOT / "opentr.sh"


def _scratch(tmp_path: Path, docker_body: str) -> tuple[Path, dict[str, str]]:
    (tmp_path / "scripts").symlink_to(REPO_ROOT / "scripts")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text("#!/bin/bash\n" + docker_body)
    fake.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
    }
    return tmp_path, env


_FAKE = (
    'echo "$@" >> "$DOCKER_LOG"\n'
    'case "$1" in\n'
    "  info) echo nvidia; exit 0;;\n"
    '  compose) for a in "$@"; do\n'
    '    case "$a" in exec|restart) exit 1;; esac\n'
    "  done; exit 0;;\n"
    "esac\n"
    "exit 0\n"
)


def _opentr(cwd: Path, env: dict[str, str], *args: str, stdin: str = ""):
    return subprocess.run(
        ["bash", str(OPENTR), *args],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("command", ["healthcheck-all", "restart-frontend"])
def test_a_failing_command_exits_non_zero(tmp_path, command):
    cwd, env = _scratch(tmp_path, _FAKE)
    proc = _opentr(cwd, env, command)
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_a_successful_command_still_exits_zero(tmp_path):
    cwd, env = _scratch(tmp_path, "exit 0\n")
    proc = _opentr(cwd, env, "healthcheck-all")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_start_refuses_an_unknown_flag(tmp_path):
    cwd, env = _scratch(tmp_path, _FAKE)
    proc = _opentr(cwd, env, "start", "dev", "--dry-run", "--frsh", "t1")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "Unknown flag: --frsh" in proc.stdout + proc.stderr


def test_reset_refuses_an_unknown_flag_before_touching_docker_volumes(tmp_path):
    cwd, env = _scratch(tmp_path, _FAKE)
    proc = _opentr(cwd, env, "reset", "dev", "--frsh", "t1", stdin="y\n")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    log = (cwd / "docker.log").read_text() if (cwd / "docker.log").exists() else ""
    assert " down" not in log
    assert "-v" not in log.split()


@pytest.mark.parametrize("command", ["reset", "remove", "purge"])
def test_a_declined_confirmation_is_not_reported_as_success(tmp_path, command):
    cwd, env = _scratch(tmp_path, "exit 0\n")
    proc = _opentr(cwd, env, command, "dev" if command == "reset" else "", stdin="n\n")
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_shell_does_not_fall_back_to_sh_when_bash_exits_non_zero(tmp_path):
    body = (
        'echo "$@" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        '  *"exec"*"/bin/bash"*) exit 1;;\n'
        '  *"exec"*"command -v bash"*) exit 0;;\n'
        "esac\n"
        "exit 0\n"
    )
    cwd, env = _scratch(tmp_path, body)
    _opentr(cwd, env, "shell", "backend")
    assert "/bin/sh" not in (cwd / "docker.log").read_text()


def test_diar_native_models_dir_ownership_fix_gets_an_absolute_bind_source(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text('#!/bin/bash\necho "$@" >> "$DOCKER_LOG"\nexit 0\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
    }
    # an owner other than 1000 forces the docker path
    script = "\n".join(
        [
            "stat() { echo 0; }",
            extract_function(OPENTR, "fresh_prepare_diar_native_models_dir"),
            "fresh_prepare_diar_native_models_dir .fresh/t1/diar-native-models",
        ]
    )
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=False)
    run_line = next(
        line
        for line in (tmp_path / "docker.log").read_text().splitlines()
        if line.startswith("run")
    )
    mount = run_line.split("-v ")[1].split()[0]
    assert mount.startswith("/"), run_line


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq required by the function under test")
def test_baked_overlay_survives_compose_warnings_on_stderr(tmp_path):
    body = (
        'if [ "$1" = compose ]; then\n'
        '  echo "WARN[0000] The \\"FOO\\" variable is not set." >&2\n'
        '  echo \'{"services":{"backend":{"volumes":[{"source":"/m","target":"/models"}],'
        '"command":["uvicorn","--reload"]}}}\'\n'
        "fi\n"
        "exit 0\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text("#!/bin/bash\n" + body)
    fake.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    script = "\n".join(
        [
            'FRESH_OVERLAY_DIR=".fresh"',
            "FRESH_BAKED_SERVICES=(backend)",
            extract_function(OPENTR, "fresh_generate_baked_overlay"),
            'fresh_generate_baked_overlay t1 "-f docker-compose.yml"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    overlay = (tmp_path / ".fresh" / "t1-baked.yml").read_text()
    assert "  backend:" in overlay, proc.stdout + proc.stderr + overlay


def test_authentik_banner_never_prints_a_configured_password(tmp_path):
    script = "\n".join(
        [
            extract_function(OPENTR, "overlay_banner"),
            "AUTHENTIK_BOOTSTRAP_PASSWORD=Sup3rSecretValue overlay_banner authentik",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Sup3rSecretValue" not in proc.stdout
    assert "AUTHENTIK_BOOTSTRAP_PASSWORD" in proc.stdout
