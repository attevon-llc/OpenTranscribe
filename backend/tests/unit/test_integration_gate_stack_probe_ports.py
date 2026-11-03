"""The gate's stack probe must follow POSTGRES_PORT / MINIO_PORT / OPENSEARCH_PORT.

``run-integration-tests.sh`` probed 5176/5178/5180 regardless of the ports the tests were about
to use, so a gate aimed at an isolated stack passed its precondition on whatever else listened
on the defaults (another project's Postgres) and then ran against the wrong database.

The REAL ``# --- BEGIN stack-probe ---`` block is extracted and run in bash against sockets this
test opens, so the control flow and the messages are the script's own.
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"

HARNESS = """
RED=''; GREEN=''; YELLOW=''; NC=''
port_open() { (exec 3<>"/dev/tcp/localhost/$1") 2>/dev/null && exec 3>&- && return 0 || return 1; }
"""


def _block() -> str:
    text = GATE.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN stack-probe ---")
    end = text.index("# --- END stack-probe ---", start)
    return text[start:end]


@contextmanager
def _listening() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        yield int(srv.getsockname()[1])


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])  # released on exit: nothing listens there


def _run(**env: str) -> subprocess.CompletedProcess[str]:
    script = HARNESS + _block() + '\necho "INCOMPLETE=${STACK_INCOMPLETE[*]}"\n'
    # A minimal environment: the ambient POSTGRES_PORT etc. must not leak into the probe.
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_probe_checks_the_exported_ports_not_the_defaults():
    with _listening() as pg, _listening() as s3:
        os_port = _closed_port()
        result = _run(POSTGRES_PORT=str(pg), MINIO_PORT=str(s3), OPENSEARCH_PORT=str(os_port))

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Postgres:   up ({pg})" in result.stdout
    assert f"MinIO:      up ({s3})" in result.stdout
    assert "OpenSearch: down" in result.stdout
    assert f"INCOMPLETE=OpenSearch ({os_port})" in result.stdout


def test_a_postgres_port_with_nothing_listening_fails_the_gate_naming_that_port():
    dead = _closed_port()

    result = _run(POSTGRES_PORT=str(dead))

    assert result.returncode == 1
    assert f"Postgres ({dead})" in result.stdout
    assert "Dev stack not reachable" in result.stdout
