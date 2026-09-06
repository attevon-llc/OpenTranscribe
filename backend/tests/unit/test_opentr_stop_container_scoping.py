"""`opentr.sh stop`'s straggler-cleanup loop must never touch a container that
merely shares a name prefix with this project.

`stop_all_containers()`'s "catch stragglers" loop used to select victims with
`docker ps -a --format '{{.Names}}' | grep -E '^opentranscribe-|^transcribe-app-'`
-- a bare name-prefix match, with no check that the container actually
belongs to this project's compose deployment. Reproduced live on
2026-08-31: a completely unrelated container on the host, named
``opentranscribe-homepage`` (a dashboard app from a different compose
project, sharing the name prefix by pure coincidence), was matched by that
grep and destroyed by the ``docker stop && docker rm`` that followed --
`./opentr.sh stop` deleted infrastructure it was never told about and had no
business touching.

The fix scopes the loop to containers actually labeled with one of this
project's two legitimate compose project names (``opentranscribe`` for every
service with an explicit ``container_name``, ``transcribe-app`` for
``docker-compose.diar-native.yml``'s service, which has none and so falls
back to the checkout directory's basename).

This test drives the REAL loop body (extracted from the script, not
reimplemented) against REAL throwaway containers, so a regression back to a
bare name-prefix match fails here before it can delete something on a real
host again.

⚠️ Driving the real loop is the whole point of this file, and it is also how
this file destroyed a live dev stack (issue #693): run unmodified, the loop
stops and removes every container labeled with the REAL project names --
which on a developer's machine is the running stack, 16 containers, mid
transcription. The loop's two project names are therefore parameterised in
``opentr.sh`` (``OPENTR_STOP_PROJECT_LABEL`` / ``OPENTR_STOP_PROJECT_LABEL_ALT``,
defaulting to the real literals), and every test here points them at a
per-test ``*-pytest-<uuid4>`` namespace that no real container can carry.

Three layers keep that containment honest, because a future edit that
re-hardcodes the label would otherwise silently restore the hazard:

1. ``straggler_loop_only`` -- the fixture the real-docker tests take -- refuses
   to hand the loop out unless every project-label filter in it is an
   overridable ``${...}`` expansion. It fails at *setup*, so those tests never
   execute a hardcoded loop.
   ``test_the_loop_reads_its_project_labels_from_overridable_variables``
   asserts the same thing directly, so a regression reads as one failure
   rather than four setup errors.
2. ``test_the_loop_can_only_select_containers_in_the_configured_namespace``
   runs the loop against a **fake** ``docker`` on ``PATH`` and asserts the
   filters it actually asked for name only this test's namespace.
3. ``test_with_the_override_unset_the_loop_targets_the_real_project_names``
   pins the production default: unset, the loop still filters on exactly
   ``opentranscribe`` and ``transcribe-app``.

The three fake-``docker`` guards touch no daemon at all, so they are safe to
run with a live stack up -- and they run in CI, where docker is absent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

#: The two environment variables ``stop_all_containers``'s straggler loop reads
#: to decide which compose project label it is allowed to match. Nothing that
#: ships ever sets them; only this file does.
PROJECT_LABEL_VAR = "OPENTR_STOP_PROJECT_LABEL"
PROJECT_LABEL_ALT_VAR = "OPENTR_STOP_PROJECT_LABEL_ALT"

#: The values those variables must default to when unset -- i.e. what
#: `./opentr.sh stop` does on a real host, which this change must not alter.
REAL_PROJECT_LABEL = "opentranscribe"
REAL_PROJECT_LABEL_ALT = "transcribe-app"

LABEL_FILTER_PREFIX = "label=com.docker.compose.project="

_END_OF_RECORD = "<<<END>>>"

_FAKE_DOCKER = f"""#!/bin/bash
{{
  for arg in "$@"; do printf '%s\\n' "$arg"; done
  printf '%s\\n' '{_END_OF_RECORD}'
}} >> "$FAKE_DOCKER_LOG"
if [ "${{1:-}}" = "ps" ] && [ -n "${{FAKE_DOCKER_PS_OUTPUT:-}}" ]; then
  printf '%s\\n' "$FAKE_DOCKER_PS_OUTPUT"
fi
exit 0
"""

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)

requires_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


def _project_label_expansions(loop_text: str) -> list[str]:
    """Whatever each ``label=com.docker.compose.project=`` filter in the loop is
    followed by, truncated to the two characters that decide whether it is an
    overridable ``${...}`` expansion or a hardcoded literal."""
    return re.findall(rf"{re.escape(LABEL_FILTER_PREFIX)}(.{{0,2}})", loop_text)


def _env_without_overrides(**overrides: str) -> dict[str, str]:
    """A copy of the ambient environment with both override variables cleared,
    then whatever the caller asked for put back. Clearing first means an
    exported value in the developer's shell cannot silently widen a test's
    namespace back out to the live stack."""
    env = dict(os.environ)
    env.pop(PROJECT_LABEL_VAR, None)
    env.pop(PROJECT_LABEL_ALT_VAR, None)
    env.update(overrides)
    return env


def _run_loop_with_fake_docker(
    loop_text: str,
    tmp_path: Path,
    *,
    overrides: dict[str, str],
    ps_output: str = "",
) -> list[list[str]]:
    """Execute the real loop with a fake ``docker`` first on ``PATH``, and return
    the argv of every ``docker`` invocation it made.

    Nothing reaches the real daemon, so this is safe to run with a live stack up
    -- which is exactly what makes it usable as the guard on the containment
    the destructive tests below depend on.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(_FAKE_DOCKER, encoding="utf-8")
    shim.chmod(0o755)

    log = tmp_path / "docker-calls.log"
    log.write_text("", encoding="utf-8")

    env = _env_without_overrides(**overrides)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(log)
    env["FAKE_DOCKER_PS_OUTPUT"] = ps_output

    subprocess.run(["bash", "-c", loop_text], capture_output=True, timeout=60, env=env, check=False)

    records: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text(encoding="utf-8").split("\n"):
        if line == _END_OF_RECORD:
            records.append(current)
            current = []
        else:
            current.append(line)
    return records


def _run_real_loop(loop_text: str, namespace: tuple[str, str]) -> None:
    """Run the loop against the real daemon, scoped to ``namespace``."""
    primary, alternate = namespace
    subprocess.run(
        ["bash", "-c", loop_text],
        capture_output=True,
        timeout=60,
        env=_env_without_overrides(
            **{PROJECT_LABEL_VAR: primary, PROJECT_LABEL_ALT_VAR: alternate}
        ),
        check=False,
    )


def _extract_straggler_loop() -> str:
    """Just the 'catch stragglers' loop, isolated from the compose-down calls
    above it (which would otherwise try to reach real compose files/services
    this test has no interest in standing up).

    Raw text, with no safety precondition applied -- only ever run this under a
    fake `docker`. The real daemon is reached through ``straggler_loop_only``."""
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "stop_all_containers")
    loop_start = body.index("for container in")
    loop_end = body.index("\n}\n", loop_start)
    return body[loop_start:loop_end]


@pytest.fixture
def raw_straggler_loop() -> str:
    """The loop as written, for the guard tests below, which execute it against
    a fake `docker` and so cannot reach a real container whatever it says."""
    return _extract_straggler_loop()


@pytest.fixture
def straggler_loop_only() -> str:
    """The loop, cleared for execution against the REAL docker daemon.

    Fails the test at SETUP if the loop's project-label filters are not
    overridable -- a hardcoded filter would make every test in this file run
    `docker stop && docker rm` over the live dev stack (issue #693), so it must
    never be handed out."""
    loop = _extract_straggler_loop()

    expansions = _project_label_expansions(loop)
    if not expansions:
        pytest.fail(
            "no 'label=com.docker.compose.project=' filter found in the straggler loop -- "
            "it may have regressed to a bare name-prefix match, or moved"
        )
    hardcoded = [e for e in expansions if not e.startswith("${")]
    if hardcoded:
        pytest.fail(
            "the straggler loop hardcodes a compose project label "
            f"({hardcoded!r}) instead of reading ${{{PROJECT_LABEL_VAR}}}/"
            f"${{{PROJECT_LABEL_ALT_VAR}}}. Refusing to run it: executing this loop "
            "unscoped stops and removes the live dev stack (issue #693)."
        )
    return loop


@pytest.fixture
def throwaway_project_namespace() -> tuple[str, str]:
    """A per-test pair of compose project names that no real container carries.

    These stand in for ``opentranscribe`` / ``transcribe-app`` while the loop
    runs, so 'a container belonging to this project' can be asserted on without
    'this project' meaning the developer's running stack."""
    token = uuid.uuid4().hex
    return (f"opentranscribe-pytest-{token}", f"transcribe-app-pytest-{token}")


@pytest.fixture
def throwaway_containers():
    """Create real, stopped-friendly containers with distinguishing labels/names,
    and guarantee cleanup even if the test (or the code under test) misbehaves."""
    if not _docker_available():
        pytest.skip("docker daemon not reachable")

    created: list[str] = []

    def _make(name: str, project_label: str | None) -> str:
        # --platform linux/amd64: this host's cached `alpine:3.20` is an s390x image
        # (measured 2026-09-06), which exits immediately with an exec-format error on an
        # amd64 host -- the container would never actually be Running, which the GPU
        # drain's stop/no-stop assertions below need to observe. The pre-existing
        # `_container_exists` (existence-only) tests never depended on this.
        cmd = ["docker", "run", "-d", "--platform", "linux/amd64", "--name", name]
        if project_label is not None:
            cmd += ["--label", f"com.docker.compose.project={project_label}"]
        cmd += ["alpine:3.20", "sleep", "300"]
        subprocess.run(cmd, capture_output=True, check=True, timeout=60)
        created.append(name)
        return name

    yield _make

    for name in created:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


def _container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{name}$"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return name in result.stdout.split()


# --------------------------------------------------------------------------
# Containment guards -- no real docker, safe with a live stack up.
# --------------------------------------------------------------------------


def test_the_loop_reads_its_project_labels_from_overridable_variables(raw_straggler_loop):
    """Fail-closed precondition, asserted in its own right so a regression reports
    as one failure here rather than as four confusing setup errors below: every
    compose-project filter in the loop must be an overridable ``${...}``
    expansion. A hardcoded one puts the live dev stack back in blast range."""
    expansions = _project_label_expansions(raw_straggler_loop)
    assert expansions, (
        "no 'label=com.docker.compose.project=' filter found in the straggler loop -- "
        "it may have regressed to a bare name-prefix match, or moved"
    )
    assert [e for e in expansions if not e.startswith("${")] == [], (
        "the straggler loop hardcodes a compose project label instead of reading "
        f"${{{PROJECT_LABEL_VAR}}}/${{{PROJECT_LABEL_ALT_VAR}}} -- executing it "
        "unscoped stops and removes the live dev stack (issue #693)"
    )


def test_the_loop_can_only_select_containers_in_the_configured_namespace(
    raw_straggler_loop, throwaway_project_namespace, tmp_path
):
    """The issue #693 guard: with the overrides set, every selection the loop
    performs is confined to this test's namespace. Nothing else -- no real
    project label, no unfiltered `docker ps` -- may appear."""
    primary, alternate = throwaway_project_namespace
    calls = _run_loop_with_fake_docker(
        raw_straggler_loop,
        tmp_path,
        overrides={PROJECT_LABEL_VAR: primary, PROJECT_LABEL_ALT_VAR: alternate},
        # A poisoned selection: if the loop ever ignored its own filters, these
        # are the names it would act on.
        ps_output="opentranscribe-backend\nopentranscribe-postgres",
    )

    selections = [call for call in calls if call and call[0] == "ps"]
    assert len(selections) == 2, (
        f"expected exactly two container selections in the straggler loop, got {selections!r}"
    )

    project_filters = {
        arg for call in selections for arg in call if arg.startswith(LABEL_FILTER_PREFIX)
    }
    assert project_filters == {
        f"{LABEL_FILTER_PREFIX}{primary}",
        f"{LABEL_FILTER_PREFIX}{alternate}",
    }, (
        "the straggler loop queried docker for a compose project outside this test's "
        f"namespace -- it can reach real containers: {project_filters!r}"
    )

    unfiltered = [
        call for call in selections if not any(arg.startswith(LABEL_FILTER_PREFIX) for arg in call)
    ]
    assert unfiltered == [], (
        f"a `docker ps` in the straggler loop carries no project-label filter: {unfiltered!r}"
    )


def test_with_the_override_unset_the_loop_targets_the_real_project_names(
    raw_straggler_loop, tmp_path
):
    """Production default, unchanged: with neither override exported, the loop
    filters on exactly the two real compose project names it always did."""
    calls = _run_loop_with_fake_docker(raw_straggler_loop, tmp_path, overrides={})

    selections = [call for call in calls if call and call[0] == "ps"]
    project_filters = {
        arg for call in selections for arg in call if arg.startswith(LABEL_FILTER_PREFIX)
    }
    assert project_filters == {
        f"{LABEL_FILTER_PREFIX}{REAL_PROJECT_LABEL}",
        f"{LABEL_FILTER_PREFIX}{REAL_PROJECT_LABEL_ALT}",
    }, (
        "parameterising the project label changed what `./opentr.sh stop` does by default -- "
        f"it must be byte-identical to the previous hardcoded filters, got {project_filters!r}"
    )


def test_the_loop_still_stops_and_removes_whatever_it_selects(
    raw_straggler_loop, throwaway_project_namespace, tmp_path
):
    """Control for the two guards above: proving the loop asks narrow questions
    is worthless if it no longer acts on the answers."""
    primary, alternate = throwaway_project_namespace
    calls = _run_loop_with_fake_docker(
        raw_straggler_loop,
        tmp_path,
        overrides={PROJECT_LABEL_VAR: primary, PROJECT_LABEL_ALT_VAR: alternate},
        ps_output=f"{primary}-straggler",
    )

    assert ["stop", f"{primary}-straggler"] in calls, (
        f"the loop selected a straggler but never stopped it: {calls!r}"
    )
    assert ["rm", f"{primary}-straggler"] in calls, (
        f"the loop stopped a straggler but never removed it: {calls!r}"
    )


# --------------------------------------------------------------------------
# The original four, now scoped to a throwaway project namespace.
# --------------------------------------------------------------------------


@requires_docker
def test_a_container_sharing_only_the_name_prefix_survives(
    straggler_loop_only, throwaway_project_namespace, throwaway_containers
):
    """The exact live defect: a container named like this project's containers
    that is NOT labeled with this project's compose project must not be touched."""
    primary, _ = throwaway_project_namespace
    unrelated = throwaway_containers(
        f"{primary}-unrelated-dashboard", project_label="some-other-app"
    )

    _run_real_loop(straggler_loop_only, throwaway_project_namespace)

    assert _container_exists(unrelated), (
        "a container that only shares the project's name prefix, but belongs to a "
        "DIFFERENT compose project, was removed -- this is the exact live incident"
    )


@requires_docker
def test_a_real_project_container_is_still_cleaned_up(
    straggler_loop_only, throwaway_project_namespace, throwaway_containers
):
    """Control: the loop must still do its actual job for a genuine straggler."""
    primary, _ = throwaway_project_namespace
    real = throwaway_containers(f"{primary}-genuine-straggler", project_label=primary)

    _run_real_loop(straggler_loop_only, throwaway_project_namespace)

    assert not _container_exists(real), (
        "a container genuinely labeled as belonging to this project's compose deployment "
        "was NOT cleaned up -- the fix must not have made the loop a no-op"
    )


@requires_docker
def test_the_transcribe_app_project_container_is_also_cleaned_up(
    straggler_loop_only, throwaway_project_namespace, throwaway_containers
):
    """The diar-native service has no explicit container_name, so its project
    is the checkout directory's basename -- must be covered too, not just
    'opentranscribe'. Here that second project name is the namespace's alternate."""
    _, alternate = throwaway_project_namespace
    diar = throwaway_containers(f"{alternate}-diar-native-straggler", project_label=alternate)

    _run_real_loop(straggler_loop_only, throwaway_project_namespace)

    assert not _container_exists(diar), "a genuine second-project straggler was not cleaned up"


@requires_docker
def test_an_unlabeled_container_sharing_the_name_prefix_survives(
    straggler_loop_only, throwaway_project_namespace, throwaway_containers
):
    """A container with no compose project label at all (e.g. a bare `docker run`,
    not part of any compose deployment) must not be treated as a straggler just
    because its name happens to start with the right prefix."""
    primary, _ = throwaway_project_namespace
    bare = throwaway_containers(f"{primary}-bare-docker-run", project_label=None)

    _run_real_loop(straggler_loop_only, throwaway_project_namespace)

    assert _container_exists(bare), "an unlabeled container was removed on name prefix alone"


# --------------------------------------------------------------------------
# ot_drain_gpu_workers_by_container() (issue #782) -- the container-driven GPU drain
# stop_all_containers() calls before EITHER of its two `down` chains. Same #693 hazard,
# same three-layer guard shape as the straggler loop above: it reads the identical
# OPENTR_STOP_PROJECT_LABEL{,_ALT} overrides, so it gets the identical fail-at-setup
# fixture and the identical fake-docker + real-docker containment proof.
# --------------------------------------------------------------------------


def _extract_gpu_drain_function() -> str:
    """The full `ot_drain_gpu_workers_by_container() { ... }` definition, unmodified --
    only ever run this under a fake `docker`, or through `gpu_drain_function_only`."""
    return _function_body(OPENTR.read_text(encoding="utf-8"), "ot_drain_gpu_workers_by_container")


@pytest.fixture
def raw_gpu_drain_function() -> str:
    return _extract_gpu_drain_function()


@pytest.fixture
def gpu_drain_function_only() -> str:
    """Same safety precondition as `straggler_loop_only`, applied to the GPU drain
    helper: fails at SETUP if its project-label filters are not overridable -- a
    hardcoded filter would let every real-docker test below stop live CUDA-holding
    containers on a real host (issue #693's exact hazard, for a second call site)."""
    body = _extract_gpu_drain_function()

    expansions = _project_label_expansions(body)
    if not expansions:
        pytest.fail(
            "no 'label=com.docker.compose.project=' filter found in "
            "ot_drain_gpu_workers_by_container() -- it may have regressed to a bare "
            "name-prefix match, or moved"
        )
    hardcoded = [e for e in expansions if not e.startswith("${")]
    if hardcoded:
        pytest.fail(
            "ot_drain_gpu_workers_by_container() hardcodes a compose project label "
            f"({hardcoded!r}) instead of reading ${{{PROJECT_LABEL_VAR}}}/"
            f"${{{PROJECT_LABEL_ALT_VAR}}}. Refusing to run it: executing this function "
            "unscoped can stop live CUDA-holding containers on a real host (issue #693)."
        )
    return body


def _container_is_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _run_real_gpu_drain(function_text: str, namespace: tuple[str, str]) -> None:
    """Run the real function against the real daemon, scoped to `namespace`, with a
    short grace period so a genuinely-drained container doesn't slow the test down."""
    primary, alternate = namespace
    script = f"{function_text}\not_drain_gpu_workers_by_container\n"
    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        timeout=60,
        env=_env_without_overrides(
            **{
                PROJECT_LABEL_VAR: primary,
                PROJECT_LABEL_ALT_VAR: alternate,
                "OT_STOP_GRACE_GPU": "1",
            }
        ),
        check=False,
    )


def test_the_gpu_drain_function_reads_its_project_labels_from_overridable_variables(
    raw_gpu_drain_function,
):
    """Fail-closed precondition for the second call site: every compose-project filter
    in ot_drain_gpu_workers_by_container() must be an overridable ${...} expansion."""
    expansions = _project_label_expansions(raw_gpu_drain_function)
    assert expansions, (
        "no 'label=com.docker.compose.project=' filter found in "
        "ot_drain_gpu_workers_by_container() -- it may have regressed to a bare "
        "name-prefix match, or moved"
    )
    assert [e for e in expansions if not e.startswith("${")] == [], (
        "ot_drain_gpu_workers_by_container() hardcodes a compose project label instead "
        f"of reading ${{{PROJECT_LABEL_VAR}}}/${{{PROJECT_LABEL_ALT_VAR}}} -- executing "
        "it unscoped can stop live CUDA-holding containers on a real host (issue #693)"
    )


def test_the_gpu_drain_function_can_only_select_containers_in_the_configured_namespace(
    raw_gpu_drain_function, throwaway_project_namespace, tmp_path
):
    primary, alternate = throwaway_project_namespace
    script = f"{raw_gpu_drain_function}\not_drain_gpu_workers_by_container"
    calls = _run_loop_with_fake_docker(
        script,
        tmp_path,
        overrides={
            PROJECT_LABEL_VAR: primary,
            PROJECT_LABEL_ALT_VAR: alternate,
            "OT_STOP_GRACE_GPU": "1",
        },
        # A poisoned selection: if the function ever ignored its own filters, this is
        # the name it would act on.
        ps_output="opentranscribe-celery-worker",
    )

    selections = [call for call in calls if call and call[0] == "ps"]
    assert len(selections) == 2, f"expected exactly two container selections, got {selections!r}"
    project_filters = {
        arg for call in selections for arg in call if arg.startswith(LABEL_FILTER_PREFIX)
    }
    assert project_filters == {
        f"{LABEL_FILTER_PREFIX}{primary}",
        f"{LABEL_FILTER_PREFIX}{alternate}",
    }, (
        "ot_drain_gpu_workers_by_container() queried docker for a compose project "
        f"outside this test's namespace -- it can reach real containers: {project_filters!r}"
    )

    stop_calls = [call for call in calls if call and call[0] == "stop"]
    assert any("opentranscribe-celery-worker" in call for call in stop_calls), (
        f"the function selected a matching container but never issued `docker stop` "
        f"for it: {calls!r}"
    )
    assert any("-t" in call and "1" in call for call in stop_calls), (
        f'the drain\'s `docker stop` did not carry -t "$OT_STOP_GRACE_GPU": {stop_calls!r}'
    )


def test_with_the_override_unset_the_gpu_drain_function_targets_the_real_project_names(
    raw_gpu_drain_function, tmp_path
):
    """Production default, unchanged: with neither override exported, the function
    filters on exactly the two real compose project names it always did."""
    script = f"{raw_gpu_drain_function}\not_drain_gpu_workers_by_container"
    calls = _run_loop_with_fake_docker(script, tmp_path, overrides={"OT_STOP_GRACE_GPU": "1"})

    selections = [call for call in calls if call and call[0] == "ps"]
    project_filters = {
        arg for call in selections for arg in call if arg.startswith(LABEL_FILTER_PREFIX)
    }
    assert project_filters == {
        f"{LABEL_FILTER_PREFIX}{REAL_PROJECT_LABEL}",
        f"{LABEL_FILTER_PREFIX}{REAL_PROJECT_LABEL_ALT}",
    }, (
        "parameterising the project label changed ot_drain_gpu_workers_by_container()'s "
        f"default behaviour -- got {project_filters!r}"
    )


def test_the_gpu_drain_function_ignores_a_non_matching_service_name(
    raw_gpu_drain_function, throwaway_project_namespace, tmp_path
):
    """The name-substring filter: a real, correctly-labeled container whose name matches
    none of celery-worker*/celery-cpu-worker*/celery-redaction*/diar-native* (e.g.
    postgres) must never be issued a `docker stop` -- this helper only drains
    CUDA-holding services, not the whole project."""
    primary, alternate = throwaway_project_namespace
    script = f"{raw_gpu_drain_function}\not_drain_gpu_workers_by_container"
    calls = _run_loop_with_fake_docker(
        script,
        tmp_path,
        overrides={
            PROJECT_LABEL_VAR: primary,
            PROJECT_LABEL_ALT_VAR: alternate,
            "OT_STOP_GRACE_GPU": "1",
        },
        ps_output=f"{primary}-postgres\n{primary}-backend",
    )

    stop_calls = [call for call in calls if call and call[0] == "stop"]
    assert stop_calls == [], (
        f"the drain issued `docker stop` against a non-CUDA-holding service name: {stop_calls!r}"
    )


@requires_docker
def test_gpu_drain_stops_a_real_matching_container(
    gpu_drain_function_only, throwaway_project_namespace, throwaway_containers
):
    primary, _ = throwaway_project_namespace
    name = throwaway_containers(f"{primary}-celery-worker", project_label=primary)

    _run_real_gpu_drain(gpu_drain_function_only, throwaway_project_namespace)

    assert not _container_is_running(name), (
        "a genuinely labeled, name-matching CUDA-holding container was not stopped by "
        "the GPU drain helper"
    )


@requires_docker
def test_gpu_drain_does_not_touch_a_non_matching_service_in_the_same_project(
    gpu_drain_function_only, throwaway_project_namespace, throwaway_containers
):
    primary, _ = throwaway_project_namespace
    name = throwaway_containers(f"{primary}-postgres", project_label=primary)

    _run_real_gpu_drain(gpu_drain_function_only, throwaway_project_namespace)

    assert _container_is_running(name), (
        "the GPU drain helper stopped a container whose name does not match any "
        "CUDA-holding service pattern -- it must only touch celery-worker*/"
        "celery-cpu-worker*/celery-redaction*/diar-native* names"
    )


@requires_docker
def test_gpu_drain_does_not_touch_a_name_matching_container_in_a_different_project(
    gpu_drain_function_only, throwaway_project_namespace, throwaway_containers
):
    """The exact #693 defect shape, reproduced for the second drain helper: a container
    that only shares the name substring, but belongs to a DIFFERENT compose project,
    must not be touched."""
    primary, _ = throwaway_project_namespace
    name = throwaway_containers(
        f"{primary}-celery-worker-unrelated", project_label="some-other-app"
    )

    _run_real_gpu_drain(gpu_drain_function_only, throwaway_project_namespace)

    assert _container_is_running(name), (
        "a container that only shares the name substring, but belongs to a DIFFERENT "
        "compose project, was stopped -- this is the #693 defect shape"
    )
