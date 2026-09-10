"""``overlay_container_name`` must not hand its callers a SIGPIPE status.

THE DEFECT THIS PINS
--------------------
``scripts/lib/compose-project.sh``'s ``overlay_container_name`` ended in::

    docker ps --filter ... --format '{{.Names}}' 2>/dev/null | head -1

A pipeline that is a function's **last command** makes the pipeline's status the
**function's return status**. ``head -1`` exits after the first line, so whenever the
filters match more than one container the still-writing ``docker ps`` takes SIGPIPE and
exits 141; under ``set -o pipefail`` that 141 becomes the function's return value.

Fifteen call sites, and almost all of them spell it ``X="$(overlay_container_name svc)"``
— an **assignment**, which under ``set -e`` aborts the calling script outright, with no
message and no failing command named. Measured::

    bash -c 'set -euo pipefail; g(){ seq 1 200000 | head -1; }; x="$(g)"; echo REACHED'
    # exits 141, never prints

WHY THIS NEEDS ITS OWN FILE
---------------------------
Neither existing scanner can see it, and that is a property of the hazard rather than an
oversight:

* ``test_pipefail_grep_q_inversion.py`` scans all of ``scripts/`` but only for the
  ``| grep -q`` boolean shape.
* ``test_opentr_docker_probe_sigpipe.py``'s scan short-circuits on files that do not
  themselves ``set -o pipefail`` — and ``compose-project.sh`` is a **sourced library**. It
  sets no options at all; it inherits ``pipefail`` from whichever script sourced it. A
  purely textual scan of the library is therefore blind here by construction, which is why
  this drives the real function instead.

More than one container matching is not a corner case: ``--gpu-scale`` runs a scaled worker
topology, and ``run-integration-tests.sh`` looks up ``celery-worker-gpu-scaled`` by exactly
these filters.

Nothing here reaches a real daemon — every case puts a stub ``docker`` earlier on ``PATH``
— so this runs in CI, and is safe with a live stack up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PROJECT_LIB = REPO_ROOT / "scripts" / "lib" / "compose-project.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _write_docker_stub(stubs: Path, *, service_body: str) -> None:
    """A fake ``docker`` answering the library's two distinct ``docker ps`` queries.

    ``compose_project_name`` asks for postgres containers' project label; everything else is
    ``overlay_container_name``'s own lookup, whose behaviour each test supplies.
    """
    stubs.mkdir(parents=True, exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'args="$*"\n'
        'case "$args" in\n'
        "  *service=postgres*)\n"
        # The live project, so overlay_container_name proceeds to its own query.
        "    printf 'opentranscribe\\n'\n"
        "    exit 0 ;;\n"
        "esac\n"
        f"{service_body}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def _run_caller(
    stubs: Path, service: str = "celery-worker-gpu-scaled"
) -> subprocess.CompletedProcess[str]:
    """Source the REAL library and use it the way the real callers do.

    ``set -euo pipefail`` plus a capturing assignment is not an invented harness: it is
    verbatim the shape of ``run-dev-tests.sh:271``, ``diar-native-smoke.sh:129``,
    ``verify-diar-native-e2e.sh:58`` and ten others.
    """
    body = (
        "set -euo pipefail\n"
        f'source "{COMPOSE_PROJECT_LIB}"\n'
        f'NAME="$(overlay_container_name {service})"\n'
        'printf "RESULT_NAME=[%s]\\n" "$NAME"\n'
        "echo REACHED_NEXT_STATEMENT\n"
    )
    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    # A COMPOSE_PROJECT_NAME inherited from the developer's shell would short-circuit
    # compose_project_name and leave half the library unexercised.
    env.pop("COMPOSE_PROJECT_NAME", None)
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=60, env=env, check=False
    )


def _result_name(proc: subprocess.CompletedProcess[str]) -> str:
    combined = proc.stdout + proc.stderr
    marker = "RESULT_NAME=["
    assert marker in combined, f"the caller never reached its own print:\n{combined}"
    return combined.split(marker, 1)[1].split("]", 1)[0]


def test_a_scaled_service_with_several_containers_does_not_abort_the_caller() -> None:
    """MUST-FIRE case for the defect.

    Three containers match — the ``--gpu-scale`` topology — and the stub writes them with a
    real gap, as a daemon round-trip does. Under the old ``| head -1`` the reader leaves
    after the first name, ``docker ps`` takes SIGPIPE, and the caller's assignment aborts
    the whole script silently.
    """
    with _stub_dir() as stubs:
        _write_docker_stub(
            stubs,
            service_body=(
                "printf 'opentranscribe-celery-worker-gpu-scaled-1\\n'\n"
                # The gap is the mechanism: SIGPIPE needs the producer to still have a write
                # to make when the reader goes away. Measured 2026-09-07 -- a 40-byte
                # producer with a 2 ms gap inverts 300/300, while the same bytes written
                # back-to-back invert 0/3000.
                "sleep 0.05\n"
                "printf 'opentranscribe-celery-worker-gpu-scaled-2\\n'\n"
                "printf 'opentranscribe-celery-worker-gpu-scaled-3\\n'\n"
                "exit 0\n"
            ),
        )
        proc = _run_caller(stubs)

    assert "REACHED_NEXT_STATEMENT" in proc.stdout, (
        'the caller died inside `NAME="$(overlay_container_name ...)"`. That is the '
        "`| head -1` SIGPIPE (141) becoming the FUNCTION's return status and aborting an "
        "assignment under `set -e` -- with no error message, which is why it reads as the "
        f"script simply stopping.\nrc={proc.returncode}\n{proc.stdout}{proc.stderr}"
    )
    assert _result_name(proc) == "opentranscribe-celery-worker-gpu-scaled-1", (
        "the first matching container name is the documented contract and must survive the "
        f"fix unchanged.\n{proc.stdout}{proc.stderr}"
    )


def test_a_single_matching_container_still_resolves_to_its_name() -> None:
    """MUST-STAY-CLEAN control: the ordinary one-container case is unchanged."""
    with _stub_dir() as stubs:
        _write_docker_stub(stubs, service_body="printf 'opentranscribe-backend\\n'\nexit 0\n")
        proc = _run_caller(stubs, service="backend")

    assert _result_name(proc) == "opentranscribe-backend", proc.stdout + proc.stderr
    assert "REACHED_NEXT_STATEMENT" in proc.stdout


def test_no_matching_container_is_the_empty_string_and_not_an_error() -> None:
    """MUST-STAY-CLEAN control: "" for none is the contract every caller branches on.

    Without this, a fix that always returned some name would satisfy the case above.
    """
    with _stub_dir() as stubs:
        _write_docker_stub(stubs, service_body="exit 0\n")
        proc = _run_caller(stubs, service="llm-test-vllm")

    assert _result_name(proc) == "", proc.stdout + proc.stderr
    assert "REACHED_NEXT_STATEMENT" in proc.stdout, (
        'an absent container must not abort the caller -- `[[ -z "$worker" ]]` branches '
        f"downstream of this depend on it returning cleanly.\n{proc.stdout}{proc.stderr}"
    )


def test_a_genuine_docker_failure_still_propagates_to_the_caller() -> None:
    """The fix must remove the SIGPIPE source WITHOUT muting real failures.

    The obvious way to stop an assignment aborting is `|| true`, and it would pass all three
    tests above while converting "the daemon is unreachable" into "no such container" --
    turning an outage into a silently empty answer at fifteen call sites. The old pipeline
    propagated a real `docker ps` failure (pipefail takes the last non-zero status, and
    `head` succeeds), so the new code must too.
    """
    with _stub_dir() as stubs:
        _write_docker_stub(stubs, service_body="exit 1\n")
        proc = _run_caller(stubs)

    assert "REACHED_NEXT_STATEMENT" not in proc.stdout, (
        "a failing `docker ps` was swallowed -- an unreachable daemon now reads as 'this "
        "service has no container', which is indistinguishable from the healthy negative "
        f"answer.\n{proc.stdout}{proc.stderr}"
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_the_library_still_exposes_the_function_this_file_drives() -> None:
    """Guard the guard: a rename would make every test above pass vacuously by erroring in
    the same way a broken library does, so assert the symbol exists on its own."""
    text = COMPOSE_PROJECT_LIB.read_text(encoding="utf-8")
    assert "\noverlay_container_name() {" in text
    assert "\ncompose_project_name() {" in text


def test_the_function_does_not_end_in_a_pipeline() -> None:
    """The structural half of the invariant.

    The behavioural tests above need a multi-container race to reproduce, and a race that
    does not fire is indistinguishable from a fix. This checks the property directly: no
    pipeline into an early-exiting reader anywhere in the function, so its return status
    cannot become a producer's SIGPIPE.
    """
    text = COMPOSE_PROJECT_LIB.read_text(encoding="utf-8")
    start = text.index("\noverlay_container_name() {")
    body = text[start : text.index("\n}\n", start)]
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    for reader in ("| head", "|head", "| grep -q", "| sed -n"):
        assert reader not in code, (
            f"`{reader}` is back in overlay_container_name. A pipeline into a reader that "
            "stops before EOF makes SIGPIPE (141) this function's return status, and its "
            f"callers capture that into an assignment under `set -e`:\n{code}"
        )


# --------------------------------------------------------------------------- #


class _stub_dir:
    """A temp directory on PATH holding stub executables, removed on exit."""

    def __init__(self) -> None:
        self._tmp: str | None = None

    def __enter__(self) -> Path:
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="ot-compose-project-stub-")
        return Path(self._tmp)

    def __exit__(self, *exc: object) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
