"""`opentr.sh backup|restore --fresh <name>` must address ONLY that fresh project.

The dispatch arms passed compose chain "" and ignored `--fresh`, so a fresh stack could not be
backed up or restored, and a restore (DROP DATABASE) run from a fresh context hit the default
(live) project. These run the real `backup_restore_run` and its real resolver helpers with
`backup_database`/`restore_database` stubbed to record the project and chain they were given,
and the real `restore_database` for the unknown-flag case.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

FUNCS = (
    "fresh_sanitize_name",
    "fresh_project_name",
    "fresh_aux_file",
    "fresh_read_aux",
    "fresh_compose_chain",
    "restart_resolve_target",
    "restart_compose",
)
TARGET_FUNCS = ("backup_restore_run", "service_cmd_run")


@pytest.fixture(autouse=True)
def _no_inherited_compose_project(monkeypatch):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)


def _extract(name: str, source: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{$", source, re.M)
    assert match, f"{name}() not found in opentr.sh"
    end = re.search(r"^\}$", source[match.start() :], re.M)
    assert end
    return source[match.start() : match.start() + end.end()]


def _run(tmp_path: Path, body: str, *, real_common: bool = False, cwd: Path = REPO_ROOT):
    source = OPENTR.read_text(encoding="utf-8")
    targets = (REPO_ROOT / "scripts" / "opentr-targets.sh").read_text(encoding="utf-8")
    defaults = "\n".join(re.findall(r'^: "\$\{[A-Za-z_][A-Za-z0-9_]*:=[^}]*\}"$', source, re.M))
    assert defaults
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir(exist_ok=True)
    (fresh_dir / "x.yml").write_text("services: {}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "docker.log"
    (bindir / "docker").write_text(
        f'#!/bin/bash\nprintf "PROJECT=%s ARGS=%s\\n" "${{COMPOSE_PROJECT_NAME:-<unset>}}" "$*" >> "{calls}"\n'
    )
    (bindir / "docker").chmod(0o755)
    stubs = (
        ""
        if real_common
        else (
            'backup_database() { echo "BACKUP project=${COMPOSE_PROJECT_NAME:-<unset>} chain=[$1] args=[${*:3}]"; }\n'
            'restore_database() { echo "RESTORE project=${COMPOSE_PROJECT_NAME:-<unset>} chain=[$1] args=[${*:3}]"; }\n'
        )
    )
    common = f'source "{COMMON}"\n' if real_common else ""
    script = (
        "set -uo pipefail\n"
        f'export PATH="{bindir}:$PATH"\nFRESH_OVERLAY_DIR="{fresh_dir}"\n'
        f'REPO="{REPO_ROOT}"\n{defaults}\n{common}{stubs}'
        + "\n".join(_extract(n, source) for n in FUNCS)
        + "\n"
        + "\n".join(_extract(n, targets) for n in TARGET_FUNCS)
        + f'\ncd "{cwd}"\n{body}\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=REPO_ROOT
    )
    return proc, (calls.read_text() if calls.exists() else "")


def test_restore_fresh_addresses_only_the_fresh_project(tmp_path):
    proc, _ = _run(tmp_path, 'cd "$REPO"; backup_restore_run restore --fresh x --yes ./b.sql')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESTORE project=otfresh-x " in proc.stdout
    assert f"{tmp_path}/fresh/x.yml" in proc.stdout  # the fresh overlay is in the chain
    assert "args=[--yes ./b.sql]" in proc.stdout  # --fresh consumed, never a file name
    assert "<unset>" not in proc.stdout


def test_backup_fresh_addresses_only_the_fresh_project(tmp_path):
    proc, _ = _run(tmp_path, 'cd "$REPO"; backup_restore_run backup --fresh x --encrypt')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BACKUP project=otfresh-x " in proc.stdout
    assert "args=[--encrypt]" in proc.stdout


def test_without_fresh_keeps_default_bare_compose(tmp_path):
    proc, _ = _run(tmp_path, 'cd "$REPO"; backup_restore_run restore --yes ./b.sql')
    assert "RESTORE project=<unset> chain=[] " in proc.stdout


def test_unknown_fresh_deployment_refuses_instead_of_hitting_default(tmp_path):
    proc, _ = _run(tmp_path, 'cd "$REPO"; backup_restore_run restore --fresh nope ./b.sql')
    assert proc.returncode != 0
    assert "RESTORE" not in proc.stdout
    assert "No fresh deployment 'nope'" in proc.stderr


def test_fresh_without_name_refuses(tmp_path):
    proc, _ = _run(tmp_path, 'cd "$REPO"; backup_restore_run backup --fresh')
    assert proc.returncode != 0
    assert "BACKUP" not in proc.stdout


@pytest.mark.parametrize("action", ["restore", "backup"])
def test_unknown_flag_errors_and_never_reaches_docker(tmp_path, action):
    work = tmp_path / "work"
    work.mkdir()
    proc, calls = _run(
        tmp_path, f"backup_restore_run {action} --bogus ./b.sql", real_common=True, cwd=work
    )
    assert proc.returncode != 0
    assert "unknown" in (proc.stdout + proc.stderr).lower()
    assert calls == "", f"docker was invoked: {calls}"


def test_logs_and_shell_fresh_are_pinned_to_the_fresh_project(tmp_path):
    proc, calls = _run(
        tmp_path,
        'cd "$REPO"; service_cmd_run logs backend --fresh x; service_cmd_run shell --fresh x',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = calls.strip().splitlines()
    assert lines and all(line.startswith("PROJECT=otfresh-x ") for line in lines), calls
    assert "logs -f backend" in calls and "exec backend /bin/" in calls


def test_logs_without_fresh_stays_on_default_project_with_positional_service(tmp_path):
    proc, calls = _run(tmp_path, 'cd "$REPO"; service_cmd_run logs backend')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls.strip() == "PROJECT=<unset> ARGS=compose logs -f backend"


def test_logs_unknown_flag_and_unknown_fresh_name_never_reach_docker(tmp_path):
    for body in ("service_cmd_run logs --bogus", "service_cmd_run logs --fresh nope"):
        proc, calls = _run(tmp_path, f'cd "$REPO"; {body}')
        assert proc.returncode != 0
        assert calls == ""
