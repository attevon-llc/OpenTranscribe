"""`stop_grace_period` must exist on every CUDA-holding service, and every source that
carries the default value must agree with the others (issue #782).

**Five files DECLARE the number, and nothing else may spell it.** The declarations are
`docker-compose.yml`'s `${OT_STOP_GRACE_GPU:-Ns}` compose default, `scripts/common.sh`'s
`OT_STOP_GRACE_GPU="${OT_STOP_GRACE_GPU:-N}"`, `opentr.sh`'s `: "${OT_STOP_GRACE_GPU:=N}"`
prologue default (required so `set -u` survives a checkout with no `.env` — see
`test_shell_env_var_guards.py`), and the standalone fallback copies in `opentranscribe.sh`
and `scripts/uninstall-offline-package.sh`, neither of which can rely on `common.sh` being
present. Every *use* site is a bare `"$OT_STOP_GRACE_GPU"` backed by one of those five —
one defaulting mechanism, not two.

⚠️ An earlier revision of this branch defaulted at each of the ~11 use sites as well, after
`test_opentr_restart_fresh_scoping.py` failed with `unbound variable` (exit 127). That
diagnosis was wrong in an instructive way: the fault was in **that harness**, which slices
individual functions out of `opentr.sh` and runs them under `set -u` without the prologue
block, so any variable added to those functions would have detonated identically. Fixing
the harness closed the whole class; defaulting at every use site would only have papered
over this one variable while creating eleven copies of a magic number for a bespoke test to
police.

Two guards below, because they are two different claims:

* `test_every_default_source_agrees` — the five declarations hold the same integer.
* `test_every_shell_literal_agrees_with_the_compose_default` — and *no other* literal has
  appeared anywhere in those four shell files. That is what keeps the "one mechanism"
  property true rather than merely intended.

Nothing else enforces this; an edit to only some would silently make the shell's
*documented* default diverge from what the container is actually created with. **These two
are the most valuable tests in this file** — everything else here would still pass against
a container with no grace period at all, if the drift guards did not exist to notice a
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


def _standalone_fallback_defaults() -> dict[str, int]:
    """The integer in each standalone script's own fallback copy of the default.

    `opentranscribe.sh` (the shipped production front end) and
    `scripts/uninstall-offline-package.sh` both carry their OWN copy, because neither can
    rely on `scripts/common.sh` being present -- a curl install predating the manifest
    entry, and a standalone uninstaller respectively. That makes them real drift surfaces,
    and they were missed by the first version of this guard: the measured value was applied
    to three sources and these two silently kept the placeholder, which is exactly the
    failure the guard exists to prevent, one file over.

    The two use DIFFERENT shapes -- `: "${OT_STOP_GRACE_GPU:=30}"` and
    `OT_STOP_GRACE_GPU="${OT_STOP_GRACE_GPU:-30}"` -- so this matches the brace expansion
    itself rather than either assignment form, and reads only NON-COMMENT lines: both files
    also *name* the default in a "kept in sync with" comment, and a regex that accepted
    those would happily read the stale number out of the prose while the code said something
    else.
    """
    found: dict[str, int] = {}
    for rel in ("opentranscribe.sh", "scripts/uninstall-offline-package.sh"):
        path = REPO_ROOT / rel
        code = "\n".join(
            line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
        )
        values = set(re.findall(r"\$\{OT_STOP_GRACE_GPU:[-=](\d+)\}", code))
        assert values, f"{rel} carries no OT_STOP_GRACE_GPU fallback default outside comments"
        assert len(values) == 1, (
            f"{rel} declares MULTIPLE different OT_STOP_GRACE_GPU defaults {sorted(values)} "
            "-- one file cannot disagree with itself about the value it ships"
        )
        found[rel] = int(next(iter(values)))
    return found


#: Every shell file that names OT_STOP_GRACE_GPU at all -- declaration or use.
SHELL_FILES_USING_THE_KNOB = (
    "opentr.sh",
    "opentranscribe.sh",
    "scripts/common.sh",
    "scripts/uninstall-offline-package.sh",
)


def _every_shell_literal() -> dict[str, set[int]]:
    """EVERY `${OT_STOP_GRACE_GPU:-N}` / `:=N` literal in the shell scripts, per file.

    Today that is exactly the four shell declarations, and the point of scanning for ALL
    of them is to keep it that way: the design is one defaulting mechanism (the prologue /
    file-scope assignment) with bare `"$OT_STOP_GRACE_GPU"` at every use site, so a second
    literal appearing anywhere is either a drift farm starting to grow or a use site that
    has quietly disagreed with its own file's declaration.

    Comments are stripped before matching. Both `opentranscribe.sh` and `common.sh` also
    *name* the default in a "kept in sync with" comment, and a scan that accepted those
    would read a stale number out of prose while the code said something else.
    """
    found: dict[str, set[int]] = {}
    for rel in SHELL_FILES_USING_THE_KNOB:
        path = REPO_ROOT / rel
        code = "\n".join(
            line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
        )
        values = {int(v) for v in re.findall(r"\$\{OT_STOP_GRACE_GPU:[-=](\d+)\}", code)}
        assert values, f"{rel} names OT_STOP_GRACE_GPU nowhere outside comments"
        found[rel] = values
    return found


@pytest.mark.unit
def test_every_default_source_agrees():
    sources = {
        "docker-compose.yml": _compose_default(),
        "scripts/common.sh": _common_sh_default(),
        "opentr.sh": _opentr_sh_prologue_default(),
        **_standalone_fallback_defaults(),
    }

    assert len(set(sources.values())) == 1, (
        "OT_STOP_GRACE_GPU's default has drifted between the sources that declare it: "
        f"{sources}. A container is created against the compose value; the shell values "
        "are what an operator (and this test suite) believes that value to be -- a "
        "mismatch means the documentation lies about the deployed behaviour."
    )


@pytest.mark.unit
def test_the_compose_comment_carries_the_measurement_not_a_guess():
    """The successor to this file's `TODO(measure #782)` placeholder test.

    While the value was a guess, the marker's PRESENCE was the assertion -- a reader had to
    be able to tell 60s was unmeasured. Now that B2 has run, the assertion inverts: the
    marker must be gone AND the comment must carry the evidence. Deleting the old test
    without adding this one would have left the number with no guard at all, so a later
    edit could put back an unmeasured value and nothing would notice.
    """
    text = COMPOSE_YML.read_text(encoding="utf-8")
    assert "TODO(measure #782)" not in text, (
        "the TODO(measure #782) placeholder is back in docker-compose.yml -- if the value "
        "is a guess again, say so here rather than leaving both claims in the tree"
    )
    assert "is MEASURED, not guessed" in text, (
        "docker-compose.yml no longer claims the grace period was measured. Either restore "
        "the measurement table beside stop_grace_period, or re-add the TODO marker and the "
        "test that asserted it -- an unlabelled number is the state issue #782 started in"
    )
    assert "handler release" in text, (
        "the per-rep measurement table is gone from docker-compose.yml's grace-period "
        "comment; 'MEASURED' with no numbers beside it is an assertion, not evidence"
    )


@pytest.mark.unit
def test_every_shell_literal_agrees_with_the_compose_default():
    """The stronger half of the drift guard: EVERY literal, not just the declarations.

    `test_every_default_source_agrees` above checks that the five places which *declare*
    the default agree. This checks that nothing *else* spells it -- so the "one defaulting
    mechanism" property is enforced rather than assumed. A `docker stop -t` given its own
    inline fallback, left on the old number while the compose key moved, is silently wrong
    in exactly the deployment the shell path exists to bridge: containers created before
    the compose key existed, which is the upgrade this whole fix is for.
    """
    compose_default = _compose_default()
    per_file = _every_shell_literal()

    disagreeing = {
        rel: sorted(values - {compose_default})
        for rel, values in per_file.items()
        if values - {compose_default}
    }
    assert not disagreeing, (
        f"docker-compose.yml's OT_STOP_GRACE_GPU default is {compose_default}, but these "
        f"shell literals disagree: {disagreeing}. Every `${{OT_STOP_GRACE_GPU:-N}}` in "
        f"{list(SHELL_FILES_USING_THE_KNOB)} must spell the same measured number -- a "
        "`docker stop -t` on a stale value is silently wrong for exactly the pre-upgrade "
        "containers the shell drain exists to cover."
    )

    total = sum(len(v) for v in per_file.values())
    assert total == len(per_file), (
        f"expected one distinct value per file, got {per_file} -- the assertion above "
        "compares against the compose default, so this is the guard that a file has not "
        "quietly grown two DIFFERENT numbers that both happen to match"
    )
