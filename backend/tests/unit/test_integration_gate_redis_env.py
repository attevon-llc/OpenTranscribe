"""The integration gate hands the stack's Redis to every phase that needs it, and to no other.

``run-integration-tests.sh`` used to resolve ``REDIS_*`` inside the integration phase only, and
only when ``REDIS_HOST`` was unset. The P3.2 gate's shell had exported ``REDIS_PORT`` (pointing
at a ``--fresh`` stack) but no password: the e2e smoke phase inherited the port with no
password and its fixtures failed with ``AuthenticationError``, and the unit phase inherited the
port too. The REAL ``# --- BEGIN stack-redis-env ---`` block is run here against a fake
``docker``, and the phase lines are read from the script, so both halves are the real ones.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"


def _block() -> str:
    text = GATE.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN stack-redis-env ---")
    end = text.index("# --- END stack-redis-env ---", start)
    return text[start : text.index("\n", end) + 1]


def _resolve(tmp_path: Path, **exported: str) -> str:
    """Run the block with a fake docker (port 5577, password ``stackpw``); return REDIS_ENV."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "ps" ]; then\n'
        '  case "$*" in\n'
        '    *service=redis*) echo "127.0.0.1:5577->6379/tcp" ;;\n'
        '    *) echo "fake-backend" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then echo stackpw; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    snippet = f"""
set -uo pipefail
YELLOW=''; NC=''
COMPOSE_PROJECT_NAME=otfresh-fake
{_block()}
echo "REDIS_ENV=${{REDIS_ENV[*]:-}}"
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("REDIS_")}
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env.update(exported)
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("REDIS_ENV="))
    return line[len("REDIS_ENV=") :]


@pytest.mark.unit
def test_nothing_exported_resolves_port_and_password_from_the_stack(tmp_path: Path) -> None:
    assert _resolve(tmp_path) == ("env REDIS_HOST=localhost REDIS_PORT=5577 REDIS_PASSWORD=stackpw")


@pytest.mark.unit
def test_an_exported_port_alone_still_gets_the_stacks_password(tmp_path: Path) -> None:
    """The P3.2 shell: REDIS_PORT exported for a --fresh stack, no password."""
    assert _resolve(tmp_path, REDIS_PORT="5977") == (
        "env REDIS_HOST=localhost REDIS_PORT=5977 REDIS_PASSWORD=stackpw"
    )


@pytest.mark.unit
def test_every_exported_value_wins(tmp_path: Path) -> None:
    assert _resolve(tmp_path, REDIS_HOST="h", REDIS_PORT="1", REDIS_PASSWORD="mine") == (
        "env REDIS_HOST=h REDIS_PORT=1 REDIS_PASSWORD=mine"
    )


def _phase_command(title: str) -> str:
    """The ``run_phase*`` invocation of the phase called ``title``, continuation lines included."""
    text = GATE.read_text(encoding="utf-8")
    match = re.search(rf'run_phase(?:_watching_skips)? "{re.escape(title)}"(?:.*\\\n)*.*', text)
    assert match, f"no run_phase invocation for {title!r}"
    return match.group(0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "title", ["Integration-marked tests", "GPU-marked tests", "E2E smoke (browser)"]
)
def test_every_phase_that_reaches_the_stack_gets_the_redis_env(title: str) -> None:
    command = _phase_command(title)
    assert re.search(r'"\$\{REDIS_ENV\[@\]\}"', command), (
        f"the {title!r} phase runs without the stack's REDIS_* (resolved by stack-redis-env)"
    )


@pytest.mark.unit
@pytest.mark.parametrize("title", ["Unit/API suite", "Security suites (FIPS_MODE=true)"])
def test_the_unit_phases_run_with_redis_unset(title: str) -> None:
    command = _phase_command(title)
    assert '"${UNIT_ENV[@]}"' in command, f"{title!r} inherits whatever REDIS_* the shell has"
    assert "REDIS_ENV" not in command


@pytest.mark.unit
def test_unit_env_unsets_every_redis_variable() -> None:
    block = _block()
    unit_env = re.search(r"^UNIT_ENV=\((.*)\)$", block, re.M)
    assert unit_env, "stack-redis-env no longer defines UNIT_ENV"
    unset = set(re.findall(r"-u (\w+)", unit_env.group(1)))
    assert {"REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "REDIS_URL"} <= unset, unset
