"""Provision the native diarizer's ONNX/PLDA model set at API startup.

The native diarizer (``diar-native``) serves ONNX graphs exported from the gated
``pyannote/speaker-diarization-community-1`` weights. Those graphs are **not**
distributable — upstream ships ``pytorch_model.bin`` and there is no ``.onnx`` on
HuggingFace for any of them — so every operator exports locally under their own token.
The conversion is not an optimisation; it is what makes the native engine fast, and
without it ``${MODEL_CACHE_DIR}/diar-native`` stays empty and the pipeline silently
falls back to in-process PyAnnote (issues #654, #639).

Nothing in this repo produced that directory before this module: five files advertised
a ``download-models diar-native`` command that did not exist.

**Where the work happens.** ``diar-server`` carries the whole export recipe inside
itself — ``provision/exporter.rs`` embeds all five Python scripts and materialises them
to a private temp dir at run time — so this module adds no export logic of its own. It
locates the binary, hands it a writable directory and a smoke clip, and translates its
typed exit codes into one log line an operator can act on. What the binary cannot carry
is the *Python packages* those embedded scripts import, which is why ``requirements.txt``
pins ``onnxscript``, ``onnxslim`` and ``onnxconverter-common``.

**Idempotency is the binary's, not ours.** ``provision-models`` short-circuits on a
valid ``diar-provision.json`` marker and only ``--force`` re-exports, so calling this on
every boot costs a ``stat`` pass once the models are there. That also means the upgrade
path needs no special case: a directory from before the marker existed is simply *not
valid*, so the same call re-exports it. ``verify-models`` cannot shortcut that — on a
marker-less directory it exits 10 (``UNVERIFIABLE``) rather than attesting.

**This never aborts startup.** A failure here means the sidecar's ``/readyz`` returns
503 and ``diarizer_native.sidecar_ready()`` routes to PyAnnote, which is a supported
configuration. Refusing to boot over it would turn a degraded diarizer into an outage.
The loud refusal belongs in the installer's upgrade preflight, where the operator can
still act, not here (issue #670).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - diar-server invoked with a fixed argv list, no shell
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.constants import DEFAULT_DIAR_NATIVE_MODEL_SET
from app.core.constants import DEFAULT_DIAR_NATIVE_PROVISION_TIMEOUT_S

logger = logging.getLogger(__name__)

#: Marker ``diar-server`` writes to vouch for an export. Its schema is upstream's.
MARKER_FILENAME = "diar-provision.json"

#: Baked into the backend image by ``Dockerfile.prod`` from the pinned diar-native
#: stage. The standalone diar-server images carry one; an image that only copies the
#: binary out of them does not, so provisioning from here must pass ``--smoke-clip``.
DEFAULT_SMOKE_CLIP = "/usr/local/share/diar-native/smoke.wav"

#: Container path the models directory is mounted at. ``DIAR_MODELS_DIR`` is
#: ``diar-server``'s own variable and the sidecar reads the identical name, so both
#: containers agree on the location by construction.
DEFAULT_MODELS_DIR = "/models"

#: ``crates/diar-core/src/provision/mod.rs::exit`` — a stable contract, branched on
#: rather than parsed out of the message text.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SMOKE_FAILED = 3
EXIT_EXPORT_FAILED = 4
EXIT_TOKEN_DENIED = 5
EXIT_NO_EXPORTER_ENV = 6
EXIT_NOT_WRITABLE = 7
EXIT_DEVICE_UNAVAILABLE = 9

#: What an operator should do about each failure. The binary emits its own actionable
#: message; these add the OpenTranscribe-specific remedy it cannot know about.
_REMEDY: dict[int, str] = {
    EXIT_TOKEN_DENIED: (
        "Set HUGGINGFACE_TOKEN in .env to a HuggingFace READ token, and — signed in as "
        "that same account — accept the terms at "
        "https://huggingface.co/pyannote/speaker-diarization-community-1. The gate is "
        "per-account and auto-approved; a valid token whose account never accepted it "
        "fails identically."
    ),
    EXIT_NO_EXPORTER_ENV: (
        "The export needs python3 with torch, pyannote.audio, onnx, onnxscript, "
        "onnxslim and onnxconverter-common. They are pinned in backend/requirements.txt "
        "— rebuild the backend image (./opentr.sh rebuild-backend). On a --lite "
        "deployment they are deliberately absent; point DIAR_NATIVE_MODELS_DIR at an "
        "export produced by a full image instead."
    ),
    EXIT_NOT_WRITABLE: (
        "The models directory must be read-write for this step. Check the backend "
        "service's bind mount and run scripts/fix-model-permissions.sh."
    ),
    EXIT_DEVICE_UNAVAILABLE: (
        "Provisioning runs on CPU and needs no GPU. Seeing this means DIAR_MODE was "
        "forced to an unusable device."
    ),
}


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of one ``ensure_native_models`` call.

    Attributes:
        status: ``ok`` (models present and vouched for), ``skipped`` (deliberately not
            attempted), or ``failed`` (attempted, did not produce a usable set).
        exit_code: ``diar-server``'s exit code, or None when nothing was run.
        reason: One human sentence. Safe to log; never carries the token.
        models_dir: The directory that was targeted.
        already_provisioned: True when a valid marker short-circuited the export.
        duration_s: Wall-clock seconds spent in the subprocess.
    """

    status: str
    exit_code: int | None
    reason: str
    models_dir: str
    already_provisioned: bool = False
    duration_s: float = 0.0


def models_dir() -> str:
    """Return the container path the diar-native model set lives at."""
    return os.environ.get("DIAR_MODELS_DIR", DEFAULT_MODELS_DIR)


def auto_provision_enabled() -> bool:
    """Whether startup should provision the model set.

    Mirrors ``RUN_MIGRATIONS_ON_STARTUP``: a self-hosted deployment runs one backend
    that owns this, but on an orchestrated deploy several API replicas would race each
    other writing the same files. Set ``DIAR_NATIVE_AUTO_PROVISION=false`` there and let
    a job own it.
    """
    raw = os.environ.get("DIAR_NATIVE_AUTO_PROVISION", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def read_marker(directory: str | None = None) -> dict | None:
    """Return the parsed provisioning marker, or None when there is not a readable one.

    Args:
        directory: Model set directory. Defaults to :func:`models_dir`.

    Returns:
        The decoded marker, or None if absent or unparseable. A malformed marker is
        deliberately indistinguishable from a missing one here: both mean "this
        directory has not been vouched for", and only ``diar-server`` adjudicates why.
    """
    path = Path(directory or models_dir()) / MARKER_FILENAME
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _smoke_clip() -> str | None:
    """Locate the verification clip, or None when the image does not carry one."""
    candidate = os.environ.get("DIAR_NATIVE_SMOKE_CLIP", DEFAULT_SMOKE_CLIP)
    return candidate if candidate and Path(candidate).is_file() else None


def _subprocess_env() -> dict[str, str]:
    """Build the child environment.

    The token is passed through the environment and never on the command line, which is
    visible to every process on the box via ``ps``. ``diar-server`` consults
    ``HF_TOKEN``, ``HUGGINGFACE_TOKEN`` and ``HUGGING_FACE_HUB_TOKEN`` itself, so
    nothing is renamed here.
    """
    env = dict(os.environ)
    # huggingface_hub reads HF_ENDPOINT with a default, so a key that is *set but
    # empty* resolves to "" and breaks the download with no useful error. Unset beats
    # blank.
    if not env.get("HF_ENDPOINT", "").strip():
        env.pop("HF_ENDPOINT", None)
    return env


def _describe(exit_code: int, payload: dict | None, stderr: str) -> str:
    """Compose one operator-facing sentence from the binary's own output."""
    message = ""
    if payload:
        message = str(payload.get("message") or "").strip()
    if not message:
        message = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    remedy = _REMEDY.get(exit_code, "")
    return " ".join(part for part in (message, remedy) if part) or "no detail reported"


def ensure_native_models(  # noqa: PLR0911 - one branch per distinct operator outcome
    directory: str | None = None,
    *,
    force: bool = False,
) -> ProvisionResult:
    """Make the diar-native model set present and vouched for.

    Idempotent: a valid marker short-circuits inside ``diar-server``, so the steady-state
    cost is a ``stat`` pass. Never raises — every failure is reported as a
    :class:`ProvisionResult` so a degraded diarizer cannot become a failed boot.

    Args:
        directory: Model set directory. Defaults to :func:`models_dir`.
        force: Re-export even when the existing marker is valid.

    Returns:
        The outcome, already logged at an appropriate level.
    """
    target = directory or models_dir()

    if not auto_provision_enabled():
        reason = (
            "DIAR_NATIVE_AUTO_PROVISION is off — skipping; a job or operator is "
            "expected to own the export."
        )
        logger.info("diar-native provisioning skipped: %s", reason)
        return ProvisionResult("skipped", None, reason, target)

    if os.environ.get("DEPLOYMENT_MODE", "").strip().lower() == "lite":
        # The lite image carries diar-server so it can SERVE on the CPU provider (#660),
        # but deliberately not onnx/onnxruntime/onnxscript/onnxslim/onnxconverter-common
        # — roughly 250 MB of export-only dependencies on an image whose whole purpose is
        # to be small. Shelling out anyway would exit 6 on every boot and tell the
        # operator to rebuild, which is the wrong advice here.
        reason = (
            "lite deployment — the export toolchain is intentionally absent. Point "
            "DIAR_NATIVE_MODELS_DIR at a set exported by a full image to serve natively."
        )
        logger.info("diar-native provisioning skipped: %s", reason)
        return ProvisionResult("skipped", None, reason, target)

    binary = shutil.which("diar-server")
    if binary is None:
        reason = "no diar-server binary in this image; the native diarizer is unavailable here."
        logger.info("diar-native provisioning skipped: %s", reason)
        return ProvisionResult("skipped", None, reason, target)

    clip = _smoke_clip()
    if clip is None:
        reason = (
            f"no verification clip at {DEFAULT_SMOKE_CLIP} — the image was built without "
            "it. Provisioning cannot verify an export, so it is not attempted."
        )
        logger.warning("diar-native provisioning skipped: %s", reason)
        return ProvisionResult("skipped", None, reason, target)

    argv = [
        binary,
        "provision-models",
        "--models-dir",
        target,
        "--set",
        os.environ.get("DIAR_NATIVE_MODEL_SET", DEFAULT_DIAR_NATIVE_MODEL_SET),
        # Stages 1, 2, 3 and 5 always run on the statically-linked CPU provider and the
        # export itself does pipeline.to("cpu"), so provisioning needs no accelerator.
        # Pinning cpu here also keeps it off the single GPU the workers are using.
        "--mode",
        "cpu",
        "--smoke-clip",
        clip,
        "--json",
    ]
    if force:
        argv.append("--force")

    had_marker = read_marker(target) is not None
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=_timeout_s(),
            env=_subprocess_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        reason = (
            f"export exceeded {_timeout_s()}s. A cold export writes ~484 MB and normally "
            "takes a couple of minutes; a stall this long usually means the HuggingFace "
            "download is hanging."
        )
        logger.error("diar-native provisioning failed: %s", reason)
        return ProvisionResult("failed", None, reason, target, duration_s=elapsed)
    except OSError as exc:
        elapsed = time.perf_counter() - started
        reason = f"could not run {binary}: {exc}"
        logger.error("diar-native provisioning failed: %s", reason)
        return ProvisionResult("failed", None, reason, target, duration_s=elapsed)

    elapsed = time.perf_counter() - started
    payload = _parse_json(completed.stdout)

    if completed.returncode != EXIT_OK:
        reason = _describe(completed.returncode, payload, completed.stderr)
        logger.error(
            "diar-native provisioning failed (exit %s): %s Diarization falls back to the "
            "in-process PyAnnote engine until this is resolved.",
            completed.returncode,
            reason,
        )
        return ProvisionResult("failed", completed.returncode, reason, target, duration_s=elapsed)

    # A short-circuit and a real export both exit 0; the marker's mtime is the only
    # thing that distinguishes them, and "was there a marker before we ran" is cheaper
    # and does not depend on filesystem timestamp granularity.
    if had_marker and not force:
        logger.info(
            "diar-native models already provisioned at %s (marker valid, %.2fs).",
            target,
            elapsed,
        )
        return ProvisionResult("ok", EXIT_OK, "already provisioned", target, True, elapsed)

    _log_export_summary(target, elapsed)
    return ProvisionResult("ok", EXIT_OK, "exported", target, False, elapsed)


def _timeout_s() -> int:
    """Wall-clock budget for one export, from the environment."""
    raw = os.environ.get("DIAR_NATIVE_PROVISION_TIMEOUT_S", "")
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_DIAR_NATIVE_PROVISION_TIMEOUT_S
    return parsed if parsed > 0 else DEFAULT_DIAR_NATIVE_PROVISION_TIMEOUT_S


def _parse_json(stdout: str) -> dict | None:
    """Decode the binary's JSON result, tolerating a non-JSON line around it."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _log_export_summary(target: str, elapsed: float) -> None:
    """Report what the export actually produced.

    ``gender_precision`` is load-bearing: without ``onnxconverter_common`` the export
    emits **no error** and silently ships the 379 MB fp32 gender classifier instead of
    the 189 MB fp16 one, costing roughly 500 MiB of VRAM for the life of the
    deployment. The marker is the only place that difference is visible.
    """
    marker = read_marker(target) or {}
    toolchain = marker.get("toolchain") or {}
    precision = toolchain.get("gender_precision")
    logger.info(
        "diar-native models exported to %s in %.1fs (set=%s, recipe=%s, folder=%s, gender=%s).",
        target,
        elapsed,
        marker.get("model_set", "unknown"),
        marker.get("exporter_version", "unknown"),
        toolchain.get("folder", "unknown"),
        precision or "absent",
    )
    if marker and precision != "fp16":
        logger.warning(
            "diar-native gender classifier was exported at %s, not fp16. That is the "
            "onnxconverter-common fallback: it costs ~500 MiB of extra VRAM and raises "
            "no error of its own. Confirm the package is installed and re-run with "
            "force=True.",
            precision or "an unrecorded precision",
        )
