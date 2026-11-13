"""The gate's stack probe must RESOLVE the stack, then probe the ports st_target observed.

``run-integration-tests.sh`` probed 5176/5178/5180 regardless of the ports the tests were about
to use, so a gate aimed at an isolated stack passed its precondition on whatever else listened
on the defaults (another project's Postgres) and then ran against the wrong database. The fix is
resolution through ``scripts/lib/stack-target.sh`` (compose labels, observed ports); resolution
itself is tested in ``test_stack_target.py``.

The REAL ``# --- BEGIN stack-probe ---`` block is extracted and run in bash against sockets this
test opens, with a recording stand-in for the library (``SCRIPT_DIR/lib/stack-target.sh``), so the
control flow, the messages and the exact arguments handed to the library are the script's own.
"""

from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"

STUB_LIB = """
st_target() { echo "st_target $*" >> "$STUB_LOG"; return "${STUB_TARGET_RC:-0}"; }
st_require() { echo "st_require $*" >> "$STUB_LOG"; return "${STUB_REQUIRE_RC:-0}"; }
"""

HARNESS = """
RED=''; GREEN=''; YELLOW=''; NC=''
ST_PROJECT=otfresh-fake
TARGET_ARGS=(${STUB_TARGET_ARGS:-})
SCRIPT_DIR="$STUB_DIR"
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


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "lib").mkdir(exist_ok=True)
    (tmp_path / "lib" / "stack-target.sh").write_text(STUB_LIB, encoding="utf-8")
    script = HARNESS + _block() + '\necho "INCOMPLETE=${STACK_INCOMPLETE[*]}"\n'
    # A minimal environment: the ambient POSTGRES_PORT etc. must not leak into the probe.
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin",
            "STUB_DIR": str(tmp_path),
            "STUB_LOG": str(tmp_path / "calls.log"),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_probe_checks_the_observed_ports_not_the_defaults(tmp_path: Path):
    with _listening() as pg, _listening() as s3:
        os_port = _closed_port()
        result = _run(
            tmp_path, POSTGRES_PORT=str(pg), MINIO_PORT=str(s3), OPENSEARCH_PORT=str(os_port)
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Postgres:   up ({pg})" in result.stdout
    assert f"MinIO:      up ({s3})" in result.stdout
    assert "OpenSearch: down" in result.stdout
    assert f"INCOMPLETE=OpenSearch ({os_port})" in result.stdout


def test_the_stack_is_resolved_and_required_healthy_at_head_on_the_bind_mount(tmp_path: Path):
    with _listening() as pg:
        _run(tmp_path, POSTGRES_PORT=str(pg), STUB_TARGET_ARGS="--fresh gate1")

    assert _calls(tmp_path) == [
        "st_target --fresh gate1",
        "st_require healthy schema-head code=bind",
    ]


def test_an_unresolvable_stack_ends_the_gate_with_the_librarys_code(tmp_path: Path):
    result = _run(tmp_path, STUB_TARGET_RC="20")

    assert result.returncode == 20
    assert "Dev stack not resolved (stack-target exit 20)" in result.stdout


def test_a_failed_requirement_ends_the_gate_with_the_librarys_code(tmp_path: Path):
    result = _run(tmp_path, POSTGRES_PORT="1", STUB_REQUIRE_RC="22")

    assert result.returncode == 22


def test_a_postgres_port_with_nothing_listening_fails_the_gate_naming_that_port(tmp_path: Path):
    dead = _closed_port()

    result = _run(tmp_path, POSTGRES_PORT=str(dead))

    assert result.returncode == 1
    assert f"Postgres ({dead})" in result.stdout
    assert "Dev stack not reachable" in result.stdout


def test_the_gate_script_never_guesses_a_stack_from_a_name_or_a_stock_port():
    code = "\n".join(
        line
        for line in GATE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "name=" not in code
    assert "docker ps" not in code
    assert not re.search(r"PORT:-51\d\d", code)
