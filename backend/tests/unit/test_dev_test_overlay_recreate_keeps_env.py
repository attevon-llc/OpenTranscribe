"""A recreate must carry EVERY needed overlay's flag, not only the missing ones.

``setup_overlays`` brings overlays up with one batched ``opentr.sh start dev --with-...``.
It used to build that flag list from ``OVERLAYS_TO_START`` — the overlays whose containers
were absent — and omit the ones already running, on the reasoning that a running container
needs no starting.

That reasoning holds only for overlays which ADD a container. Several instead PATCH existing
services: ``mock-asr`` sets ``GLADIA_API_BASE_URL`` and ``ASR_ALLOW_PRIVATE_ENDPOINTS`` on
``backend`` and ``celery-cloud-asr-worker``; ``watch`` sets ``WATCH_FOLDER_PATH`` on four
services. ``opentr.sh start dev`` RECREATES the app services, and an overlay's compose file is
in that chain only if its flag was passed — so omitting an already-up overlay recreates those
services **without its env**, silently un-configuring it while its container keeps running and
every container-based health check keeps passing.

Measured, not hypothesised. A run needing mock-asr (already up) plus keycloak/ldap (not) issued
``start dev --with-keycloak-test --with-ldap-test``; ``GLADIA_API_BASE_URL`` came back
``<UNSET>`` on both services, the app stopped routing ASR to the mock, and all six lite-mode
integration tests failed with ``status=error`` — forty minutes after the same six passed. The
mock-asr container was up and healthy throughout, which is precisely why "container running =>
overlay effective" is the wrong test and why ``watch_overlay_active()`` already detects the
watch overlay by reading backend env rather than by looking for a container.

These tests run the REAL ``setup_overlays`` against a fake ``opentr.sh`` that records its
argv, because a grep for ``OVERLAYS_NEEDED`` would pass against a version that builds the list
correctly and then never uses it.

⚠️ Running the real library means the real ``docker`` was reachable, and that made this file
KILL A LIVE CONTAINER on every run of the unit suite. ``setup_overlays`` resolves each started
overlay's container through ``overlay_container_name`` (``scripts/lib/compose-project.sh``),
which does a real ``docker ps`` scoped to the LIVE compose project — so ``mock-llm`` resolved to
``opentranscribe-mock-llm``, was appended to ``OVERLAYS_STARTED_BY_US``, and the
``trap teardown_overlays EXIT`` the library installs at source time then ran
``docker stop opentranscribe-mock-llm`` when the test's ``bash -c`` exited.
``scripts/mock-llm-server.py`` is PID 1 and ignores SIGTERM, so that was a 10 s grace followed
by SIGKILL. Measured on a decoy container carrying the live project's
``com.docker.compose.{project,service}`` labels: the first test took **11.58 s** and the
container ended ``exited exit=137``. In the gate the three tests run on different xdist workers,
so all three race the same live container and each pays the grace (11.24/11.11/11.09 s in the
2026-09-06 ``--durations`` list) — and the mock-llm container was dead four minutes into a
38-minute run, which silently skipped 6 integration and 26 e2e tests downstream.

Both halves of that chain are neutralised below, deliberately belt-and-braces:
a fake ``docker`` first on ``PATH`` (so no lookup can resolve a real container) and
``trap - EXIT`` (so the library's teardown cannot run even if a lookup somehow did). Same rule
as issue #693: a test that executes real infrastructure tooling must be scoped to a namespace
no real object can occupy, and the scoping must be ASSERTED, not intended — see
``test_docker_is_never_reached`` and ``tests/unit/test_overlay_lib_tests_are_docker_sealed.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.overlay_lib_harness import OVERLAY_LIB
from tests.fixtures.overlay_lib_harness import SEALED_PROJECT
from tests.fixtures.overlay_lib_harness import docker_calls
from tests.fixtures.overlay_lib_harness import mutating_docker_calls
from tests.fixtures.overlay_lib_harness import sealed_script

pytestmark = pytest.mark.skipif(
    not OVERLAY_LIB.exists(), reason="scripts/lib/dev-test-overlays.sh not in this checkout"
)


def _overlay_script(tmp_path: Path, fake_root: Path, body: str) -> str:
    """``sealed_script`` plus the ``detect_overlay_state`` stub every test here needs."""
    return sealed_script(
        tmp_path,
        fake_root,
        "\ndetect_overlay_state() { :; }   # state is injected below, not discovered\n" + body,
    )


def _recorded_flags(tmp_path: Path, needed: list[str], already_up: list[str]) -> list[str]:
    """Run the real setup_overlays and return the --with-* flags it passed to opentr.sh.

    ``detect_overlay_state`` is overridden after sourcing (bash keeps the last definition) so
    the arrays are set directly instead of shelling out to docker — the thing under test is
    what setup_overlays does WITH that state, not how it discovers it.
    """
    bash = shutil.which("bash")
    assert bash, "bash not on PATH"

    fake_root = tmp_path / "repo"
    fake_root.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.txt"
    fake = fake_root / "opentr.sh"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >> "{argv_log}"\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)

    to_start = [f for f in needed if f not in already_up]
    script = _overlay_script(
        tmp_path,
        fake_root,
        f"""
        OVERLAYS_NEEDED=({" ".join(needed)})
        OVERLAYS_ALREADY_UP=({" ".join(already_up)})
        OVERLAYS_TO_START=({" ".join(to_start)})

        setup_overlays
        """,
    )
    subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    if not argv_log.exists():
        return []
    return [
        ln for ln in argv_log.read_text(encoding="utf-8").splitlines() if ln.startswith("--with-")
    ]


def test_an_already_up_env_patching_overlay_keeps_its_flag_on_the_recreate(tmp_path: Path):
    """The regression itself: mock-asr already up, keycloak missing -> BOTH flags."""
    flags = _recorded_flags(tmp_path, needed=["mock-llm", "mock-asr"], already_up=["mock-asr"])
    assert "--with-mock-asr" in flags, (
        f"opentr.sh was called with {flags} — mock-asr's flag was dropped because its container "
        f"was already running. `start dev` recreates backend and celery-cloud-asr-worker, and "
        f"without --with-mock-asr in the chain they come back with GLADIA_API_BASE_URL unset, "
        f"so ASR silently stops routing to the mock while the mock container stays healthy."
    )
    assert "--with-mock-llm" in flags, f"the overlay that WAS missing is also absent: {flags}"


def test_nothing_extra_is_passed(tmp_path: Path):
    """Control: the fix must not pass flags for overlays this run does not need.

    Without this, `--with-<everything>` would satisfy the test above while starting overlays
    nobody asked for — recreating app containers and burning minutes on every run.
    """
    flags = _recorded_flags(tmp_path, needed=["mock-llm"], already_up=[])
    assert flags == ["--with-mock-llm"], f"expected exactly the needed overlay's flag, got {flags}"


def test_teardown_still_only_owns_what_this_run_started(tmp_path: Path):
    """Guards the OTHER direction: passing every needed flag must not make the run claim
    ownership of a container it found already running, or teardown would stop someone else's.

    OVERLAYS_STARTED_BY_US is appended from OVERLAYS_TO_START, deliberately, and this asserts
    that stayed true when the flag list moved to OVERLAYS_NEEDED.
    """
    bash = shutil.which("bash")
    assert bash, "bash not on PATH"
    fake_root = tmp_path / "repo"
    fake_root.mkdir(exist_ok=True)
    fake = fake_root / "opentr.sh"
    fake.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    script = _overlay_script(
        tmp_path,
        fake_root,
        """
        OVERLAYS_NEEDED=(mock-llm mock-asr)
        OVERLAYS_ALREADY_UP=(mock-asr)
        OVERLAYS_TO_START=(mock-llm)
        setup_overlays
        printf 'OWNED:%s\\n' "${OVERLAYS_STARTED_BY_US[*]:-}"
        """,
    )
    proc = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, timeout=120, cwd=tmp_path
    )
    owned = [
        ln.removeprefix("OWNED:").split()
        for ln in proc.stdout.splitlines()
        if ln.startswith("OWNED:")
    ]
    assert owned, f"no OWNED line.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "mock-asr" not in owned[0], (
        f"this run claimed ownership of mock-asr ({owned[0]}), which it found already running "
        f"— teardown would stop a container belonging to whoever started it"
    )


def test_docker_is_sealed(tmp_path: Path):
    """The seal, asserted rather than assumed (issue #693's rule).

    Running the real ``setup_overlays`` used to issue a real ``docker ps`` against the LIVE
    compose project and then ``docker stop`` whatever it named — see this module's docstring.
    Nothing above would notice: the flag assertions pass identically either way, which is
    exactly why the kill survived in the gate for as long as it did.

    Two properties, one per half of the seal:

    * every project lookup resolved to the sentinel, so ``PATH`` interception is in effect and
      ``compose_project_name`` could not read the live daemon;
    * no mutating verb was issued even though a container name WAS resolved — i.e. the EXIT
      trap really is neutralised, not merely starved of a target.
    """
    flags = _recorded_flags(tmp_path, needed=["mock-llm", "mock-asr"], already_up=["mock-asr"])
    assert flags, "the harness did not run at all — the assertions below would be vacuous"

    calls = docker_calls(tmp_path)
    assert calls, (
        "the sealed docker shim was never invoked, so this test cannot distinguish a sealed "
        "run from one that talked to the real daemon"
    )

    project_filters = [c for c in calls if "com.docker.compose.project=" in c]
    assert project_filters, f"no container lookup happened at all; calls={calls}"
    for call in project_filters:
        assert f"com.docker.compose.project={SEALED_PROJECT}" in call, (
            f"a container lookup was scoped to a project the shim did not invent: {call!r}. "
            f"compose_project_name reached a real docker daemon, which means this file is "
            f"once again pointed at whatever stack is running on this host."
        )

    mutating = mutating_docker_calls(tmp_path)
    assert mutating == [], (
        f"setup_overlays issued mutating docker commands: {mutating}. The library installs "
        f"`trap teardown_overlays EXIT` at source time; without the `trap - EXIT` in "
        f"_overlay_script these run against whatever the container lookup named — on the live "
        f"stack that was `docker stop opentranscribe-mock-llm`, SIGKILLed after a 10s grace."
    )
