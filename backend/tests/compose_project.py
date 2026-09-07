"""Resolve the compose project of the live stack UNDER TEST, and containers within it.

Python mirror of ``scripts/lib/compose-project.sh`` (issue #630), for the live-stack tests
that shell out to ``docker``. Read that file's header first: it records the two guesses about
the compose project that have already shipped bugs on the shell side.

WHY A TEST NEEDS THIS
---------------------
``docker ps --filter name=<something>`` is not scoped to a deployment. More than one
OpenTranscribe stack routinely runs on this host -- the plain dev stack plus any number of
``./opentr.sh start dev --fresh <name>`` deployments -- and every one of them contains a
container matching that filter. Taking ``[0]`` therefore selects by ``docker ps`` ordering,
which is creation time, newest first.

Measured with the dev stack and an ``otfresh-demo`` deployment both up: ``--filter
name=diar-native`` returns ``transcribe-app-diar-native-1`` AND ``otfresh-demo-diar-native``,
and which one lands first is decided by whichever was restarted more recently. A test that
picks the wrong one measures a deployment it was never pointed at -- and since the two
deployments sit on different GPUs, it can report a PASS having verified nothing about the
stack under test. That is the ``readiness-probe-target`` defect class ``scripts/audit-tests.py``
exists to catch, reached from the other direction: not a hardcoded target, but an
under-specified one.

HOW THE PROJECT IS RESOLVED
---------------------------
The stack under test is the one this pytest process is configured to TALK to, so it is
derived from that, never from a directory name:

1. ``COMPOSE_PROJECT_NAME`` when set -- explicit operator intent wins, as in the shell lib.
2. The project of the ``postgres`` service container **publishing this process's
   ``POSTGRES_PORT``** (the root ``conftest.py`` defaults it to 5176 = the dev stack; a
   ``--fresh`` stack is targeted by exporting the offset port, e.g. 5276). This is the step
   the shell lib cannot take -- it has no pytest connection to key off -- and it is what makes
   the answer unambiguous with several stacks up. ``scripts/lib/compose-project.sh`` resolves
   the same question with ``head -1`` over every running ``postgres`` container, which on this
   host currently matches two.
3. Exactly one ``postgres`` service container running anywhere -> its project. No ambiguity
   to resolve.
4. The checkout's directory basename, the shell lib's documented last resort.

Steps 3 and 4 are reached only when 2 found nothing, and both FAIL SAFE: a wrong project
matches no container, the caller gets ``None`` and its test skips as NOT MEASURED. Ambiguity
must never silently resolve to some other operator's stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: ``backend/tests/compose_project.py`` -> the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Mirrors the root ``conftest.py`` default, which is the dev stack's published port.
_DEFAULT_POSTGRES_PORT = "5176"

_DOCKER_TIMEOUT = 15


def _docker_ps(filters: list[str], fmt: str) -> list[str]:
    """Run ``docker ps`` with ``--filter`` pairs, returning non-empty output lines.

    Returns ``[]`` rather than raising when docker is absent or misbehaves: every caller
    treats "cannot tell" as "skip", which is the correct reading of an unmeasurable
    precondition.
    """
    if shutil.which("docker") is None:
        return []
    argv = ["docker", "ps", "--filter", "status=running"]
    for spec in filters:
        argv += ["--filter", spec]
    argv += ["--format", fmt]
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 -- fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in completed.stdout.strip().splitlines() if line]


def _project_label(*filters: str) -> list[str]:
    return _docker_ps(list(filters), '{{.Label "com.docker.compose.project"}}')


def compose_project_name() -> str:
    """The compose project of the stack this pytest process is pointed at.

    See the module docstring for the four-step resolution order and why the port is what
    disambiguates it.
    """
    explicit = os.environ.get("COMPOSE_PROJECT_NAME")
    if explicit:
        return explicit

    port = os.environ.get("POSTGRES_PORT") or _DEFAULT_POSTGRES_PORT
    by_port = _project_label(
        f"publish={port}",
        "label=com.docker.compose.service=postgres",
    )
    if len(by_port) == 1:
        return by_port[0]

    any_postgres = _project_label("label=com.docker.compose.service=postgres")
    if len(any_postgres) == 1:
        return any_postgres[0]

    return _REPO_ROOT.name


def compose_service_containers(service: str) -> list[str]:
    """Every running container for a compose ``service`` IN THE PROJECT UNDER TEST.

    Matches on the compose service label, not the container name, so it works for services
    that declare no ``container_name:`` -- ``diar-native`` is one, which is why its container
    is named after the checkout directory (``transcribe-app-diar-native-1``) rather than with
    the ``opentranscribe-`` prefix every other service uses.
    """
    return _docker_ps(
        [
            f"label=com.docker.compose.project={compose_project_name()}",
            f"label=com.docker.compose.service={service}",
        ],
        "{{.Names}}",
    )


def compose_service_container(service: str) -> str | None:
    """One running container for ``service`` in the project under test, else ``None``.

    Several containers here means replicas of one service in one deployment (the
    ``--gpu-scale`` worker), which are interchangeable -- unlike the cross-project
    ambiguity this module exists to remove.
    """
    containers = compose_service_containers(service)
    return containers[0] if containers else None
