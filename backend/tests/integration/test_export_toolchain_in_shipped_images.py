"""Every shipped image must be able to EXPORT the gated models, not just run them.

This is the test that was missing, and its absence shipped the same defect twice.

WHY THE EXISTING TESTS DID NOT CATCH IT
----------------------------------------
``unit/test_native_provision.py`` fakes the ``diar-server`` subprocess and asserts the Python
wrapper branches correctly on exit codes 0/4/5/6. That is worth testing and it is not this.
It never runs the real binary in a real image, so it passed happily while the image could not
export at all — it was testing the wrapper, not the container's capability.

The capability broke twice on one branch:

1. #660 removed pyannote.audio + the ONNX toolchain from ``requirements-lite.txt`` as "unused
   at runtime". True — and they were also the only thing that could CREATE the models, so lite
   was left with no speaker-voiceprint path whatsoever.
2. The restore then omitted ``onnxruntime``, which the exporter imports **unguarded** in two
   places. The image looked complete and still could not provision.

Neither was visible to any test. Both were found by hand, late.

WHY IT MATTERS ARCHITECTURALLY
--------------------------------
The ONNX/PLDA graphs are non-redistributable derivatives of the gated
``pyannote/speaker-diarization-community-1`` weights: there is nothing to download, so each
install must export them ITSELF. Doing that in the same image that serves them is deliberate —
one image, no separate conversion container, no "which image produced these models?" ambiguity.
That property only holds if the runtime image carries the export dependencies, which is exactly
what this asserts.

WHAT IT ASSERTS
----------------
``provision-models`` with **no token** must reach **EXIT_TOKEN_DENIED (5)** — "everything is
ready, I just have no credentials" — and specifically NOT ``EXIT_NO_EXPORTER_ENV (6)``, which
is the binary's own verdict that the Python export environment is incomplete. That single
distinction is the whole capability, it costs no download, and it is the check that was
missing.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

EXIT_TOKEN_DENIED = 5
EXIT_NO_EXPORTER_ENV = 6

#: Import names the binary's embedded export scripts use. DERIVED, never trimmed by eye — the
#: comment in requirements-lite.txt that trimmed it to four is what let onnxruntime go missing:
#:
#:   docker exec <diar-native-container> sh -c \
#:     'grep -a -oE "^ *(import|from) [a-zA-Z_][a-zA-Z0-9_.]*" /usr/local/bin/diar-server \
#:      | sed -E "s/^ *//" | sort -u'
#:
#: `onnxsim` is deliberately absent: it has no CPython 3.13 wheel, and the exporter falls back
#: to `onnxslim` (bit-exact, differently-shaped graph, recorded in the marker's
#: `toolchain.folder`). Several of these arrive transitively rather than being declared, which
#: is why this is checked against the BUILT IMAGE and not against a requirements file.
EXPORTER_IMPORTS = (
    "torch",
    "torchaudio",
    "pyannote.audio",
    "transformers",
    "huggingface_hub",
    "numpy",
    "onnx",
    "onnxruntime",
    "onnxscript",
    "onnxslim",
    "onnxconverter_common",
)

_IMPORT_PROBE = (
    "import importlib.util as u;"
    f"mods={EXPORTER_IMPORTS!r};"
    "print(','.join(m for m in mods if not u.find_spec(m)))"
)


def _verdict(stdout: str) -> dict | None:
    """Parse provision-models' JSON verdict out of mixed output.

    ⚠️ Not line-by-line. The FAILURE payload is a single line (`{"exit_code":5,...}`) but the
    SUCCESS payload is pretty-printed across ~40 lines, and the run also interleaves `[export]`
    progress lines. A `json.loads` of "the first line starting with {" therefore works for
    every error case and raises JSONDecodeError on every success — which is how this test first
    failed against a perfectly good 157-second export.

    Takes the last balanced object in the stream, so trailing verdict wins over any earlier
    JSON-ish progress line.
    """
    start = stdout.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(stdout)):
            ch = stdout[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(stdout[start : i + 1])
                    except ValueError:
                        break
                    # Only an object is a verdict. json.loads returns Any, and a bare list or
                    # string would otherwise be handed back as if it were the payload, so
                    # every `payload.get(...)` below would raise instead of asserting.
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = stdout.find("{", start + 1)
    return None


def _running_backend() -> str:
    """The backend container of THIS compose project, or '' — scoped by label, never by name."""
    project = (
        subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.service=postgres",
                "--filter",
                "status=running",
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        .stdout.strip()
        .splitlines()
    )
    if not project:
        return ""
    names = (
        subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project[0]}",
                "--filter",
                "label=com.docker.compose.service=backend",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        .stdout.strip()
        .splitlines()
    )
    return names[0] if names else ""


def test_the_running_backend_image_has_every_exporter_dependency():
    """The full image must be able to export — it is what a normal install provisions with."""
    container = _running_backend()
    if not container:
        pytest.skip("no running backend container in this compose project — stack is down")

    proc = subprocess.run(
        ["docker", "exec", container, "python3", "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-800:]!r}"
    missing = [m for m in proc.stdout.strip().split(",") if m]
    assert not missing, (
        f"{container} is missing exporter dependencies: {missing}. The gated models cannot be "
        f"exported by the image that serves them, so a fresh install has no way to produce "
        f"them at all — they are non-redistributable derivatives with nothing to download. "
        f"Pin the missing package in backend/requirements.txt and rebuild."
    )


@pytest.mark.slow
def test_the_running_backend_actually_completes_a_real_export():
    """End-to-end capability: a REAL export must succeed in the image that serves the models.

    ⚠️ THE OBVIOUS CHEAP VERSION OF THIS TEST CANNOT FAIL, and this file shipped it for one
    commit. It ran ``provision-models`` with NO token and asserted TOKEN_DENIED (5) rather
    than NO_EXPORTER_ENV (6), reasoning that 5 means "environment ready, only credentials
    missing". Measured: the binary validates the TOKEN BEFORE it preflights the export
    environment, so a token-less run returns 5 even with ``python3`` replaced by ``exit 1`` —
    verified by putting a stub python3 first on PATH. The assertion was true regardless of the
    thing it claimed to measure. ``diar-server`` exposes no exporter-preflight subcommand
    (``serve``, ``provision-models``, ``verify-models``, ``check-token``), so there is no cheap
    honest form of this check.

    So this one does the real thing, and is opt-in because it downloads the gated weights and
    takes ~150 s: ``RUN_EXPORT_CAPABILITY_TEST=1``. The fast, always-on guard is the import
    probe above, which IS falsifiable — a missing package makes it red.

    Exports into a throwaway directory, never the live model set.
    """
    if os.environ.get("RUN_EXPORT_CAPABILITY_TEST") != "1":
        pytest.skip(
            "set RUN_EXPORT_CAPABILITY_TEST=1 to run a real gated-model export (~150 s, "
            "downloads weights). The import probe in this module is the everyday guard."
        )
    container = _running_backend()
    if not container:
        pytest.skip("no running backend container in this compose project — stack is down")

    probe_dir = "/tmp/export-capability-probe"  # noqa: S108 - path inside the container
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            f"rm -rf {probe_dir}; mkdir -p {probe_dir}; "
            f'HF_TOKEN="$HUGGINGFACE_TOKEN" diar-server provision-models '
            f"--models-dir {probe_dir} --mode cpu --json",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    payload = _verdict(proc.stdout)
    subprocess.run(["docker", "exec", container, "rm", "-rf", probe_dir], check=False, timeout=60)

    assert payload is not None, (
        f"provision-models emitted no JSON verdict.\nstdout={proc.stdout[-1500:]!r}\n"
        f"stderr={proc.stderr[-1500:]!r}"
    )
    # The PROCESS exit status is authoritative. The JSON carries `exit_code` only on the
    # FAILURE path — a successful export emits the marker (model_set, toolchain, smoke,
    # gender_precision) with no exit_code field at all, so reading it from the payload gave
    # `None` and failed a genuinely successful 157-second export.
    code = proc.returncode if payload.get("exit_code") is None else payload["exit_code"]
    assert code != EXIT_NO_EXPORTER_ENV, (
        f"provision-models exited NO_EXPORTER_ENV ({EXIT_NO_EXPORTER_ENV}): the image cannot "
        f"export the gated models it serves. message={payload.get('message')!r}"
    )
    if code == EXIT_TOKEN_DENIED:
        pytest.skip(
            "HUGGINGFACE_TOKEN is unset, or its account has not accepted the "
            "pyannote/speaker-diarization-community-1 gate — no real export is possible"
        )
    assert code == 0, f"export failed with exit {code}: {payload.get('message')!r}"
    assert payload.get("gender_precision") == "fp16", (
        f"gender model exported at {payload.get('gender_precision')!r}, not fp16 — that means "
        f"onnxconverter_common is missing, and its absence is SILENT: the export ships a "
        f"379 MB fp32 model instead of 189 MB, costing ~500 MiB of VRAM forever."
    )
