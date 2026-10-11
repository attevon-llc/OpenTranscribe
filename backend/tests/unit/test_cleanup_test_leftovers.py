"""``scripts/cleanup-test-leftovers.sh`` (``./opentr.sh test-cleanup``), driven for real.

The script runs unattended against a live stack, so each of its safety claims is exercised
end to end with a fake ``docker`` and a fake sweep interpreter on ``PATH``/``OT_VENV_PY`` —
the real bash runs, nothing reaches a daemon or a database:

* it refuses to guess a stack, and refuses a project with no running postgres + backend;
* every port and credential comes from the named project's own containers;
* ``--dry-run`` never passes a delete flag, and a real pass passes only the Tier A one;
* the age floor reaches the sweep;
* the lock stops two passes overlapping;
* ``--stop`` signals only this project's own loop, never another pid in its pid file.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-leftovers.sh"
_PROJECT = "otfresh-pytest-leftovers"
_BASH = shutil.which("bash") or "/bin/bash"
_SLEEP = shutil.which("sleep") or "/bin/sleep"

_FAKE_DOCKER = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"service=postgres"*"{{.Label"*) [ "${FAKE_NO_STACK:-}" = 1 ] || echo "$FAKE_PROJECT" ;;
  "ps "*"project=$FAKE_PROJECT"*"service=postgres"*) [ "${FAKE_NO_STACK:-}" = 1 ] || echo pg-c ;;
  "ps "*"project=$FAKE_PROJECT"*"service=backend"*) [ "${FAKE_NO_STACK:-}" = 1 ] || echo be-c ;;
  "ps "*"project=$FAKE_PROJECT"*"service=opensearch"*) echo os-c ;;
  "ps "*"project=$FAKE_PROJECT"*"service=minio"*) echo minio-c ;;
  "ps "*) ;;
  "port pg-c 5432/tcp") echo 127.0.0.1:6376 ;;
  "port be-c 8080/tcp") echo 127.0.0.1:6374 ;;
  "port os-c 9200/tcp") echo 127.0.0.1:6380 ;;
  "port minio-c 9000/tcp") echo 127.0.0.1:6378 ;;
  "exec pg-c printenv POSTGRES_PASSWORD") echo fake-pg-password ;;
  "exec "*" printenv "*) echo fake-value ;;
esac
exit 0
"""

#: Stands in for backend/venv/bin/python: records argv + the env the sweep would see, and
#: prints a report shaped like cleanup-test-data.py --json. ``-c`` (the summary helper) is
#: delegated to the real interpreter.
_FAKE_PYTHON = r"""#!/bin/bash
if [ "${1:-}" = "-c" ]; then exec "$REAL_PYTHON" "$@"; fi
{
  printf 'ARGV %s\n' "$*"
  printf 'ENV POSTGRES_PORT=%s OPENSEARCH_PORT=%s MINIO_PORT=%s E2E_BACKEND_URL=%s\n' \
    "${POSTGRES_PORT:-}" "${OPENSEARCH_PORT:-}" "${MINIO_PORT:-}" "${E2E_BACKEND_URL:-}"
} >> "$FAKE_SWEEP_LOG"
sleep "${FAKE_SWEEP_SECONDS:-0}"
echo "--- JSON report ---"
echo '{"resources": {"speaker_profile": {"count": 1, "deleted": 1}}}'
"""


def _exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def harness(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "docker", _FAKE_DOCKER)
    python = _exe(tmp_path / "fake-python", _FAKE_PYTHON)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_SWEEP_LOG": str(tmp_path / "sweep.log"),
        "FAKE_PROJECT": _PROJECT,
        "OT_VENV_PY": str(python),
        "REAL_PYTHON": sys.executable,
        "OT_TEST_CLEANUP_DIR": str(tmp_path / "state"),
    }
    return {"env": env, "tmp": tmp_path}


def _run(harness: dict, *args: str, extra: dict | None = None, timeout: int = 60):
    return subprocess.run(  # noqa: S603 - fixed argv: the script under test
        [_BASH, str(_SCRIPT), *args],
        env={**harness["env"], **(extra or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sweep_calls(harness: dict) -> list[str]:
    log = harness["tmp"] / "sweep.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.unit
def test_without_a_named_stack_it_refuses_and_sweeps_nothing(harness: dict) -> None:
    result = _run(harness)
    assert result.returncode == 2
    assert "refusing to guess" in result.stderr and _PROJECT in result.stderr
    assert _sweep_calls(harness) == []


@pytest.mark.unit
def test_a_project_with_no_running_stack_is_refused(harness: dict) -> None:
    result = _run(harness, "--project", _PROJECT, extra={"FAKE_NO_STACK": "1"})
    assert result.returncode == 2
    assert "refusing" in result.stderr
    assert _sweep_calls(harness) == []


@pytest.mark.unit
def test_a_dry_run_passes_no_delete_flag(harness: dict) -> None:
    result = _run(harness, "--project", _PROJECT, "--dry-run")
    assert result.returncode == 0, result.stderr
    argv = [line for line in _sweep_calls(harness) if line.startswith("ARGV")]
    assert len(argv) == 1
    assert "--execute" not in argv[0], "a dry run must not pass any delete flag"


@pytest.mark.unit
def test_a_real_pass_is_tier_a_only_and_scoped_to_the_named_stack(harness: dict) -> None:
    result = _run(harness, "--fresh", "pytest-leftovers")
    assert result.returncode == 0, result.stderr
    calls = _sweep_calls(harness)
    argv = next(line for line in calls if line.startswith("ARGV"))
    assert "--execute-unambiguous" in argv.split()
    assert "--execute" not in argv.split(), "Tier B is never swept unattended"
    env = next(line for line in calls if line.startswith("ENV"))
    # Every port is the named project's own published port.
    assert "POSTGRES_PORT=6376" in env and "OPENSEARCH_PORT=6380" in env
    assert "MINIO_PORT=6378" in env and "E2E_BACKEND_URL=http://localhost:6374" in env
    docker = (harness["tmp"] / "docker.log").read_text(encoding="utf-8").splitlines()
    container_lookups = [c for c in docker if c.startswith("ps ") and "{{.Names}}" in c]
    assert container_lookups and all(
        f"label=com.docker.compose.project={_PROJECT}" in c for c in container_lookups
    ), docker
    summary = (harness["tmp"] / "state" / f"{_PROJECT}.log").read_text(encoding="utf-8")
    assert "speaker_profile 1 (deleted 1)" in summary


@pytest.mark.unit
def test_the_age_floor_reaches_the_sweep(harness: dict) -> None:
    _run(harness, "--project", _PROJECT, "--dry-run")
    _run(harness, "--project", _PROJECT, "--dry-run", "--min-age-minutes", "90")
    argv = [line.split() for line in _sweep_calls(harness) if line.startswith("ARGV")]
    floors = [a[a.index("--min-age-minutes") + 1] for a in argv]
    assert floors == ["30", "90"], "default 30, and the operator's value when given"


@pytest.mark.unit
def test_a_held_lock_stops_an_overlapping_pass(harness: dict) -> None:
    state = harness["tmp"] / "state"
    state.mkdir()
    lock = state / f"{_PROJECT}.lock"
    fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = _run(harness, "--project", _PROJECT)
    finally:
        os.close(fd)
    assert result.returncode == 3
    assert "not overlapping" in result.stderr
    assert _sweep_calls(harness) == []


@pytest.mark.unit
def test_stop_refuses_to_signal_a_pid_that_is_not_its_own_loop(harness: dict) -> None:
    state = harness["tmp"] / "state"
    state.mkdir()
    stranger = subprocess.Popen([_SLEEP, "300"])  # noqa: S603 - a harmless bystander
    try:
        (state / f"{_PROJECT}.pid").write_text(f"{stranger.pid}\n", encoding="utf-8")
        result = _run(harness, "--project", _PROJECT, "--stop")
        assert result.returncode == 1
        assert "refusing to signal" in result.stderr
        assert stranger.poll() is None, "the bystander must still be running"
    finally:
        stranger.terminate()
        stranger.wait(timeout=10)


@pytest.mark.unit
def test_background_loop_status_and_stop(harness: dict) -> None:
    state = harness["tmp"] / "state"
    pid_file = state / f"{_PROJECT}.pid"
    started = _run(harness, "--project", _PROJECT, "--background", "--interval-minutes", "60")
    assert started.returncode == 0, started.stderr
    pid = 0
    try:
        for _ in range(150):
            if pid_file.exists():
                break
            time.sleep(0.1)
        pid = int(pid_file.read_text(encoding="utf-8").split()[0])
        # A second loop for the same project is refused while the first lives.
        again = _run(harness, "--project", _PROJECT, "--background")
        assert again.returncode == 3, again.stdout + again.stderr

        status = _run(harness, "--project", _PROJECT, "--status")
        assert f"running: pid {pid}" in status.stdout

        stopped = _run(harness, "--project", _PROJECT, "--stop")
        assert stopped.returncode == 0, stopped.stderr
        assert f"stopped loop pid {pid}" in stopped.stdout
        assert not pid_file.exists()
        assert "not running" in _run(harness, "--project", _PROJECT, "--status").stdout
    finally:
        if pid and _alive(pid):
            os.kill(pid, signal.SIGTERM)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.mark.unit
def test_the_summary_parser_reads_the_sweeps_report() -> None:
    """The summary is the only thing an operator reads per pass; it must not silently say
    'nothing to remove' when the report holds candidates."""
    report = {"resources": {"indexes": {"tier_a": ["a", "b"], "deleted": 0}}}
    text = "noise\n--- JSON report ---\n" + json.dumps(report)
    source = _SCRIPT.read_text(encoding="utf-8")
    body = source.split("summarize() {", 1)[1].split("\n}\n", 1)[0]
    code = body.split("-c '", 1)[1].rsplit("'", 1)[0]
    result = subprocess.run(  # noqa: S603 - the script's own embedded summary program
        [sys.executable, "-c", code],
        input=text,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "indexes 2"
