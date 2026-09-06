"""``diar-server`` must LOAD the bundled OpenBLAS, not merely ship it (issue #721).

WHY A SEPARATE RUNTIME TEST
---------------------------
``tests/unit/test_diar_native_openblas_bundled.py`` asserts the Dockerfiles are wired to
bundle diar-native's validated **0.3.26** and to point ``diar-server``'s DT_RPATH at it.
That is a claim about the *build*. This module is the claim about the *run*, and the two
are not the same: a Dockerfile that copies a library into an image but whose process still
maps ``libopenblasp-r0.3.29.so`` is a **silent no-op that looks exactly like success** —
same files on disk, same green build, same passing smoke test. The only honest answer to
"which OpenBLAS did this run use" is the one the loader resolved, read out of
``/proc/<pid>/maps``.

That is also how the measurement on this issue was taken (``scripts/diar-openblas-der-ab.py``
does the identical read), so the test and the evidence agree on their method.

WHAT IS AND IS NOT COVERED
--------------------------
⚠️ This asserts **which library is loaded**, not that loading a different one would be wrong.
Both versions were measured at **0.0669 DER** — on amd64 *and* on native aarch64 (Apple M2
Max) — so this is not guarding an observed accuracy regression. It guards the **pinning**:
``libopenblas0`` is unpinned in both Dockerfiles, so without the bundled copy the shipped
pairing is whatever the base image last installed, and Graviton3/4's SVE kernels (which no
available hardware can execute) stay covered by upstream's validation rather than by nothing.
The residual architecture-blocked gap is tracked by **#713**.

⚠️ These runs are amd64. QEMU could not substitute even if it were tried — OpenBLAS
dispatches kernels by runtime CPU detection, so an emulated run exercises the wrong path.

⚠️ Do not reach for ``tests/integration/test_boundary_regression.py`` as coverage for any of
this. It replays frozen ``*.rawinfer.json`` inference and never runs the diarizer, so it is
structurally incapable of seeing an inference-time BLAS regression.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from tests.compose_project import compose_service_container

pytestmark = pytest.mark.integration

#: The private directory DT_RPATH names. Deliberately NOT on any image-wide LD_LIBRARY_PATH.
BUNDLED_DIR = "/opt/diar-native/lib"

#: First OpenBLAS release upstream root-causes its arm64-only GEMM->GEMV report to (AMI-16
#: DER 13.8% -> 48.7%, with ``verify-models`` passing every stage throughout). Not reproduced
#: on any hardware available here -- see this module's docstring.
FIRST_DEFECTIVE_VERSION = (0, 3, 28)

_OPENBLAS_PATH = re.compile(r"/\S*libopenblas\S*")
_OPENBLAS_VERSION = re.compile(r"libopenblasp-r(\d+)\.(\d+)\.(\d+)\.so")

_DOCKER_TIMEOUT = 60


def _run(argv: list[str], timeout: int = _DOCKER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603 -- fixed argv, no shell
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def _mapped_openblas(container: str, pid: str = "1") -> set[str]:
    """The libopenblas paths the process has actually MAPPED.

    Read from ``/proc/<pid>/maps`` rather than from ``ldd``, ``dpkg`` or the Dockerfile:
    those describe intent, this describes what happened.
    """
    proc = _run(["docker", "exec", container, "cat", f"/proc/{pid}/maps"])
    return set(_OPENBLAS_PATH.findall(proc.stdout))


def _unbundled(mapped: set[str]) -> list[str]:
    """Mapped OpenBLAS paths that did NOT come from the private bundled directory."""
    return sorted(path for path in mapped if not path.startswith(f"{BUNDLED_DIR}/"))


def _versions(mapped: set[str]) -> list[tuple[int, int, int]]:
    """The ``libopenblasp-rX.Y.Z.so`` versions readable from the mapped paths."""
    found = (_OPENBLAS_VERSION.search(path) for path in sorted(mapped))
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3))) for m in found if m]


#: Shared so the sidecar case and the throwaway case cannot drift in strictness. Kept as
#: pure predicates rather than an assert-helper: each test then carries its own visible,
#: falsifiable assertions instead of delegating them somewhere a reader (and the test
#: auditor) cannot see them.
_REBUILD_HINT = (
    "The image's own apt libopenblas0 is UNPINNED and currently Debian trixie's 0.3.29+ds-3, "
    "outside the envelope diar-native validates on (Ubuntu 24.04's 0.3.26). Both versions "
    "measure 0.0669 DER on the hardware available here, so this is a pinning failure rather "
    "than a known accuracy regression -- but it means the shipped pairing is whatever the "
    "base image last installed, and it leaves Graviton3/4's SVE kernels (unexecutable here) "
    "outside upstream's validation. If this image predates the change, rebuild it: "
    "`./opentr.sh rebuild-backend` (issue #721 landed the bundling in "
    "backend/Dockerfile.prod and backend/Dockerfile.lite)."
)

_NOT_MEASURED_HINT = (
    "This is NOT MEASURED rather than a pass — diar-server carries an ELF NEEDED entry for "
    "libopenblas.so.0, so a live process always has one mapped. Something read the wrong pid "
    "or the wrong container."
)


def test_the_running_diar_native_sidecar_maps_the_bundled_openblas() -> None:
    """The production path: the process that actually answers ``/diarize``.

    Skips when the sidecar is not up, because there is then nothing to observe — but the
    throwaway test below still runs against the same image, so a down sidecar never leaves
    this file with nothing measured.
    """
    # `or ""` rather than relying on narrowing: mypy does not treat pytest.skip as NoReturn,
    # and this is the idiom test_export_toolchain_in_shipped_images.py already uses.
    container = compose_service_container("diar-native") or ""
    if not container:
        pytest.skip("no running diar-native sidecar in this compose project")

    mapped = _mapped_openblas(container)
    assert len(mapped) >= 1, (
        f"diar-native sidecar ({container}) mapped no OpenBLAS. {_NOT_MEASURED_HINT}"
    )
    assert _unbundled(mapped) == [], (
        f"diar-native sidecar ({container}) mapped OpenBLAS from outside {BUNDLED_DIR}: "
        f"{_unbundled(mapped)}. {_REBUILD_HINT}"
    )
    versions = _versions(mapped)
    assert len(versions) >= 1, (
        f"diar-native sidecar ({container}) mapped {sorted(mapped)}, none carrying a "
        f"libopenblasp-rX.Y.Z.so version in its filename. The soname link must point at the "
        f"VERSIONED file so the version that ran is readable here."
    )
    assert max(versions) < FIRST_DEFECTIVE_VERSION, (
        f"diar-native sidecar ({container}) mapped OpenBLAS {max(versions)} from "
        f"{BUNDLED_DIR} — the BUNDLED copy is itself at or past "
        f"{'.'.join(map(str, FIRST_DEFECTIVE_VERSION))} — the release upstream implicates "
        f"in its arm64 GEMM->GEMV report. Bundling that version defeats the whole point: "
        f"the bundled copy exists to BE the version diar-native validated on."
    )


#: Starts ``diar-server`` **inside the backend container**, inheriting that container's own
#: environment, and reports the libopenblas it mapped.
#:
#: Run in-container rather than as a `docker run` + `docker exec` poll for one measured
#: reason: with no model set the process exits in about a second, and each `docker exec`
#: round trip costs a few hundred milliseconds, so an out-of-container poll loses the race
#: and reports "no OpenBLAS mapped" — NOT MEASURED dressed as a failure. The loop below runs
#: in the same namespace as the process it watches, so it reads `/proc/<pid>/maps` while the
#: dynamic loader's work is still on display.
#:
#: `DIAR_MODELS_DIR` deliberately points at nothing: this test is about which library the
#: loader resolved, which is settled before `main()` runs, so the process is expected to die
#: shortly afterwards. `DIAR_MODE=cpu` keeps it off the GPU the workers are using.
_SUBPROCESS_MAPS_PROBE = (
    "import json, os, subprocess, time\n"
    "env = dict(os.environ)\n"
    "env['DIAR_MODE'] = 'cpu'\n"
    "env['DIAR_MODELS_DIR'] = '/tmp/ot-blas721-no-models'\n"
    "env['DIAR_BIND'] = '127.0.0.1:18701'\n"
    "proc = subprocess.Popen(['/usr/local/bin/diar-server', 'serve'],\n"
    "                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)\n"
    "found, deadline = set(), time.time() + 30\n"
    "while time.time() < deadline:\n"
    "    try:\n"
    "        maps = open(f'/proc/{proc.pid}/maps').read()\n"
    "    except OSError:\n"
    "        break\n"
    "    found = {t for line in maps.splitlines() for t in line.split()\n"
    "             if t.startswith('/') and 'libopenblas' in t}\n"
    "    if found or proc.poll() is not None:\n"
    "        break\n"
    "proc.kill()\n"
    "proc.wait(timeout=10)\n"
    "print(json.dumps(sorted(found)))\n"
)


def test_diar_server_started_as_a_backend_subprocess_maps_the_bundled_openblas() -> None:
    """The invocation a service-scoped ``LD_LIBRARY_PATH`` would NOT have covered.

    ``app/transcription/native_provision.py`` runs ``diar-server provision-models`` as a
    subprocess of the **backend**, with ``env=_subprocess_env()`` = ``dict(os.environ)`` —
    the backend's environment, whose image-wide ``LD_LIBRARY_PATH`` names only the torch CUDA
    wheel directories, never ``/opt/diar-native/lib``. And that subprocess is what EXPORTS the
    ONNX/PLDA model set the sidecar later serves, so getting it wrong would corrupt model
    *creation* while the serving path looked fixed.

    This reproduces that shape exactly — same container, same inherited environment, real
    binary — and asserts the bundled library still wins. It is the case that makes DT_RPATH
    the right mechanism rather than a compose env var, so if it ever goes red while the
    sidecar test stays green, the fix has been quietly downgraded to the env-only version.
    """
    backend = compose_service_container("backend") or ""
    if not backend:
        pytest.skip("no running backend container in this compose project — stack is down")

    proc = _run(["docker", "exec", backend, "python3", "-c", _SUBPROCESS_MAPS_PROBE], timeout=120)
    assert proc.returncode == 0, (
        f"the in-container probe failed in {backend}: {proc.stderr[-800:]!r}"
    )
    mapped = set(json.loads(proc.stdout.strip().splitlines()[-1]))

    assert len(mapped) >= 1, (
        f"diar-server as a subprocess of {backend} mapped no OpenBLAS. {_NOT_MEASURED_HINT}"
    )
    assert _unbundled(mapped) == [], (
        f"diar-server as a subprocess of {backend} mapped OpenBLAS from outside "
        f"{BUNDLED_DIR}: {_unbundled(mapped)}. A compose-scoped LD_LIBRARY_PATH does not "
        f"reach this invocation, which is precisely why the guarantee is carried by the "
        f"binary's DT_RPATH. {_REBUILD_HINT}"
    )
    versions = _versions(mapped)
    assert len(versions) >= 1, (
        f"diar-server as a subprocess of {backend} mapped {sorted(mapped)}, none carrying a "
        f"libopenblasp-rX.Y.Z.so version in its filename."
    )
    assert max(versions) < FIRST_DEFECTIVE_VERSION, (
        f"diar-server as a subprocess of {backend} mapped OpenBLAS {max(versions)} from "
        f"{BUNDLED_DIR} — the BUNDLED copy is itself at or past "
        f"{'.'.join(map(str, FIRST_DEFECTIVE_VERSION))} — the release upstream implicates "
        f"in its arm64 GEMM->GEMV report. The bundled copy exists to BE the version "
        f"diar-native validated on; bundling that one defeats the point."
    )


def test_numpy_and_scipy_are_undisturbed_by_the_bundled_openblas() -> None:
    """The blast-radius check: one shared image runs the API, the workers AND the sidecar.

    Bundling was scoped to ``diar-server``'s own DT_RPATH precisely so nothing else in this
    image changed. This asserts that from the other side — numpy and scipy must still load
    their OWN vendored builds (they never used the system copy) and must still compute
    correctly. Asserting only "the import worked" would pass against a silently wrong BLAS,
    which is the failure mode this whole issue is about, so real linear algebra with an
    exactly-known answer is checked too.
    """
    backend = compose_service_container("backend") or ""
    if not backend:
        pytest.skip("no running backend container in this compose project — stack is down")

    probe = (
        "import json, numpy as np\n"
        "from scipy import linalg\n"
        "a = np.arange(9, dtype=float).reshape(3, 3)\n"
        "ident = np.eye(3)\n"
        "trace3 = float(np.trace(np.array([[2.0, 0, 0], [0, 3.0, 0], [0, 0, 4.0]]) @ ident))\n"
        "det = float(linalg.det(np.eye(5) * 3.0))\n"
        "roundtrip = bool(np.allclose(a @ ident, a))\n"
        "b = np.array([[4.0, 7.0], [2.0, 6.0]])\n"
        "inv_ok = bool(np.allclose(b @ linalg.inv(b), np.eye(2)))\n"
        "maps = open('/proc/self/maps').read()\n"
        "libs = sorted({t for line in maps.splitlines() for t in line.split()\n"
        "               if t.startswith('/') and 'openblas' in t})\n"
        "print(json.dumps({'trace3': trace3, 'det': det, 'roundtrip': roundtrip,\n"
        "                  'inv_ok': inv_ok, 'libs': libs}))\n"
    )
    proc = _run(["docker", "exec", backend, "python3", "-c", probe], timeout=180)
    assert proc.returncode == 0, f"numpy/scipy probe failed in {backend}: {proc.stderr[-800:]!r}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["trace3"] == pytest.approx(9.0), (
        f"numpy matmul against the identity is wrong: trace {result['trace3']} != 9.0. "
        f"BLAS in the backend image is broken."
    )
    assert result["det"] == pytest.approx(243.0), (
        f"scipy det(3*I5) is {result['det']}, not 3**5 = 243. LAPACK in the backend image is broken."
    )
    assert result["roundtrip"], "A @ I != A — numpy's BLAS is producing wrong results"
    assert result["inv_ok"], "B @ inv(B) != I — scipy's LAPACK is producing wrong results"

    intruders = sorted(lib for lib in result["libs"] if lib.startswith(f"{BUNDLED_DIR}/"))
    assert not intruders, (
        f"numpy/scipy in {backend} loaded OpenBLAS from {BUNDLED_DIR}: {intruders}. The "
        f"bundled 0.3.26 is scoped to diar-server's DT_RPATH and must never reach Python — "
        f"that would mean it landed on an image-wide LD_LIBRARY_PATH or in the ld cache, "
        f"widening the blast radius of a diarization fix to every process in the image."
    )
    assert result["libs"], (
        f"numpy/scipy in {backend} mapped no OpenBLAS at all. Expected their own vendored "
        f"builds (numpy.libs/libscipy_openblas64_-*.so, scipy.libs/libscipy_openblas-*.so); "
        f"finding none means this probe measured nothing rather than passing."
    )
