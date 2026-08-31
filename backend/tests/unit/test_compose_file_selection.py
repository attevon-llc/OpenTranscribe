"""`opentranscribe.sh`'s compose-overlay selection, exercised without GPU hardware.

`get_compose_files()` decides which of `docker-compose.gpu.yml`,
`docker-compose.blackwell.yml`, the nginx overlay and the scheduled-backup overlay a
deployment runs. It is the single most consequential branch in the shipped script:
picking the wrong one means the app runs on the wrong image (Blackwell), or on the CPU
when a GPU is present, or without the reverse proxy the operator configured.

Nothing tested it. The release rehearsal that was supposed to
(`scripts/release-tests/`) hand-built its own parallel `-f` list instead, so the whole
layer was dead code at rehearsal time — see
`scripts/release-tests/REHEARSAL_ALIGNMENT_PLAN.md` finding A. These tests are the fast
half of the fix; the rehearsal now drives the real command for the slow half.

They run the REAL script through its `compose-files` arm with `docker` and `nvidia-smi`
stubbed on `PATH`, so every branch — including Blackwell, which no machine here has —
is reachable in milliseconds with no daemon and no card. Same convention as
`test_install_upgrade_scripts.py` (pytest + subprocess + stubs); this repo has no
`.bats` harness and does not need one for this.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGER = REPO_ROOT / "opentranscribe.sh"
MANIFEST = REPO_ROOT / "release-manifest.txt"
RELEASE_TESTS = REPO_ROOT / "scripts" / "release-tests"

pytestmark = pytest.mark.skipif(
    not MANAGER.exists(), reason="opentranscribe.sh not present in this checkout"
)

# Every overlay get_compose_files() can put in front of `docker compose`.
BASE = "docker-compose.yml"
PROD = "docker-compose.prod.yml"
GPU = "docker-compose.gpu.yml"
BLACKWELL = "docker-compose.blackwell.yml"
NGINX = "docker-compose.nginx.yml"
BACKUP = "docker-compose.backup.yml"
DIAR = "docker-compose.diar-native.yml"

ALL_OVERLAYS = (BASE, PROD, GPU, BLACKWELL, NGINX, BACKUP, DIAR)


def _make_deployment(
    tmp_path: Path,
    *,
    overlays: tuple[str, ...] = ALL_OVERLAYS,
    env_lines: tuple[str, ...] = (),
    certs: bool = False,
    diar_weights: bool = False,
) -> Path:
    """Lay out a directory shaped like a real curl install and drop the script in it."""
    install = tmp_path / "install"
    install.mkdir(parents=True)
    for name in overlays:
        (install / name).write_text("services: {}\n", encoding="utf-8")
    (install / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    if certs:
        ssl_dir = install / "nginx" / "ssl"
        ssl_dir.mkdir(parents=True)
        (ssl_dir / "server.crt").write_text("cert\n", encoding="utf-8")
        (ssl_dir / "server.key").write_text("key\n", encoding="utf-8")
    if diar_weights:
        # The default DIAR_NATIVE_MODELS_DIR, i.e. ${MODEL_CACHE_DIR:-./models}/diar-native.
        weights = install / "models" / "diar-native"
        weights.mkdir(parents=True)
        (weights / "segmentation-3.0.onnx").write_text("onnx\n", encoding="utf-8")
    (install / "opentranscribe.sh").write_bytes(MANAGER.read_bytes())
    (install / "opentranscribe.sh").chmod(0o755)
    return install


def _make_stubs(tmp_path: Path, *, nvidia_runtime: bool, compute_cap: str) -> Path:
    """`docker` and `nvidia-smi` stand-ins, so every GPU branch is reachable on any host.

    `detect_nvidia_runtime` greps `docker info` for `Runtimes.*nvidia`; `is_blackwell_gpu`
    reads `nvidia-smi --query-gpu=compute_cap`. Nothing else in this arm shells out.
    """
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()

    runtimes = "Runtimes: io.containerd.runc.v2 nvidia runc" if nvidia_runtime else "Runtimes: runc"
    docker = bin_dir / "docker"
    docker.write_text(
        f'#!/bin/bash\nif [ "$1" = "info" ]; then echo " {runtimes}"; exit 0; fi\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    smi = bin_dir / "nvidia-smi"
    if compute_cap:
        smi.write_text(f"#!/bin/bash\necho {compute_cap}\n", encoding="utf-8")
    else:
        # No card at all: the real binary is absent, so the command must fail.
        smi.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    smi.chmod(0o755)
    return bin_dir


def _resolve(
    tmp_path: Path,
    *,
    nvidia_runtime: bool = True,
    compute_cap: str = "8.6",
    overlays: tuple[str, ...] = ALL_OVERLAYS,
    env_lines: tuple[str, ...] = (),
    certs: bool = False,
    diar_weights: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Run `./opentranscribe.sh compose-files` and return (stdout chain, stderr banners)."""
    install = _make_deployment(
        tmp_path,
        overlays=overlays,
        env_lines=env_lines,
        certs=certs,
        diar_weights=diar_weights,
    )
    bin_dir = _make_stubs(tmp_path, nvidia_runtime=nvidia_runtime, compute_cap=compute_cap)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["./opentranscribe.sh", "compose-files"],
        cwd=str(install),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"compose-files exited {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip(), proc.stderr


def _files(chain: str) -> list[str]:
    """The compose files, in order, from a `-f a -f b` chain."""
    return re.findall(r"-f\s+(\S+)", chain)


# --------------------------------------------------------------------------- #
# The GPU branch: the one that has already shipped a silent regression
# --------------------------------------------------------------------------- #


def test_ordinary_nvidia_gpu_selects_the_generic_overlay(tmp_path: Path):
    chain, stderr = _resolve(tmp_path, nvidia_runtime=True, compute_cap="8.6")
    assert _files(chain) == [BASE, PROD, GPU], chain
    assert "GPU acceleration enabled" in stderr, stderr


def test_blackwell_gpu_selects_the_blackwell_overlay_not_the_generic_one(tmp_path: Path):
    """SM_12x (DGX Spark / GB10) needs its own image; the generic one does not work there.

    This is the branch no machine in this project can reach, which is exactly why it
    needs a stub test: the release rehearsal ran on 8.6 hardware and could never have
    exercised it.
    """
    chain, stderr = _resolve(tmp_path, nvidia_runtime=True, compute_cap="12.1")
    assert _files(chain) == [BASE, PROD, BLACKWELL], chain
    assert GPU not in chain, f"generic GPU overlay must NOT be added alongside Blackwell: {chain}"
    assert "Blackwell GPU overlay enabled" in stderr, stderr


def test_blackwell_host_falls_back_silently_when_the_overlay_was_never_downloaded(
    tmp_path: Path,
):
    """The regression release-manifest.txt's header documents, reproduced deliberately.

    `[ -f docker-compose.blackwell.yml ]` turns a missing overlay into a *silent*
    fallback to the generic GPU overlay — i.e. the wrong image, with no error. This
    test pins that behaviour so the next test (the manifest coupling) is visibly the
    only thing standing between a user and it.
    """
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="12.1",
        overlays=(BASE, PROD, GPU, NGINX, BACKUP),
    )
    assert _files(chain) == [BASE, PROD, GPU], (
        f"expected the documented silent fallback to the generic overlay, got: {chain}"
    )


def test_no_nvidia_runtime_selects_no_gpu_overlay(tmp_path: Path):
    chain, stderr = _resolve(tmp_path, nvidia_runtime=False, compute_cap="")
    assert _files(chain) == [BASE, PROD], chain
    assert "GPU acceleration enabled" not in stderr, stderr


def test_force_cpu_mode_in_env_beats_a_present_nvidia_runtime(tmp_path: Path):
    """`setup-opentranscribe.sh --cpu` persists FORCE_CPU_MODE=true and it must WIN.

    Docker advertising an nvidia runtime is necessary but not sufficient for a working
    GPU (WSL2 with the toolkit but no adapter passthrough advertises it anyway), so the
    operator's explicit opt-out is the authoritative signal.
    """
    chain, stderr = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("FORCE_CPU_MODE=true",),
    )
    assert _files(chain) == [BASE, PROD], chain
    assert "CPU-only mode" in stderr, stderr


def test_force_cpu_mode_false_does_not_suppress_the_gpu_overlay(tmp_path: Path):
    """Control for the test above: the installer writes FORCE_CPU_MODE=false on a GPU
    install, so a truthiness bug here would disable the GPU for every such deployment."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("FORCE_CPU_MODE=false",),
    )
    assert _files(chain) == [BASE, PROD, GPU], chain


def test_force_cpu_env_var_overrides_for_a_single_invocation(tmp_path: Path):
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        extra_env={"OPENTRANSCRIBE_FORCE_CPU": "1"},
    )
    assert _files(chain) == [BASE, PROD], chain


# --------------------------------------------------------------------------- #
# nginx and backup overlays
# --------------------------------------------------------------------------- #


def test_nginx_overlay_is_added_only_when_the_certificates_exist(tmp_path: Path):
    chain, stderr = _resolve(
        tmp_path,
        nvidia_runtime=False,
        compute_cap="",
        env_lines=("NGINX_SERVER_NAME=opentranscribe.local",),
        certs=True,
    )
    assert _files(chain) == [BASE, PROD, NGINX], chain
    assert "HTTPS enabled" in stderr, stderr


def test_nginx_server_name_without_certificates_warns_and_stays_http(tmp_path: Path):
    chain, stderr = _resolve(
        tmp_path,
        nvidia_runtime=False,
        compute_cap="",
        env_lines=("NGINX_SERVER_NAME=opentranscribe.local",),
        certs=False,
    )
    assert _files(chain) == [BASE, PROD], chain
    assert "SSL certificates not found" in stderr, stderr
    assert "setup-ssl" in stderr, "the warning must name the command that fixes it"


def test_backup_overlay_needs_its_own_toggle_not_just_a_backup_path(tmp_path: Path):
    """.env.example ships BACKUP_HOST_PATH=./backups SET (issue #616).

    Keying selection off that would enable the overlay for every install — and it sets
    `path.repo` on the opensearch service, so every existing deployment's next `update`
    would force-recreate OpenSearch.
    """
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=False,
        compute_cap="",
        env_lines=("BACKUP_HOST_PATH=./backups",),
    )
    assert _files(chain) == [BASE, PROD], chain

    enabled, stderr = _resolve(
        tmp_path / "opted-in",
        nvidia_runtime=False,
        compute_cap="",
        env_lines=("BACKUP_HOST_PATH=./backups", "BACKUP_OVERLAY_ENABLED=true"),
    )
    assert _files(enabled) == [BASE, PROD, BACKUP], enabled
    assert "Scheduled-backup overlay enabled" in stderr, stderr


# --------------------------------------------------------------------------- #
# The native diarization sidecar (issue #639)
# --------------------------------------------------------------------------- #


def test_diar_native_does_not_start_without_its_exported_weights(tmp_path: Path):
    """.env.example ships ENGINE_DIARIZER_BACKEND=native and several DIAR_NATIVE_*
    variables, so none of those can mean "run the sidecar" — every install has them.

    It also must not start weightless: diar-server exits when it cannot load its models
    and the service is `restart: unless-stopped`, so that combination is an endless
    crash loop that ALSO fails `up --wait` for the entire stack. Falling back to the
    in-process PyAnnote engine is the correct, working outcome here.
    """
    chain, _ = _resolve(
        tmp_path,
        env_lines=(
            "ENGINE_DIARIZER_BACKEND=native",
            "DIAR_NATIVE_URL=http://diar-native:8701",
            "DIAR_NATIVE_GPU=0",
        ),
        diar_weights=False,
    )
    assert DIAR not in _files(chain), chain


def test_diar_native_starts_once_its_weights_have_been_exported(tmp_path: Path):
    """The whole point of #639: a self-hosted deployment must have SOME path to the
    engine its own config claims is the default. Before this, there was none.

    Exporting the weights is the opt-in — that directory is created only by
    `download-models diar-native`, so no extra env var is needed to express intent.
    """
    chain, stderr = _resolve(tmp_path, diar_weights=True)
    assert _files(chain) == [BASE, PROD, GPU, DIAR], chain
    assert "Native diarization sidecar enabled" in stderr, stderr


def test_diar_native_weights_without_the_overlay_file_warns_loudly(tmp_path: Path):
    """The Blackwell lesson (#640) applied to this overlay: a missing compose file must
    not be a silent `[ -f ]` fallthrough when the operator went to the trouble of
    exporting several hundred MB of weights."""
    chain, stderr = _resolve(
        tmp_path,
        overlays=(BASE, PROD, GPU, BLACKWELL, NGINX, BACKUP),  # no DIAR on disk
        diar_weights=True,
    )
    assert DIAR not in _files(chain), chain
    assert "docker-compose.diar-native.yml is missing" in stderr, stderr
    assert "PyAnnote" in stderr, "the operator must be told which engine actually runs"
    assert "update-full" in stderr, "the warning must name the command that fetches it"


# --------------------------------------------------------------------------- #
# Chain shape
# --------------------------------------------------------------------------- #


def test_base_compose_always_comes_first_and_prod_second(tmp_path: Path):
    """Compose merges later `-f` files onto earlier ones. The base file carries every
    service DEFINITION; an overlay merged the other way round produces a project whose
    services have neither an image nor a build context."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("NGINX_SERVER_NAME=x.local", "BACKUP_OVERLAY_ENABLED=true"),
        certs=True,
    )
    assert _files(chain)[0] == BASE, chain
    assert _files(chain)[1] == PROD, chain


def test_a_deployment_without_the_prod_overlay_still_resolves(tmp_path: Path):
    chain, _ = _resolve(tmp_path, nvidia_runtime=False, compute_cap="", overlays=(BASE,))
    assert _files(chain) == [BASE], chain


# --------------------------------------------------------------------------- #
# The rehearsal must drive this logic, not reimplement it
# --------------------------------------------------------------------------- #


def _rehearsal_source(name: str) -> str:
    """A release-test script's code with comment lines stripped (prose is not logic)."""
    path = RELEASE_TESTS / name
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


# Scenario scripts that bring a stack up, and whether they are allowed to hand-build a
# `docker compose -f ... up` chain. An entry may only be True with a written reason —
# same stale-exemption discipline as backend/tests/audit-allowlist.txt.
_HAND_BUILT_BRINGUP_EXEMPTIONS = {
    "test-lite-mode.sh": (
        "docker-compose.lite.yml is NOT in release-manifest.txt and get_compose_files() "
        "has no lite branch, so no shipped command can select it — see "
        "test_lite_mode_is_not_reachable_by_a_shipped_deployment below. The moment that "
        "changes, that test fails and this exemption must be removed."
    ),
}


def _named_compose_files(script: str) -> list[str]:
    """Lines where a scenario names a compose file itself instead of asking the script.

    Deliberately a flat literal scan rather than a `docker compose ... up` regex: the
    chain is built into a bash ARRAY several lines above the command that consumes it
    (`compose_args=(-f docker-compose.yml ...)` … `docker compose "${compose_args[@]}"
    up -d`), so a command-shaped regex matches nothing and silently passes. It also
    catches the `[[ -f docker-compose.gpu.yml ]]` form, which is the same mistake one
    step earlier — the harness second-guessing which overlays a deployment has.
    """
    return [
        line.strip()
        for line in _rehearsal_source(script).splitlines()
        if "-f docker-compose" in line
    ]


@pytest.mark.parametrize("script", ["test-fresh-install.sh", "test-upgrade.sh"])
def test_scenario_scripts_bring_stacks_up_through_the_shipped_script(script: str):
    """A rehearsal that hand-builds its own `-f` chain is not rehearsing anything.

    This is the regression guard for the whole alignment change: it fails the moment a
    scenario goes back to naming compose files itself instead of running
    `./opentranscribe.sh start` / `update`, which is what made every branch above
    unreachable from a release gate in the first place.
    """
    hand_built = _named_compose_files(script)
    assert not hand_built, (
        f"{script} names compose files itself: {hand_built}. Use ./opentranscribe.sh "
        "start|update — it owns overlay selection (opentr.sh stays deliberately "
        "excluded: it is dev-only and not in release-manifest.txt)."
    )
    assert "opentranscribe.sh" in _rehearsal_source(script), (
        f"{script} no longer invokes the shipped management script at all"
    )


def test_the_hand_built_bringup_detector_actually_fires():
    """Must-fire control. A detector that matches nothing reports a clean suite.

    test-lite-mode.sh is the one scenario that legitimately still hand-builds a chain
    (see the exemption table), so it doubles as the live positive case: if this stops
    matching, the detector above has silently stopped detecting. An earlier draft of
    that detector was a `docker compose ... up` regex and matched NOTHING in any of the
    three scripts — including the two that were full of hand-built chains at the time.
    """
    assert "test-lite-mode.sh" in _HAND_BUILT_BRINGUP_EXEMPTIONS
    hand_built = _named_compose_files("test-lite-mode.sh")
    assert hand_built, (
        "the hand-built-bring-up detector matched nothing in test-lite-mode.sh, which "
        "is known to contain several — the detector above is dead and the parametrized "
        "test beside it proves nothing"
    )


def test_lite_mode_is_not_reachable_by_a_shipped_deployment():
    """Finding E: `--lite` is a repo/dev-only shape today, and must be labelled as one.

    README.md advertises "API-Lite Deployment", but `docker-compose.lite.yml` is absent
    from release-manifest.txt (so a curl install never downloads it) and
    get_compose_files() has no lite branch (so nothing could select it if it did). The
    lite image is not published by `scripts/docker-build-push.sh all` either.

    This test is the tripwire on that relabel. If someone makes lite genuinely
    shippable, this fails — and `test-lite-mode.sh`'s "dev-only" header, its exemption
    above, and the docs all have to be revisited in the same change rather than left
    stale.
    """
    manifest = MANIFEST.read_text(encoding="utf-8")
    listed = [
        line.split("\t")[0].strip()
        for line in manifest.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    manager_code = "\n".join(
        line
        for line in MANAGER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    in_manifest = "docker-compose.lite.yml" in listed
    in_selector = "docker-compose.lite.yml" in manager_code
    assert not in_manifest and not in_selector, (
        "docker-compose.lite.yml is now reachable by a shipped deployment "
        f"(manifest={in_manifest}, selector={in_selector}). test-lite-mode.sh and "
        "scripts/release-tests/README.md still describe it as repo/dev-only, and the "
        "release pipeline still does not publish opentranscribe-backend-lite — update "
        "all three in this change, and drop the exemption in "
        "_HAND_BUILT_BRINGUP_EXEMPTIONS."
    )
