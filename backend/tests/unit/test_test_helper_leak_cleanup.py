"""A SIGKILLed test run must not leave its helpers behind.

Covers the mock LLM child (``--exit-with-parent``) and the stale throwaway Postgres reaper in
``scripts/lib/throwaway-pg.sh`` (``tpg_reap_stale``), with docker stubbed.
"""

from __future__ import annotations

import os
import socket
import subprocess  # fixed argv, tmp paths  # nosec B404
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER = REPO_ROOT / "scripts" / "mock-llm-server.py"
LIB = REPO_ROOT / "scripts" / "lib" / "throwaway-pg.sh"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn(extra: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, str(SERVER), "--port", str(_free_port()), *extra])  # nosec B603


def _wait_gone(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _doomed_parent() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # nosec B603


def test_mock_llm_exits_when_its_parent_is_sigkilled() -> None:
    parent = _doomed_parent()
    child = _spawn(["--exit-with-parent", str(parent.pid)])
    try:
        assert not _wait_gone(child, 1.5)  # parent alive: keeps serving
        parent.kill()
        parent.wait()
        assert _wait_gone(child, 10)
    finally:
        child.kill()
        parent.kill()


def test_mock_llm_without_the_flag_keeps_running() -> None:
    """Control: the compose-service path (no flag) is unchanged."""
    parent = _doomed_parent()
    child = _spawn([])
    try:
        parent.kill()
        parent.wait()
        assert not _wait_gone(child, 2)
    finally:
        child.kill()


def _reap(tmp_path: Path, listing: str, created: str = "") -> list[str]:
    """Run tpg_reap_stale against a stub docker; return the names it ``rm -f``'d."""
    log = tmp_path / "docker.log"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text(
        f'#!/bin/bash\necho "$*" >> "{log}"\n'
        'if [[ "$1 $2" == "container ls" ]]; then cat "$STUB_LIST"; fi\n'
        f'if [[ "$1" == inspect ]]; then echo "{created}"; fi\n'
    )
    stub.chmod(0o755)
    (tmp_path / "list").write_text(listing)
    subprocess.run(  # nosec B603 B607
        ["bash", "-c", f'source "{LIB}"; tpg_reap_stale'],
        env={
            **os.environ,
            "PATH": f"{stub.parent}:{os.environ['PATH']}",
            "STUB_LIST": str(tmp_path / "list"),
        },
        check=True,
        capture_output=True,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return [ln.split()[-1] for ln in lines if ln.startswith("rm -f")]


def test_reaper_selects_only_stale_same_shape_dead_owner(tmp_path: Path) -> None:
    now = int(time.time())
    old, fresh = now - 7 * 3600, now - 60
    proj = "opentranscribe-pytest-" + "a" * 32
    me = os.getpid()
    my_start = Path(f"/proc/{me}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    rows = [
        f"ot-testdb-{'1' * 12}|999999|1|{old}|{proj}",  # stale, owner gone -> reap
        f"ot-testdb-{'2' * 12}|{me}|{my_start}|{old}|{proj}",  # owner alive -> keep
        f"ot-testdb-{'3' * 12}|999999|1|{fresh}|{proj}",  # too young -> keep
        f"cvat_db|999999|1|{old}|{proj}",  # other name shape -> keep
        f"ot-testdb-{'4' * 12}|999999|1|{old}|opentranscribe-pytest-short",  # other project -> keep
        f"ot-testdb-{'5' * 12}|999999|1|{old}|myapp",  # other project -> keep
        f"ot-testdb-{'6' * 12}|{me}|1|{old}|{proj}",  # pid reused (start differs) -> reap
    ]
    assert _reap(tmp_path, "\n".join(rows) + "\n") == [
        f"ot-testdb-{'1' * 12}",
        f"ot-testdb-{'6' * 12}",
    ]


def test_reaper_handles_unlabelled_legacy_container_by_docker_age(tmp_path: Path) -> None:
    proj = "opentranscribe-pytest-" + "b" * 32
    row = f"ot-testdb-{'7' * 12}|||  |{proj}\n".replace("|  |", "||")
    assert _reap(tmp_path, row, created="2020-01-01T00:00:00Z") == [f"ot-testdb-{'7' * 12}"]
