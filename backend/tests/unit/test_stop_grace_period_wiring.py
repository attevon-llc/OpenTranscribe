"""`stop_grace_period` must exist on every CUDA-holding service, and every source that
carries the default value must agree with the others (issue #782).

Three independent files declare the same number today: `docker-compose.yml`'s
`${OT_STOP_GRACE_GPU:-Ns}` compose default, `scripts/common.sh`'s
`OT_STOP_GRACE_GPU="${OT_STOP_GRACE_GPU:-N}"` assignment, and `opentr.sh`'s
`: "${OT_STOP_GRACE_GPU:=N}"` prologue default (required so `set -u` survives a checkout
with no `.env` — see `test_shell_env_var_guards.py`). Nothing enforces they stay in sync;
a future edit to only one of them would silently make the shell's *documented* default
diverge from what the container actually gets created with. **The drift guard below is
the most valuable test in this file** — everything else here would still pass with a
container that has no grace period at all if the drift guard did not exist to notice a
mismatched, still-present key.

The compose key is the PRIMARY fix (issue #782, premise P2): compose v2.29.7 bakes
`stop_grace_period` into the container's `Config.StopTimeout` at CREATE time, and the
docker daemon honours it even for a bare `docker stop` — verified live,
`docker inspect opentranscribe-celery-worker --format '{{.Config.StopTimeout}}'` returned
`<nil>` before this change. The shell `-t`/drain wiring (test_teardown_call_sites_drain.py)
is the migration bridge for containers created before the key existed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"
DIAR_NATIVE_YML = REPO_ROOT / "docker-compose.diar-native.yml"
OPENTR_SH = REPO_ROOT / "opentr.sh"
COMMON_SH = REPO_ROOT / "scripts" / "common.sh"

#: The six CUDA-holding services declared in base docker-compose.yml (issue #782 B3).
#: celery-worker (GPU transcription+diarization), celery-cpu-worker (`count: all` GPU
#: reservation), celery-redaction (REDACTION_DEVICE=auto can open CUDA), and the three
#: gpu-split/gpu-scale profile-gated workers. diar-native is EXCLUDED on purpose: it
#: carries its own deliberately SHORTER, non-configurable grace period — see
#: `test_diar_native_has_its_own_short_grace_period` below.
CUDA_HOLDING_SERVICES = frozenset(
    {
        "celery-worker",
        "celery-cpu-worker",
        "celery-redaction",
        "celery-worker-gpu-transcribe",
        "celery-worker-gpu-diarize",
        "celery-worker-gpu-scaled",
    }
)

#: Every compose overlay that could, in principle, redefine a service key for one of the
#: six above. None may set `stop_grace_period` -- the base file is the single source, and
#: a per-overlay override would defeat the drift guard for whichever deployment shape
#: loads that overlay.
OVERLAY_FILES = (
    "docker-compose.override.yml",
    "docker-compose.gpu.yml",
    "docker-compose.blackwell.yml",
    "docker-compose.gpu-scale.yml",
    "docker-compose.gpu-split.yml",
    "docker-compose.prod.yml",
    "docker-compose.local.yml",
    "docker-compose.lite.yml",
    "docker-compose.offline.yml",
    "docker-compose.nas.yml",
    "docker-compose.nginx.yml",
    "docker-compose.pki.yml",
)


def _compose_yaml() -> dict[str, Any]:
    # Annotated rather than widened: yaml.safe_load is untyped, so the result is Any and
    # mypy flags the implicit Any return. Naming the shape here keeps every downstream
    # subscript checked, which `-> Any` would have deleted.
    parsed: dict[str, Any] = yaml.safe_load(COMPOSE_YML.read_text(encoding="utf-8"))
    return parsed


@pytest.mark.unit
def test_every_cuda_holding_service_declares_the_key():
    services = _compose_yaml()["services"]
    missing = sorted(
        name for name in CUDA_HOLDING_SERVICES if "stop_grace_period" not in services.get(name, {})
    )
    assert not missing, (
        f"docker-compose.yml declares no `stop_grace_period` for: {missing} -- these are "
        "CUDA-holding services and will be SIGKILLed 10s after SIGTERM (issue #782)"
    )


@pytest.mark.unit
def test_the_cuda_holding_service_set_matches_the_compose_file():
    """Guard the guard: if a new GPU worker is added to compose and not to
    CUDA_HOLDING_SERVICES above, this fails loudly instead of the coverage check silently
    checking one fewer service than it should."""
    services = _compose_yaml()["services"]
    celery_worker_like = {
        name
        for name in services
        if name.startswith("celery-worker") or name in {"celery-cpu-worker", "celery-redaction"}
    }
    assert celery_worker_like == CUDA_HOLDING_SERVICES, (
        "the celery-worker*/celery-cpu-worker/celery-redaction services in docker-compose.yml "
        f"no longer match CUDA_HOLDING_SERVICES ({sorted(celery_worker_like)} vs "
        f"{sorted(CUDA_HOLDING_SERVICES)}) -- update this file's set (and re-check whether "
        "the new/removed service is actually CUDA-holding) before trusting the coverage test"
    )


@pytest.mark.unit
def test_no_overlay_redefines_the_key():
    checked_services = 0
    for overlay_name in OVERLAY_FILES:
        overlay_path = REPO_ROOT / overlay_name
        if not overlay_path.exists():
            continue
        doc = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        for service_name, service_def in (doc.get("services") or {}).items():
            checked_services += 1
            assert "stop_grace_period" not in (service_def or {}), (
                f"{overlay_name} redefines stop_grace_period on service '{service_name}' -- "
                "base docker-compose.yml must be the single source, or a deployment shape "
                "loading this overlay silently disagrees with every other shape about the "
                "grace period"
            )
    # Non-emptiness assertion OUTSIDE the loop: without it, every overlay file missing or
    # newly emptied of services would make the loop above run zero times and pass vacuously.
    assert checked_services >= 10, (
        f"only inspected {checked_services} service definitions across {OVERLAY_FILES} -- "
        "a path typo or an emptied overlay would also produce a low number that still "
        "passes the assertion inside the loop"
    )


@pytest.mark.unit
def test_lite_mode_scales_the_grace_period_to_zero_containers():
    """Edge case (issue #782 B3): docker-compose.lite.yml sets replicas: 0 on
    celery-worker/celery-worker-gpu-scaled. The grace period key is inert there BY
    CONSTRUCTION -- it applies at container create time, and lite creates none -- which
    this test pins so a future lite-mode change can't silently reintroduce a live
    CUDA-holding container with no grace period."""
    lite_path = REPO_ROOT / "docker-compose.lite.yml"
    doc = yaml.safe_load(lite_path.read_text(encoding="utf-8"))
    services = doc["services"]
    for name in ("celery-worker", "celery-worker-gpu-scaled"):
        assert name in services, f"expected {name} in docker-compose.lite.yml"
        replicas = services[name].get("deploy", {}).get("replicas")
        assert replicas == 0, (
            f"docker-compose.lite.yml no longer scales {name} to 0 replicas -- if it now "
            "creates a real container, that container needs the base file's "
            "stop_grace_period to actually apply, not just be inert"
        )


@pytest.mark.unit
def test_diar_native_has_its_own_short_grace_period():
    """diar-native is deliberately NOT on OT_STOP_GRACE_GPU: it already carries a
    documented upstream teardown crash under `restart: unless-stopped`, and a long grace
    period only lengthens a crash this repo cannot fix. 20s is a fixed value, not tunable
    by the same knob as the six CUDA-holding celery services."""
    doc = yaml.safe_load(DIAR_NATIVE_YML.read_text(encoding="utf-8"))
    diar_native = doc["services"]["diar-native"]
    assert diar_native.get("stop_grace_period") == "20s", (
        f"diar-native's stop_grace_period is {diar_native.get('stop_grace_period')!r}, "
        "expected the fixed '20s' -- see the comment beside restart: unless-stopped for why "
        "it must not be tied to OT_STOP_GRACE_GPU"
    )
    assert "unless-stopped" in str(diar_native.get("restart", "")), (
        "diar-native's restart policy changed -- re-check whether the fixed, shorter grace "
        "period comment is still accurate"
    )


# -----------------------------------------------------------------------------------
# The drift guard: three sources of the SAME default, parsed independently.
# -----------------------------------------------------------------------------------


def _compose_default() -> int:
    """The integer inside every `${OT_STOP_GRACE_GPU:-N}s` in docker-compose.yml."""
    matches = set(
        re.findall(r"\$\{OT_STOP_GRACE_GPU:-(\d+)\}s", COMPOSE_YML.read_text(encoding="utf-8"))
    )
    assert matches, "no `${OT_STOP_GRACE_GPU:-N}s` found in docker-compose.yml at all"
    assert len(matches) == 1, (
        f"docker-compose.yml declares MULTIPLE different defaults for OT_STOP_GRACE_GPU: "
        f"{sorted(matches)} -- every CUDA-holding service must agree"
    )
    return int(next(iter(matches)))


def _common_sh_default() -> int:
    """The integer inside `OT_STOP_GRACE_GPU="${OT_STOP_GRACE_GPU:-N}"` in common.sh."""
    match = re.search(
        r'OT_STOP_GRACE_GPU="\$\{OT_STOP_GRACE_GPU:-(\d+)\}"', COMMON_SH.read_text(encoding="utf-8")
    )
    assert match, "scripts/common.sh does not assign OT_STOP_GRACE_GPU in the expected shape"
    return int(match.group(1))


def _opentr_sh_prologue_default() -> int:
    """The integer inside `: "${OT_STOP_GRACE_GPU:=N}"` in opentr.sh's defaults block."""
    match = re.search(
        r':\s*"\$\{OT_STOP_GRACE_GPU:=(\d+)\}"', OPENTR_SH.read_text(encoding="utf-8")
    )
    assert match, (
        'opentr.sh has no `: "${OT_STOP_GRACE_GPU:=N}"` in its defaults block -- '
        "test_shell_env_var_guards.py's contract requires one"
    )
    return int(match.group(1))


@pytest.mark.unit
def test_the_three_default_sources_agree():
    compose_default = _compose_default()
    common_default = _common_sh_default()
    opentr_default = _opentr_sh_prologue_default()

    assert compose_default == common_default == opentr_default, (
        "OT_STOP_GRACE_GPU's default has drifted between the three sources that declare "
        f"it: docker-compose.yml={compose_default}, scripts/common.sh={common_default}, "
        f"opentr.sh={opentr_default}. A container is created against the compose value; "
        "the shell values are what an operator (and this test suite) believes that value "
        "to be -- a mismatch here means the documentation lies about the deployed behaviour."
    )


@pytest.mark.unit
def test_the_measured_placeholder_marker_is_still_present():
    """B2 (the real-hardware measurement) has not landed in this unit yet -- the compose
    comment must still say so, or a reader has no way to know 60s is a guess rather than a
    measured value. Delete this test in the same commit that removes the TODO."""
    text = COMPOSE_YML.read_text(encoding="utf-8")
    assert "TODO(measure #782)" in text, (
        "the TODO(measure #782) placeholder marker is gone from docker-compose.yml but "
        "the grace period is still 60 -- either the value was measured (update this test "
        "and the comment together) or the marker was deleted by mistake"
    )
