"""Start an IdP overlay (LLDAP / Keycloak) for a test session, and give back only what it took.

``test_ldap_oidc.py`` brings the two identity providers up itself when nothing is listening.
The stack may already have them, though: ``run-dev-tests.sh`` and ``./opentr.sh start dev
--with-ldap-test`` start them as part of the stack and own their teardown. The rule here is
**leave things as the test found them**:

* the port was already open  -> the stack (or an operator) owns it; touch nothing, ever;
* it was closed and the service had NO container -> the test created it, so ``down`` it
  (containers and the network, never ``-v``: a named volume may predate this run and holds
  state this module cannot prove is ours);
* it was closed but a STOPPED container existed -> the test merely started someone else's
  container, so ``stop`` it again and leave it where it was.

Only compose files named explicitly are passed to docker, always with ``-p <project>`` (the
stack under test: ``COMPOSE_PROJECT_NAME``, else the one ``scripts/lib/stack_target.py``
resolves) -- never a bare ``docker compose`` or a name grep -- so another project's containers
are out of reach by construction.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tests.stack_preflight import load_stack_target


@dataclass(frozen=True)
class OverlayHandle:
    """What ``start_overlay`` did, so ``stop_overlay`` can undo exactly that."""

    compose_file: str
    services: tuple[str, ...]
    cwd: Path
    started_by_test: bool
    container_existed: bool
    project: str = ""


def _project() -> str:
    """The compose project the overlay belongs to: the exported one, else the resolved stack's."""
    explicit = os.environ.get("COMPOSE_PROJECT_NAME")
    if explicit:
        return explicit
    return str(load_stack_target().resolve().project)


def _compose(cwd: Path, project: str, compose_file: str, *args: str, check: bool = True):
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", compose_file, *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def start_overlay(
    compose_file: str,
    services: tuple[str, ...],
    *,
    cwd: Path,
    port: int,
    is_open: Callable[[str, int], bool],
) -> OverlayHandle:
    """Ensure the overlay is up; record whether this call is what brought it up."""
    if is_open("localhost", port):
        return OverlayHandle(compose_file, services, cwd, False, True)
    project = _project()
    existing = _compose(cwd, project, compose_file, "ps", "-a", "-q", *services, check=False)
    existed = bool(existing.stdout.strip())
    _compose(cwd, project, compose_file, "up", "-d", *services)
    return OverlayHandle(compose_file, services, cwd, True, existed, project)


def stop_overlay(handle: OverlayHandle) -> None:
    """Undo ``start_overlay``. A no-op unless the test is what started the overlay."""
    if not handle.started_by_test:
        return
    if handle.container_existed:
        _compose(
            handle.cwd, handle.project, handle.compose_file, "stop", *handle.services, check=False
        )
    else:
        _compose(handle.cwd, handle.project, handle.compose_file, "down", check=False)
