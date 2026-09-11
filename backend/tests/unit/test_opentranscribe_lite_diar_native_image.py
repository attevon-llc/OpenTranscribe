"""Issue #896: a lite deployment must never resolve DIAR_NATIVE_IMAGE (or any other
image variable in the resolved compose chain) from the CUDA `opentranscribe-backend`
repository.

⚠️ The ORDERING half of #896's remaining gap — `arm64_deployment_preflight` reaching every
case arm below `start`, via a single `apply_deployment_pins()` wrapper — is covered by
`test_opentranscribe_arm64_pin_ordering.py`, not here. This file's second half (below the
original docstring content) covers the parallel fix in `resolve_diar_native_downloader_image`
and `preflight_upgrade_env`: both used to read `DEPLOYMENT_MODE` straight off `.env`, which is
blind to `arm64_deployment_preflight`'s in-process `DEPLOYMENT_MODE=lite` export; they now call
`effective_deployment_mode()`, which sees it.

`docker-compose.diar-native.yml` interpolates `${DIAR_NATIVE_IMAGE:-...}`, and that
default falls through to the FULL/CUDA image
(`davidamacey/opentranscribe-backend:${OT_IMAGE_TAG:-latest}`). Before this fix,
`opentranscribe.sh` (the production/self-hosted installer, as distinct from the dev-only
`opentr.sh`) had no code path that ever exported a lite-specific override for the
RUNNING sidecar service — only `resolve_diar_native_downloader_image()`, used exclusively
by the one-shot `download-models diar-native` container, resolved lite correctly. So an
ordinary lite install (including every arm64 host, since `arm64_deployment_preflight`
defaults there) pulled the full ~15 GB CUDA image for the diarization sidecar: a large,
pointless download on amd64, and an outright failure on arm64 (docker-build-push.sh's
`cuda-arm64` capability is reserved and never built).

This extracts the REAL `pin_diar_native_image_for_lite` / `effective_deployment_mode`
function bodies (not a reimplementation) out of `opentranscribe.sh` and drives them via
subprocess bash against the real `scripts/common.sh` (`read_env_value`), so a regression
in the actual shipped script fails here — the same technique
`test_diar_native_lite_image_pairing.py` uses for `opentr.sh`'s dev-only sibling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTRANSCRIBE = REPO_ROOT / "opentranscribe.sh"
COMMON_SH = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not OPENTRANSCRIBE.exists() or not COMMON_SH.exists(),
    reason="opentranscribe.sh or scripts/common.sh not present in this checkout",
)

CUDA_REPO = "davidamacey/opentranscribe-backend:"
LITE_REPO = "davidamacey/opentranscribe-backend-lite:"


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _run_raw(tmp_path: Path, *, env_lines: list[str]) -> subprocess.CompletedProcess:
    """Run the real pin_diar_native_image_for_lite body against a real .env file.

    `effective_deployment_mode` is extracted too (pin_diar_native_image_for_lite calls
    it), and both are sourced alongside the REAL scripts/common.sh so `read_env_value`
    is the genuine implementation, not a stand-in.
    """
    text = OPENTRANSCRIBE.read_text(encoding="utf-8")
    effective_mode = _function_body(text, "effective_deployment_mode")
    pin_lite = _function_body(text, "pin_diar_native_image_for_lite")

    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    script = (
        f"source '{COMMON_SH}'\n"
        f"{effective_mode}\n"
        f"{pin_lite}\n"
        "pin_diar_native_image_for_lite\n"
        'echo "DIAR_NATIVE_IMAGE=${DIAR_NATIVE_IMAGE:-<unset>}"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )


def _extract_image(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("DIAR_NATIVE_IMAGE="):
            return line.split("=", 1)[1]
    raise AssertionError(f"DIAR_NATIVE_IMAGE line not found in stdout:\n{stdout}")


def _run(tmp_path: Path, *, env_lines: list[str]) -> str:
    return _extract_image(_run_raw(tmp_path, env_lines=env_lines).stdout)


def test_lite_deployment_pins_diar_native_to_the_lite_image(tmp_path: Path):
    image = _run(tmp_path, env_lines=["DEPLOYMENT_MODE=lite"])
    assert image.startswith(LITE_REPO), f"expected the lite repo, got {image!r}"
    assert not image.startswith(CUDA_REPO), (
        f"a lite deployment resolved DIAR_NATIVE_IMAGE from the CUDA repo: {image!r}"
    )


def test_control_a_full_deployment_never_gets_the_lite_pin(tmp_path: Path):
    """Must-stay-clean: DEPLOYMENT_MODE=full must not fire the lite-specific pin."""
    image = _run(tmp_path, env_lines=["DEPLOYMENT_MODE=full"])
    assert image == "<unset>", (
        f"the lite pin fired for a full deployment: {image!r} "
        "(pin_diar_native_image_for_full-equivalent logic belongs elsewhere)"
    )


def test_an_operators_own_diar_native_image_pin_always_wins(tmp_path: Path):
    image = _run(
        tmp_path,
        env_lines=[
            "DEPLOYMENT_MODE=lite",
            "DIAR_NATIVE_IMAGE=myregistry/custom-diar-native:pinned",
        ],
    )
    assert image == "myregistry/custom-diar-native:pinned"


def test_lite_pin_prefers_backend_lite_image_override(tmp_path: Path):
    """BACKEND_LITE_IMAGE (the operator's own lite tag) must be honoured before the
    hardcoded davidamacey default — same precedence opentr.sh's dev script uses."""
    image = _run(
        tmp_path,
        env_lines=[
            "DEPLOYMENT_MODE=lite",
            "BACKEND_LITE_IMAGE=myregistry/opentranscribe-backend-lite:custom",
        ],
    )
    assert image == "myregistry/opentranscribe-backend-lite:custom"


def test_lite_pin_tracks_ot_image_tag_rather_than_hardcoding_latest(tmp_path: Path):
    """The lite pin must resolve the CURRENT OT_IMAGE_TAG, not a value baked in once.

    Unlike pin_diar_native_image_for_blackwell (whose `:blackwell` tag never changes
    across an `update --version`), a lite pin that hardcoded `:latest` — or that
    persisted a resolved `:vX.Y.Z` string to .env once — would silently stop tracking
    every later release, defeating issue #895's whole point for exactly the one
    service that needed the same fix.
    """
    image = _run(
        tmp_path,
        env_lines=["DEPLOYMENT_MODE=lite", "OT_IMAGE_TAG=v0.5.0"],
    )
    assert image == f"{LITE_REPO}v0.5.0", f"expected the pinned release tag, got {image!r}"


def test_pin_is_never_persisted_to_env(tmp_path: Path):
    """Deliberately NOT written to .env, unlike pin_diar_native_image_for_blackwell.

    A written pin would bake in the CURRENT OT_IMAGE_TAG's resolved value, and
    `update --version` only ever rewrites OT_IMAGE_TAG itself — so a persisted
    lite pin would silently stop tracking every later release, defeating issue
    #895's whole point for exactly the one service that needed the same fix.
    """
    _run(tmp_path, env_lines=["DEPLOYMENT_MODE=lite", "OT_IMAGE_TAG=v0.4.1"])
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DIAR_NATIVE_IMAGE" not in env_text, (
        "pin_diar_native_image_for_lite wrote DIAR_NATIVE_IMAGE into .env — that "
        f"bakes in a resolved tag that stops tracking future OT_IMAGE_TAG bumps:\n{env_text}"
    )


def test_pin_tracks_a_later_ot_image_tag_bump_rather_than_a_cached_value(tmp_path: Path):
    """Simulates `update --version`: it only rewrites OT_IMAGE_TAG in .env, so a
    second run against the bumped tag must resolve the NEW value, not a value
    memorised from the first run (there is none — see the no-persistence test above
    — but this proves the observable behaviour end to end)."""
    image_from = _run(tmp_path, env_lines=["DEPLOYMENT_MODE=lite", "OT_IMAGE_TAG=v0.4.1"])
    assert image_from == f"{LITE_REPO}v0.4.1"

    image_to = _run(tmp_path, env_lines=["DEPLOYMENT_MODE=lite", "OT_IMAGE_TAG=v0.5.0"])
    assert image_to == f"{LITE_REPO}v0.5.0"


def test_deployment_mode_lite_case_insensitive(tmp_path: Path):
    """effective_deployment_mode lowercases; the pin must fire on `Lite`/`LITE` too."""
    image = _run(tmp_path, env_lines=["DEPLOYMENT_MODE=LITE"])
    assert image.startswith(LITE_REPO), f"expected the lite repo, got {image!r}"


# --------------------------------------------------------------------------- #
# Issue #896's remaining gap, second half: resolve_diar_native_downloader_image()
# and preflight_upgrade_env() must resolve DEPLOYMENT_MODE through
# effective_deployment_mode() (env var beating .env), not a raw read_env_value — or
# arm64_deployment_preflight's in-process DEPLOYMENT_MODE=lite export is invisible to
# them, on an arm64 host that has not already run `start` to persist it to disk.
# --------------------------------------------------------------------------- #


def _run_downloader_resolution(tmp_path: Path, *, env_lines: list[str], machine: str) -> str:
    """Drive the REAL host_architecture / effective_deployment_mode /
    arm64_deployment_preflight / resolve_diar_native_downloader_image bodies against a
    real .env and a stubbed `uname -m`, sourcing the real scripts/common.sh for
    read_env_value.
    """
    text = OPENTRANSCRIBE.read_text(encoding="utf-8")
    host_arch = _function_body(text, "host_architecture")
    effective_mode = _function_body(text, "effective_deployment_mode")
    preflight = _function_body(text, "arm64_deployment_preflight")
    resolver = _function_body(text, "resolve_diar_native_downloader_image")

    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uname_bin = bin_dir / "uname"
    uname_bin.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "-m" ]; then echo "{machine}"; exit 0; fi\n'
        'exec /usr/bin/uname "$@"\n',
        encoding="utf-8",
    )
    uname_bin.chmod(0o755)

    script = (
        f"source '{COMMON_SH}'\n"
        f"{host_arch}\n"
        f"{effective_mode}\n"
        f"{preflight}\n"
        f"{resolver}\n"
        "arm64_deployment_preflight\n"
        "resolve_diar_native_downloader_image\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_downloader_image_on_arm64_resolves_lite_without_an_explicit_deployment_mode(
    tmp_path: Path,
):
    """Before the fix: an arm64 host with no DEPLOYMENT_MODE on disk (the common case —
    it is only ever written there by a prior `start`) resolved this function against the
    FULL/CUDA repository, pulling the 15.2 GB full image to produce a 484 MB export on a
    host that image cannot even run on.
    """
    image = _run_downloader_resolution(
        tmp_path, env_lines=["OT_IMAGE_TAG=v0.5.0"], machine="aarch64"
    )
    assert image == f"{LITE_REPO}v0.5.0", (
        f"expected an arm64 host with no DEPLOYMENT_MODE set to resolve the lite "
        f"downloader image via arm64_deployment_preflight's in-process export, got {image!r}"
    )


def test_downloader_image_on_amd64_full_still_resolves_the_cuda_image_control(
    tmp_path: Path,
):
    """Must-stay-clean: an amd64 host must still resolve the full/CUDA repository."""
    image = _run_downloader_resolution(
        tmp_path, env_lines=["OT_IMAGE_TAG=v0.5.0"], machine="x86_64"
    )
    assert image == f"{CUDA_REPO}v0.5.0", f"expected the CUDA repo, got {image!r}"


def test_downloader_image_honours_an_explicit_lite_env_on_any_arch(tmp_path: Path):
    """Pre-existing branch (DEPLOYMENT_MODE=lite set explicitly in .env) must stay green
    on an amd64 host too — this is not an arm64-only code path."""
    image = _run_downloader_resolution(
        tmp_path,
        env_lines=["DEPLOYMENT_MODE=lite", "OT_IMAGE_TAG=v0.5.0"],
        machine="x86_64",
    )
    assert image == f"{LITE_REPO}v0.5.0", f"expected the lite repo, got {image!r}"


def test_downloader_image_still_prefers_an_operators_own_env_pin(tmp_path: Path):
    """An operator's own DIAR_NATIVE_IMAGE pin wins over everything, even the arm64
    default — same precedence as pin_diar_native_image_for_lite."""
    image = _run_downloader_resolution(
        tmp_path,
        env_lines=["DIAR_NATIVE_IMAGE=myregistry/custom-diar-native:pinned"],
        machine="aarch64",
    )
    assert image == "myregistry/custom-diar-native:pinned"


def test_download_models_diar_native_runs_the_arm64_preflight_before_resolving():
    """Structural: download_models_diar_native() must call arm64_deployment_preflight
    BEFORE resolve_diar_native_downloader_image, or the resolver's
    effective_deployment_mode() read would run before the export exists."""
    text = OPENTRANSCRIBE.read_text(encoding="utf-8")
    fn = _function_body(text, "download_models_diar_native")
    preflight_idx = fn.index("arm64_deployment_preflight")
    resolver_idx = fn.index("resolve_diar_native_downloader_image")
    assert preflight_idx < resolver_idx, (
        "download_models_diar_native must call arm64_deployment_preflight before "
        f"resolve_diar_native_downloader_image; found body:\n{fn}"
    )


def test_resolve_diar_native_downloader_image_uses_effective_deployment_mode():
    text = OPENTRANSCRIBE.read_text(encoding="utf-8")
    fn = _function_body(text, "resolve_diar_native_downloader_image")
    assert "effective_deployment_mode" in fn, (
        "resolve_diar_native_downloader_image must call effective_deployment_mode() so "
        "it can see arm64_deployment_preflight's in-process DEPLOYMENT_MODE=lite export"
    )
    assert "read_env_value DEPLOYMENT_MODE" not in fn, (
        "a raw read_env_value DEPLOYMENT_MODE call cannot see the in-process export — "
        f"found body:\n{fn}"
    )


def test_preflight_upgrade_env_uses_effective_deployment_mode():
    text = OPENTRANSCRIBE.read_text(encoding="utf-8")
    fn = _function_body(text, "preflight_upgrade_env")
    assert "effective_deployment_mode" in fn, (
        "preflight_upgrade_env must call effective_deployment_mode() so it can see "
        "arm64_deployment_preflight's in-process DEPLOYMENT_MODE=lite export"
    )
    assert "read_env_value DEPLOYMENT_MODE" not in fn, (
        f"a raw read_env_value DEPLOYMENT_MODE call cannot see the in-process export — "
        f"found body:\n{fn}"
    )
