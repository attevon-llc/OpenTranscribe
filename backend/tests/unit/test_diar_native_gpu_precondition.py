"""The diar-native GPU-residency precondition must never make a CPU sidecar on a
GPU host disappear.

``tests/integration/test_diar_native_smoke_live.py`` asserts that the diar-native
sidecar's process holds device memory. Its original fixture skipped for exactly
three things -- no ``nvidia-smi``, no container, a nonexistent GPU index -- and
never asked what mode the sidecar was **configured** for, so:

* a sidecar deliberately in ``DIAR_MODE=cpu`` (CPU-only host, ``--lite``, or an
  explicit ``DIAR_NATIVE_MODE=cpu``) FAILED, blaming "the CUDA execution provider
  did not register" on a container that was never asked to load one; and
* the obvious repair -- skip when the mode isn't ``cuda`` -- would have hidden the
  real 2026-09-07 defect, where the sidecar was on CPU **because
  ``docker-compose.diar-native-gpu.yml`` was never loaded on a GPU host**
  (root cause: ``tests/unit/test_opentr_docker_probe_sigpipe.py``). A skip there
  turns a live misconfiguration into a NOT MEASURED line nobody reads.

So the precondition returns three verdicts, not two, and this file drives the REAL
``classify_gpu_residency_precondition`` -- imported from the integration module, not
restated here -- across every input combination that decides between them.

It is a **unit** test on purpose: the decision is pure, and pinning it must not
depend on a live stack, a GPU, or docker. The docker/`nvidia-smi` readers it sits
behind (``_sidecar_configured_mode``, ``_sidecar_has_device_reservation``,
``_host_has_nvidia_runtime``) are exercised here too, against stub executables on
``PATH``, so their parsing is real code rather than an assumption.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_diar_native_smoke_live import DEFECT
from tests.integration.test_diar_native_smoke_live import EXPECT_CPU_ENV
from tests.integration.test_diar_native_smoke_live import MEASURE
from tests.integration.test_diar_native_smoke_live import SKIP
from tests.integration.test_diar_native_smoke_live import _host_has_nvidia_runtime
from tests.integration.test_diar_native_smoke_live import _sidecar_configured_mode
from tests.integration.test_diar_native_smoke_live import _sidecar_has_device_reservation
from tests.integration.test_diar_native_smoke_live import classify_gpu_residency_precondition

#: Every non-``cuda`` mode the sidecar can legitimately report. ``""`` is the
#: "key absent from the container entirely" case -- an image or overlay change could
#: drop it, and "no answer" must not read as "cuda".
_NON_CUDA_MODES = ("cpu", "mps", "")


def _classify(**overrides: object) -> tuple[str, str]:
    """The real classifier with an all-benign baseline, so each test states only the
    one or two signals it is actually about."""
    kwargs: dict[str, object] = {
        "configured_mode": "cuda",
        "has_device_reservation": True,
        "host_nvidia_runtime": True,
        "force_cpu_mode": False,
        "expect_cpu_optout": False,
    }
    kwargs.update(overrides)
    return classify_gpu_residency_precondition(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the verdict table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", _NON_CUDA_MODES)
def test_a_cpu_sidecar_with_no_reservation_on_a_gpu_host_is_a_defect(mode: str) -> None:
    """THE case this file exists for -- the live 2026-09-07 state.

    An nvidia runtime on the host, no device reservation on the container: the GPU
    overlay was not in the compose chain. That is a product defect, and it must be
    reported as one rather than passed, skipped, or blamed on ONNX Runtime.
    """
    verdict, reason = _classify(
        configured_mode=mode, has_device_reservation=False, host_nvidia_runtime=True
    )

    assert verdict == DEFECT, f"mode={mode!r} on a GPU host with no reservation -> {verdict}"
    assert "docker-compose.diar-native-gpu.yml" in reason, reason
    assert EXPECT_CPU_ENV in reason, (
        "the defect message must name the explicit opt-out, or an operator with a "
        f"legitimately CPU-only GPU host has no way out except deleting the test.\n{reason}"
    )


def test_a_cuda_sidecar_is_measured() -> None:
    """MUST-STAY-CLEAN: the normal, correct deployment still gets asserted on.

    Without this, "always DEFECT" would satisfy the case above.
    """
    verdict, reason = _classify(configured_mode="cuda")

    assert verdict == MEASURE, reason


@pytest.mark.parametrize("mode", _NON_CUDA_MODES)
def test_a_cpu_sidecar_on_a_host_with_no_nvidia_runtime_is_a_named_skip(mode: str) -> None:
    """A CPU-only host is not a defect. It is also not a pass -- the residency claim
    genuinely cannot be made there, so the run must say so with a reason rather than
    assert something it cannot know."""
    verdict, reason = _classify(
        configured_mode=mode, has_device_reservation=False, host_nvidia_runtime=False
    )

    assert verdict == SKIP, reason
    assert "no nvidia runtime" in reason, reason


@pytest.mark.parametrize("mode", _NON_CUDA_MODES)
def test_an_explicit_cpu_override_with_the_gpu_overlay_loaded_is_a_named_skip(mode: str) -> None:
    """``docker-compose.diar-native.yml`` documents that an operator may force
    ``DIAR_NATIVE_MODE=cpu``/``mps`` *with* the GPU overlay loaded. The device
    reservation is what distinguishes that deliberate choice from the missing
    overlay above -- it is the only signal that survives on the container itself."""
    verdict, reason = _classify(
        configured_mode=mode, has_device_reservation=True, host_nvidia_runtime=True
    )

    assert verdict == SKIP, reason
    assert "docker-compose.diar-native-gpu.yml IS loaded" in reason, reason


@pytest.mark.parametrize("mode", _NON_CUDA_MODES)
def test_force_cpu_mode_in_env_is_a_named_skip(mode: str) -> None:
    """``FORCE_CPU_MODE=true`` is the documented ``.env`` opt-out from GPU on a GPU
    host (``opentr.sh``'s ``detect_and_configure_hardware`` honours it), so a CPU
    sidecar there is the requested configuration, not a defect."""
    verdict, reason = _classify(
        configured_mode=mode,
        has_device_reservation=False,
        host_nvidia_runtime=True,
        force_cpu_mode=True,
    )

    assert verdict == SKIP, reason
    assert "FORCE_CPU_MODE" in reason, reason


@pytest.mark.parametrize("mode", _NON_CUDA_MODES)
def test_the_explicit_operator_optout_is_a_named_skip(mode: str) -> None:
    """The escape hatch for the deployment shapes that leave no trace anywhere a
    test can read them (``--lite`` / ``--cpu`` are per-invocation flags)."""
    verdict, reason = _classify(
        configured_mode=mode,
        has_device_reservation=False,
        host_nvidia_runtime=True,
        expect_cpu_optout=True,
    )

    assert verdict == SKIP, reason
    assert EXPECT_CPU_ENV in reason, reason


def test_cuda_mode_is_measured_regardless_of_every_other_signal() -> None:
    """A ``cuda``-configured sidecar is always measured: if it is configured for CUDA
    and holds no device memory, that IS the failure this suite is for, and no opt-out
    should suppress it. Guards against a future signal being wired in ahead of the
    mode check and quietly disarming the whole suite."""
    combos = itertools.product([True, False], repeat=4)
    verdicts = {
        _classify(
            configured_mode="cuda",
            has_device_reservation=reservation,
            host_nvidia_runtime=runtime,
            force_cpu_mode=force_cpu,
            expect_cpu_optout=optout,
        )[0]
        for reservation, runtime, force_cpu, optout in combos
    }

    assert verdicts == {MEASURE}, f"cuda mode produced non-MEASURE verdicts: {verdicts}"


def test_every_non_measure_verdict_carries_a_reason() -> None:
    """A verdict with an empty reason is a silent skip wearing a label. Sweeps the
    whole input space so a new branch cannot be added without one."""
    reasonless = [
        (mode, reservation, runtime, force_cpu, optout)
        for mode in _NON_CUDA_MODES
        for reservation, runtime, force_cpu, optout in itertools.product([True, False], repeat=4)
        if not _classify(
            configured_mode=mode,
            has_device_reservation=reservation,
            host_nvidia_runtime=runtime,
            force_cpu_mode=force_cpu,
            expect_cpu_optout=optout,
        )[1].strip()
    ]

    assert not reasonless, f"verdicts returned with no reason for inputs: {reasonless}"


def test_no_input_combination_yields_a_verdict_outside_the_documented_three() -> None:
    """Guard the guard: the fixture branches on these three strings, so a fourth
    would fall through every branch and the suite would proceed to assert residency
    on a deployment it had just decided it could not measure."""
    verdicts = {
        _classify(
            configured_mode=mode,
            has_device_reservation=reservation,
            host_nvidia_runtime=runtime,
            force_cpu_mode=force_cpu,
            expect_cpu_optout=optout,
        )[0]
        for mode in ("cuda", *_NON_CUDA_MODES)
        for reservation, runtime, force_cpu, optout in itertools.product([True, False], repeat=4)
    }

    assert verdicts <= {MEASURE, SKIP, DEFECT}, verdicts
    assert verdicts == {MEASURE, SKIP, DEFECT}, (
        f"the input sweep never reached one of the three verdicts ({verdicts}) -- either a "
        "branch is unreachable or this sweep stopped covering it"
    )


# --------------------------------------------------------------------------- #
# the readers the verdict is fed from -- real parsing, stub executables
# --------------------------------------------------------------------------- #


def _stub_docker(tmp_path: Path, body: str) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    docker.chmod(0o755)
    return stubs


@pytest.fixture
def stub_path(monkeypatch: pytest.MonkeyPatch):
    """Put a caller-supplied stub directory first on ``PATH``. Nothing in this file
    reaches a real daemon, so it runs in CI (no docker) and is safe beside a live stack."""

    def _install(stubs: Path) -> None:
        monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")

    return _install


def test_the_mode_reader_returns_the_containers_own_diar_mode(tmp_path: Path, stub_path) -> None:
    stub_path(
        _stub_docker(
            tmp_path,
            "printf 'DIAR_MAX_INFLIGHT=2\\nDIAR_MODE=cuda\\nDIAR_DEVICES\\nPATH=/usr/bin\\n'",
        )
    )

    assert _sidecar_configured_mode("any-container") == "cuda"


def test_the_mode_reader_returns_empty_when_the_key_is_absent(tmp_path: Path, stub_path) -> None:
    """``docker-compose.diar-native.yml`` carries bare, valueless entries
    (``DIAR_DEVICES``, ``SPEAKRS_ARENA_SHRINK``) on purpose, so a naive
    ``split("=")`` parser crashes on real input and a prefix match would mistake
    ``DIAR_MODE_EXTRA`` for the key."""
    stub_path(_stub_docker(tmp_path, "printf 'DIAR_DEVICES\\nDIAR_MODE_EXTRA=cuda\\n'"))

    assert _sidecar_configured_mode("any-container") == ""


def test_the_reservation_reader_reads_null_as_absent(tmp_path: Path, stub_path) -> None:
    """``docker inspect --format '{{json .HostConfig.DeviceRequests}}'`` prints the
    JSON literal ``null`` -- the exact output of the misconfigured 2026-09-07
    container -- and a truthiness test on the raw string would call that present."""
    stub_path(_stub_docker(tmp_path, "printf 'null\\n'"))

    assert _sidecar_has_device_reservation("any-container") is False


def test_the_reservation_reader_reads_a_device_request_as_present(
    tmp_path: Path, stub_path
) -> None:
    stub_path(
        _stub_docker(
            tmp_path,
            """printf '[{"Driver":"nvidia","DeviceIDs":["1"],"Capabilities":[["gpu"]]}]\\n'""",
        )
    )

    assert _sidecar_has_device_reservation("any-container") is True


def test_the_runtime_reader_finds_nvidia_among_the_daemons_runtimes(
    tmp_path: Path, stub_path
) -> None:
    stub_path(
        _stub_docker(tmp_path, """printf '{"nvidia":{"path":"nvidia-container-runtime"}}\\n'""")
    )

    assert _host_has_nvidia_runtime() is True


def test_the_runtime_reader_is_false_for_a_daemon_without_it(tmp_path: Path, stub_path) -> None:
    stub_path(_stub_docker(tmp_path, """printf '{"runc":{"path":"runc"}}\\n'"""))

    assert _host_has_nvidia_runtime() is False


def test_the_runtime_reader_fails_soft_when_the_daemon_cannot_answer(
    tmp_path: Path, stub_path
) -> None:
    """A daemon that will not answer must read as False, which only ever SOFTENS the
    verdict (defect -> skip). The opposite default would manufacture a defect report
    out of a docker outage."""
    stub_path(_stub_docker(tmp_path, "exit 1"))

    assert _host_has_nvidia_runtime() is False


def test_the_readers_actually_shell_out(tmp_path: Path, stub_path) -> None:
    """Guard the guard: if a reader stopped invoking ``docker`` (cached, stubbed out,
    short-circuited), every stub-driven test above would keep passing while measuring
    nothing. A stub that records its argv proves the real subprocess call happened."""
    log = tmp_path / "argv.log"
    stub_path(_stub_docker(tmp_path, f'printf "%s\\n" "$*" >> "{log}"\nprintf "null\\n"'))

    _sidecar_configured_mode("some-container")
    _sidecar_has_device_reservation("some-container")

    recorded = log.read_text(encoding="utf-8")
    assert "inspect" in recorded, recorded
    assert "some-container" in recorded, recorded


def test_the_mode_reader_propagates_a_docker_failure(tmp_path: Path, stub_path) -> None:
    """``check=True``: an unreadable container must raise, not return ``""``. An empty
    string is a legitimate value here (key absent), so swallowing the error would let
    "docker is broken" masquerade as a real reading."""
    stub_path(_stub_docker(tmp_path, "exit 1"))

    with pytest.raises(subprocess.CalledProcessError):
        _sidecar_configured_mode("any-container")
