"""Issue #660 (B4's compose half): under `--lite --with-diar-native`, the sidecar
must be pinned to the LITE backend image, or `docker-compose.yml + prod + lite +
diar-native` resolves the exact mismatched pair B4 describes:

    backend                  -> opentranscribe-backend-lite:latest
    celery-cpu-worker        -> opentranscribe-backend-lite:latest
    diar-native              -> opentranscribe-backend:latest   <-- THE FULL IMAGE

`docker-compose.diar-native.yml:38` interpolates `${DIAR_NATIVE_IMAGE:-...}`, so a
shell export beats `-f` order — the same mechanism `pin_diar_native_image_for_blackwell`
(`opentranscribe.sh`) already uses for the blackwell tag.

This extracts the real `add_diar_native_overlay` function body (not a
reimplementation) and drives it via subprocess bash, so a regression in the
actual script fails here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _run_raw(tmp_path: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the real `add_diar_native_overlay` body, mode=start, and print the
    resulting DIAR_NATIVE_IMAGE plus the function's own banner. `resolve_diar_native_
    models_dir` and `diar_native_container_present` are stubbed no-ops — this test is
    only about the image-pairing export, not model resolution (covered elsewhere)."""
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "add_diar_native_overlay")
    (tmp_path / "docker-compose.diar-native.yml").write_text("services: {}\n")

    script = (
        "resolve_diar_native_models_dir() { :; }\n"
        "diar_native_container_present() { return 1; }\n"
        f"{body}\n"
        "add_diar_native_overlay start\n"
        'echo "DIAR_NATIVE_IMAGE=${DIAR_NATIVE_IMAGE:-<unset>}"\n'
    )
    full_env = {"PATH": "/usr/bin:/bin", **env}
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )


def _extract_image(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("DIAR_NATIVE_IMAGE="):
            return line.split("=", 1)[1]
    raise AssertionError(f"DIAR_NATIVE_IMAGE line not found in stdout:\n{stdout}")


def _run(tmp_path: Path, *, env: dict[str, str]) -> str:
    return _extract_image(_run_raw(tmp_path, env=env).stdout)


def test_lite_plus_with_diar_native_pins_the_sidecar_to_the_lite_image(tmp_path: Path):
    image = _run(
        tmp_path,
        env={
            "LITE_FLAG": "--lite",
            "WITH_DIAR_NATIVE_FLAG": "--with-diar-native",
            "ENVIRONMENT": "prod",
        },
    )
    assert "-lite" in image, f"expected a lite image, got {image!r}"


def test_control_without_lite_flag_the_pairing_export_does_not_fire(tmp_path: Path):
    """Must-stay-clean: no --lite means no lite-specific pin from this code path.

    Checking only `"-lite" not in image` is not a real control: with
    `add_diar_native_overlay`'s entire body deleted, `DIAR_NATIVE_IMAGE` is never set
    at all (the harness prints `<unset>`), which also contains no `"-lite"` substring
    and would pass for the wrong reason. Require proof the function actually ran —
    its own overlay banner — in addition to the negative image check.
    """
    result = _run_raw(
        tmp_path,
        env={
            "LITE_FLAG": "",
            "WITH_DIAR_NATIVE_FLAG": "--with-diar-native",
            "ENVIRONMENT": "prod",
        },
    )
    assert "Adding native diarization sidecar" in result.stdout, (
        "add_diar_native_overlay's own banner never printed — the function did not "
        "run at all, which would also satisfy the '-lite not in image' check below "
        "for the wrong reason (a deleted function body leaves DIAR_NATIVE_IMAGE unset)"
    )
    image = _extract_image(result.stdout)
    assert "-lite" not in image, f"lite pin fired without --lite: {image!r}"


def test_an_operators_own_override_still_wins(tmp_path: Path):
    """`:-` everywhere: an operator-set DIAR_NATIVE_IMAGE must never be clobbered."""
    image = _run(
        tmp_path,
        env={
            "LITE_FLAG": "--lite",
            "WITH_DIAR_NATIVE_FLAG": "--with-diar-native",
            "ENVIRONMENT": "prod",
            "DIAR_NATIVE_IMAGE": "myregistry/custom-diar-native:pinned",
        },
    )
    assert image == "myregistry/custom-diar-native:pinned"


def test_lite_pin_prefers_backend_lite_image_override(tmp_path: Path):
    """BACKEND_LITE_IMAGE (the operator's own lite tag) must be honoured before the
    hardcoded davidamacey default."""
    image = _run(
        tmp_path,
        env={
            "LITE_FLAG": "--lite",
            "WITH_DIAR_NATIVE_FLAG": "--with-diar-native",
            "ENVIRONMENT": "prod",
            "BACKEND_LITE_IMAGE": "myregistry/opentranscribe-backend-lite:custom",
        },
    )
    assert image == "myregistry/opentranscribe-backend-lite:custom"
