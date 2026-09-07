"""The ONE sealed way to run ``scripts/lib/dev-test-overlays.sh`` from a test.

Sourcing that library is the whole value of the tests that use it — a grep for a table key
proves the key is spelled right, not that anything reads it. But sourcing it also arms two
things that reach outside the test:

1. ``overlay_container_name`` (``scripts/lib/compose-project.sh``) runs a real ``docker ps``
   scoped to the LIVE compose project, which it resolves from the running postgres container.
2. The library installs ``trap teardown_overlays EXIT`` at source time, and
   ``teardown_overlays`` runs ``docker stop`` on every container ``setup_overlays`` named.

Together those made ``tests/unit/test_dev_test_overlay_recreate_keeps_env.py`` **kill the live
``opentranscribe-mock-llm`` container on every run of the unit suite**. ``mock-llm-server.py``
is PID 1 and ignores SIGTERM, so each of the three tests paid ``docker stop``'s 10 s grace and
then SIGKILLed it — measured 11.24/11.11/11.09 s in the 2026-09-06 gate, container dead four
minutes into a 38-minute run, and 6 integration + 26 e2e tests silently skipped downstream for
want of the mock they needed.

So: never hand-roll the preamble. ``sealed_script()`` puts a fake ``docker`` first on ``PATH``
and neutralises the EXIT trap, and ``docker_calls()`` lets a test ASSERT the containment rather
than intend it — the same rule issue #693 imposed on
``tests/unit/test_opentr_stop_container_scoping.py``.
``tests/unit/test_overlay_lib_tests_are_docker_sealed.py`` fails if a test module sources the
library without going through here.

The shim ANSWERS lookups with sentinels rather than swallowing them, so the library walks the
same code path a live hit produces (project resolved, container named, ownership recorded) with
nothing real on the other end. That is what makes both halves of the seal falsifiable: with
``PATH`` interception broken the project comes back as the live one instead of
``sealed-project``; with the trap left installed a ``stop sealed-container`` appears in the log.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_LIB = REPO_ROOT / "scripts" / "lib" / "dev-test-overlays.sh"

SEALED_BIN_DIRNAME = "sealed-bin"
DOCKER_LOG_NAME = "docker-argv.txt"
SEALED_PROJECT = "sealed-project"
SEALED_CONTAINER = "sealed-container"

#: Docker verbs that change something. ``stop`` is the one that killed mock-llm; the rest are
#: listed because the next version of the library could reach for any of them.
MUTATING_DOCKER_VERBS = ("stop", "kill", "rm", "restart", "exec", "down", "up", "start", "prune")


def seal_docker(tmp_path: Path) -> Path:
    """Create the ``docker`` shim directory and return it, for prepending to ``PATH``."""
    bindir = tmp_path / SEALED_BIN_DIRNAME
    bindir.mkdir(exist_ok=True)
    log = bindir / DOCKER_LOG_NAME
    shim = bindir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [[ "${1:-}" == "ps" ]]; then\n'
        '    if [[ "$*" == *"compose.service=postgres"* ]]; then\n'
        f'        echo "{SEALED_PROJECT}"\n'
        "    else\n"
        f'        echo "{SEALED_CONTAINER}"\n'
        "    fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bindir


def docker_calls(tmp_path: Path) -> list[str]:
    """Every argv the sealed ``docker`` shim recorded under ``tmp_path``."""
    log = tmp_path / SEALED_BIN_DIRNAME / DOCKER_LOG_NAME
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def mutating_docker_calls(tmp_path: Path) -> list[str]:
    """The recorded calls that would have changed a container's state."""
    return [
        c for c in docker_calls(tmp_path) if c.split() and c.split()[0] in MUTATING_DOCKER_VERBS
    ]


def sealed_script(tmp_path: Path, repo_root: Path | str, body: str) -> str:
    """A bash script that sources the overlay library with docker sealed off.

    ``body`` runs after the ``source``, so it is where a caller overrides functions (bash keeps
    the last definition), injects state, and calls the function under test.

    Args:
        tmp_path: the test's ``tmp_path``; the shim and its log live under it.
        repo_root: value for ``REPO_ROOT`` — a fake root for tests that stub ``opentr.sh``,
            the real one for tests that only read the table.
        body: bash to run after sourcing.

    Returns:
        The full script, ending with ``trap - EXIT`` so ``teardown_overlays`` cannot fire.
    """
    preamble = textwrap.dedent(f"""
        set -uo pipefail
        PATH="{seal_docker(tmp_path)!s}:$PATH"
        REPO_ROOT={repo_root!s}
        VENV_PY=/nonexistent/python
        AUTH_CONFIG_CLI=/nonexistent/cli.py
        RED='' GREEN='' YELLOW='' NC=''
        EXIT_PRECONDITION=3
        RUN_BACKEND=true RUN_E2E=false
        ALL_OVERLAYS=false NO_OVERLAYS=false WITH_GPU_SCALE=false

        source "{OVERLAY_LIB!s}"
    """)
    return preamble + textwrap.dedent(body) + "\ntrap - EXIT\n"
