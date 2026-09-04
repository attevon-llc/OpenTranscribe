"""One test that FAILS when the diar-native sidecar is not the engine (issue #669).

#669's central acceptance criterion is: **"no diarization test passes with the sidecar
deliberately stopped."** Before this file, the branch had none. What it had was the inverse —
five test modules that assert the PyAnnote fallback and therefore pass *because* the sidecar
is unreachable. Taken together the suite was entirely compatible with diar-native being
silently dead on every deployment, which is precisely the failure this whole subsystem is
built to prevent and which was observed live twice (a CPU-routing regression that sent every
`/embed_window` to a 400, and a sidecar whose stale mount set made every job fall back while
every health check stayed green).

WHY THIS IS NOT test_diar_native_smoke_live.py
-----------------------------------------------
That file proves the sidecar *process* holds device memory on the configured GPU. This proves
something strictly different and not implied by it: that **the application's own diarization
call path reached it**. A sidecar can be healthy, GPU-resident and completely unused — that is
exactly what the two live regressions above looked like from the outside.

WHY IT DOES NOT SKIP WHEN THE SIDECAR IS DOWN
----------------------------------------------
Every other infrastructure-dependent test in this tree skips when its dependency is missing,
and that is right for them. It is wrong here, and deliberately so: "the sidecar is down" is
not an absent dependency, it is **the defect under test**. A skip would restore the exact
false-green #669 exists to remove.

The distinction the guards below draw is therefore:
  * stack not running at all      -> SKIP  (nothing is under test; not a diarization claim)
  * stack running, sidecar not used -> FAIL (the claim is false)

Run:
    cd backend && PYTHONPATH=. pytest -m integration \\
        tests/integration/test_diar_native_is_actually_used.py -v
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.integration

COMPOSE_SERVICE = "celery-worker"

# Drives the REAL NativeSpeakerDiarizer through the REAL config, inside the worker, because
# the sidecar publishes no host port (in-network `http://diar-native:8701` only). Hitting the
# sidecar directly from the host would prove the sidecar answers — not that the app asks it,
# which is the entire claim.
#
# allow_local_fallback=False is what makes this falsifiable: with it True the call would
# succeed via PyAnnote on a dead sidecar and only `last_provider` would differ. Both are
# asserted — the exception path AND the provenance — because they fail for different reasons.
_PROBE = """
import sys
sys.path.insert(0, "/app")
import numpy as np
from app.transcription.config import TranscriptionConfig
from app.transcription.diarizer_native import NativeSpeakerDiarizer

cfg = TranscriptionConfig.from_environment()
diarizer = NativeSpeakerDiarizer(cfg)
diarizer.load_model()

# 10 s of 16 kHz mono. Content is irrelevant: the claim is which ENGINE ran, not how well it
# separated speakers (that is diar-native-der-parity.py's job, against ground truth).
audio = np.zeros(16000 * 10, dtype=np.float32)
diarizer.diarize(audio, None, allow_local_fallback=False)
print("PROVIDER=" + str(diarizer.last_provider))
print("MODEL=" + str(diarizer.last_model))
"""


def _docker_ps(*filters: str) -> list[str]:
    args = ["docker", "ps", "--filter", "status=running"]
    for f in filters[:-1]:
        args += ["--filter", f]
    args += ["--format", filters[-1]]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.stdout.strip().splitlines() if ln]


def _container() -> str:
    """The worker in THIS compose project, or '' — never an unscoped name filter.

    A bare `--filter name=celery-worker` matches whatever stack happens to be up on the host;
    this machine runs unrelated compose projects sharing name prefixes, and an unscoped filter
    in opentr.sh once destroyed an unrelated container. The project is resolved the same way
    scripts/lib/compose-project.sh does it — from a running postgres container's own label,
    never from a directory name, which is wrong from a git worktree.
    """
    project = _docker_ps(
        "label=com.docker.compose.service=postgres",
        '{{.Label "com.docker.compose.project"}}',
    )
    if not project:
        return ""
    names = _docker_ps(
        f"label=com.docker.compose.project={project[0]}",
        f"label=com.docker.compose.service={COMPOSE_SERVICE}",
        "{{.Names}}",
    )
    return names[0] if names else ""


def test_the_application_path_actually_reaches_the_sidecar():
    """A real diarize() must be served by diar-native, not by the PyAnnote fallback."""
    container = _container()
    if not container:
        pytest.skip(
            f"no running {COMPOSE_SERVICE} in this compose project — the dev stack is down, so "
            f"there is no diarization claim to falsify (start it with ./opentr.sh start dev)"
        )

    proc = subprocess.run(
        ["docker", "exec", container, "python3", "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert proc.returncode == 0, (
        f"a real NativeSpeakerDiarizer.diarize(allow_local_fallback=False) FAILED inside "
        f"{container}. With the fallback refused, this is what a stopped, unreachable or "
        f"misconfigured sidecar looks like — the exact state in which every other diarization "
        f"test in this suite still passes.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
    )

    provider = next(
        (
            ln.split("=", 1)[1].strip()
            for ln in proc.stdout.splitlines()
            if ln.startswith("PROVIDER=")
        ),
        None,
    )
    assert provider is not None, (
        f"probe produced no PROVIDER line — it did not run to completion.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
    )
    assert provider == "native", (
        f"diarization was served by {provider!r}, not the diar-native sidecar. The engine is "
        f"configured native but the app fell back, which is silent by design and is why this "
        f"assertion exists (issue #669)."
    )


def test_the_provenance_pair_agrees_provider_and_model_together():
    """Control: `last_provider` and `last_model` must BOTH describe the native engine.

    They are assigned together at each of the two exit points (diarizer_native.py:864 for the
    PyAnnote fallback, :1022 for the native path), so asserting only the provider would pass
    against a half-updated pair — exactly the shape #706 fixed.

    ⚠️ The obvious form of this check is wrong, and the first draft shipped it: *"assert
    'pyannote' not in last_model"*. `NATIVE_MODEL_NAME` **is**
    `pyannote/speaker-diarization-community-1`, because the sidecar's ONNX/PLDA graphs are
    derived from exactly those gated weights — naming the upstream repo is correct and is what
    the NOTICE attribution (#663) is about. So the discriminator is not the word "pyannote";
    it is whether the model equals the value the native path assigns. Imported from the
    module rather than spelled here, so a rename cannot leave this test asserting a constant
    the code no longer uses.
    """
    container = _container()
    if not container:
        pytest.skip(f"no running {COMPOSE_SERVICE} in this compose project — dev stack is down")

    expected = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python3",
            "-c",
            "import sys; sys.path.insert(0, '/app'); "
            "from app.transcription.diarizer_native import NATIVE_MODEL_NAME; print(NATIVE_MODEL_NAME)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert expected.returncode == 0, f"could not read NATIVE_MODEL_NAME: {expected.stderr[-500:]!r}"
    native_model = expected.stdout.strip()
    assert native_model, "NATIVE_MODEL_NAME resolved empty — nothing to compare against"

    proc = subprocess.run(
        ["docker", "exec", container, "python3", "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-2000:]!r}"

    model = next(
        (ln.split("=", 1)[1].strip() for ln in proc.stdout.splitlines() if ln.startswith("MODEL=")),
        None,
    )
    assert model, f"probe produced no MODEL line.\nstdout={proc.stdout!r}"
    assert model == native_model, (
        f"provenance reports model {model!r} but the native path assigns {native_model!r}. "
        f"Provider and model are written together, so a disagreement means one half went "
        f"stale (#706)."
    )
