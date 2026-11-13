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
3. The checkout's directory basename, the shell lib's documented last resort.

There is deliberately no "the only postgres anywhere" step: on a host that runs other
applications it picked an unrelated project's postgres. Step 3 FAILS SAFE: a wrong project
matches no container, the caller gets ``None`` and its test skips as NOT MEASURED. Ambiguity
must never silently resolve to some other operator's stack. The decision rule itself lives in
``scripts/lib/stack_target.py`` (``project_from_env``); this module only supplies the docker data.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

#: ``backend/tests/compose_project.py`` -> the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Fixed at import: tests re-point ``_REPO_ROOT`` at a tmp dir to model a worktree.
_STACK_TARGET_PATH = _REPO_ROOT / "scripts" / "lib" / "stack_target.py"

#: Mirrors the root ``conftest.py`` default, which is the dev stack's published port.
_DEFAULT_POSTGRES_PORT = "5176"

_DOCKER_TIMEOUT = 15


def _stack_target() -> ModuleType:
    """Load scripts/lib/stack_target.py (not a package) the way test_target_guard.py does."""
    path = _STACK_TARGET_PATH
    spec = importlib.util.spec_from_file_location("stack_target", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stack_target", module)
    spec.loader.exec_module(module)
    return module


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
        completed = subprocess.run(  # fixed argv, no shell  # nosec B603
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
    port = os.environ.get("POSTGRES_PORT") or _DEFAULT_POSTGRES_PORT
    owners = (
        []
        if os.environ.get("COMPOSE_PROJECT_NAME")
        else _project_label(f"publish={port}", "label=com.docker.compose.service=postgres")
    )
    return _stack_target().project_from_env(dict(os.environ), owners) or _REPO_ROOT.name


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


def default_worker_running() -> bool:
    """Whether the project under test runs the default ``celery-worker`` container.

    This is the deployed truth for "dual-GPU mode". ``GPU_SCALE_DEFAULT_WORKER`` as read
    by a test process (``.env`` or host env) is not: ``./opentr.sh start --gpu-scale
    --gpu-scale-default-worker 0`` overrides ``.env`` after the fact.
    """
    return bool(compose_service_containers("celery-worker"))


_QUEUE_FLAG = re.compile(r"(?:^|\s)(?:-Q|--queues)(?:=|\s+)(\S+)")


def consumed_queues(cmd: list[str]) -> set[str]:
    """Celery queues named by ``-Q`` / ``--queues`` in a container's ``Config.Cmd``."""
    match = _QUEUE_FLAG.search(" ".join(cmd))
    return set(match.group(1).split(",")) if match else set()


def rivals_from_commands(
    worker: str, commands: dict[str, list[str]], queues: set[str]
) -> list[str]:
    """Containers other than ``worker`` whose command consumes any of ``queues``."""
    return sorted(
        name for name, cmd in commands.items() if name != worker and consumed_queues(cmd) & queues
    )


def queue_rivals(worker: str, queues: set[str]) -> list[str]:
    """Running containers in the project under test, other than ``worker``, that consume
    any of ``queues``.

    Derived from each container's real command rather than a hardcoded service name, so a
    consumer nobody listed (the default ``celery-worker`` under ``GPU_SCALE_DEFAULT_WORKER=1``)
    is still found. A container that cannot be inspected is not a rival.
    """
    names = _docker_ps([f"label=com.docker.compose.project={compose_project_name()}"], "{{.Names}}")
    commands: dict[str, list[str]] = {}
    for name in names:
        try:
            completed = subprocess.run(  # fixed argv, no shell  # nosec B603
                ["docker", "inspect", "--format", "{{json .Config.Cmd}}", name],
                capture_output=True,
                text=True,
                timeout=_DOCKER_TIMEOUT,
                check=False,
            )
            parsed = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if isinstance(parsed, list):
            commands[name] = [str(part) for part in parsed]
    return rivals_from_commands(worker, commands, queues)
