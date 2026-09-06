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
#
# ⚠️ This tuple is what `_make_deployment` lays down on disk, and get_compose_files() guards
# every overlay behind a `[ -f ... ]` existence check — so an overlay MISSING here is not a
# smaller fixture, it is a branch this whole module cannot reach. LITE was absent until
# issue #667, which is why nothing here had ever exercised the `DEPLOYMENT_MODE=lite` arm
# despite that being the only backend an arm64 host can install.
BASE = "docker-compose.yml"
PROD = "docker-compose.prod.yml"
GPU = "docker-compose.gpu.yml"
BLACKWELL = "docker-compose.blackwell.yml"
NGINX = "docker-compose.nginx.yml"
BACKUP = "docker-compose.backup.yml"
DIAR = "docker-compose.diar-native.yml"
GPU_SPLIT = "docker-compose.gpu-split.yml"
LITE = "docker-compose.lite.yml"

DIAR_GPU = "docker-compose.diar-native-gpu.yml"
ALL_OVERLAYS = (BASE, PROD, GPU, BLACKWELL, NGINX, BACKUP, DIAR, DIAR_GPU, GPU_SPLIT, LITE)


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


def test_diar_native_does_not_start_with_neither_weights_nor_a_token(tmp_path: Path):
    """.env.example ships ENGINE_DIARIZER_BACKEND=native and several DIAR_NATIVE_*
    variables, so none of those can mean "run the sidecar" — every install has them.

    It also must not start weightless: diar-server exits when it cannot load its models
    and the service is `restart: unless-stopped`, so that combination is an endless
    crash loop that ALSO fails `up --wait` for the entire stack. With no weights AND no
    token, nothing can ever produce them, so falling back to the in-process PyAnnote
    engine is the correct, working outcome.
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


def test_diar_native_starts_on_a_token_alone_before_any_export_exists(tmp_path: Path):
    """A fresh install must converge in ONE start, not two.

    The backend exports the model set from its own lifespan, so gating the overlay on the
    export already existing meant the first start provisioned and the second finally
    noticed. A configured HUGGINGFACE_TOKEN is what makes that export possible, so it
    stands in for the weights until they exist.
    """
    chain, stderr = _resolve(
        tmp_path,
        env_lines=("ENGINE_DIARIZER_BACKEND=native", "HUGGINGFACE_TOKEN=hf_example"),
        diar_weights=False,
    )
    assert DIAR in _files(chain), chain
    assert "provision" in stderr.lower(), stderr


def test_diar_native_starts_once_its_weights_have_been_exported(tmp_path: Path):
    """The whole point of #639: a self-hosted deployment must have SOME path to the
    engine its own config claims is the default. Before this, there was none.
    """
    chain, stderr = _resolve(tmp_path, diar_weights=True)
    assert _files(chain) == [BASE, PROD, GPU, DIAR, DIAR_GPU], chain
    assert "Native diarization sidecar enabled" in stderr, stderr


def test_diar_native_gpu_overlay_is_omitted_without_an_nvidia_runtime(tmp_path: Path):
    """The reservation lives in its own file so the base overlay stays CPU-loadable.

    If both were one file, a GPU-less or --lite host would fail `up` with "could not
    select device driver" (#660). Splitting them only helps if the GPU half is genuinely
    conditional, so this drives the no-nvidia path and asserts the sidecar still starts.
    """
    chain, stderr = _resolve(tmp_path, diar_weights=True, nvidia_runtime=False)
    assert DIAR in _files(chain), "the sidecar must still run without a GPU"
    assert DIAR_GPU not in _files(chain), chain
    assert "CPU" in stderr, stderr


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
# GPU split overlay (issue #708): opentranscribe.sh had ZERO references to
# gpu-split before this — `rg 'gpu-split|GPU_SPLIT' opentranscribe.sh` returned
# nothing, so a self-hosted install had no way to select the overlay even once
# it was added to release-manifest.txt. gpu_split_active() is the single gate,
# keyed on the SAME ENGINE_GPU_SPLIT variable dispatch.py's gpu_split_enabled()
# reads to route work onto the split queues — one operator-facing switch for
# both halves.
# --------------------------------------------------------------------------- #


def test_gpu_split_overlay_is_not_selected_by_default(tmp_path: Path):
    """.env.example ships ENGINE_GPU_SPLIT=false, so a plain install must not load it."""
    chain, _ = _resolve(tmp_path, nvidia_runtime=True, compute_cap="8.6")
    assert GPU_SPLIT not in _files(chain), chain


def test_gpu_split_overlay_needs_the_dedicated_toggle_not_just_device_ids(tmp_path: Path):
    """GPU_TRANSCRIBE_DEVICE_ID / GPU_DIARIZE_DEVICE_ID ship set in .env.example too
    (0 and 1) — keying selection off their presence would enable the overlay for
    every install, the same trap issue #616 already caught for the backup overlay."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("GPU_TRANSCRIBE_DEVICE_ID=0", "GPU_DIARIZE_DEVICE_ID=1"),
    )
    assert GPU_SPLIT not in _files(chain), chain


def test_gpu_split_overlay_loads_when_engine_gpu_split_is_true(tmp_path: Path):
    chain, stderr = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("ENGINE_GPU_SPLIT=true",),
    )
    assert _files(chain) == [BASE, PROD, GPU, GPU_SPLIT], chain
    assert "GPU split overlay enabled" in stderr, stderr


def test_gpu_split_overlay_is_case_insensitive_on_the_toggle(tmp_path: Path):
    """The app-side gpu_split_enabled() normalises case; this script's gate must match
    it exactly, or the two halves of the single switch can disagree."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("ENGINE_GPU_SPLIT=True",),
    )
    assert GPU_SPLIT in _files(chain), chain


def test_gpu_split_overlay_is_skipped_without_an_nvidia_runtime(tmp_path: Path):
    """A GPU-reservation overlay cannot load on a host with no GPU at all."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=False,
        compute_cap="",
        env_lines=("ENGINE_GPU_SPLIT=true",),
    )
    assert GPU_SPLIT not in _files(chain), chain


def test_gpu_split_overlay_is_skipped_under_force_cpu_mode(tmp_path: Path):
    """FORCE_CPU_MODE is the authoritative opt-out even with an nvidia runtime present
    (WSL2 can advertise the runtime with no working adapter passthrough)."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("ENGINE_GPU_SPLIT=true", "FORCE_CPU_MODE=true"),
    )
    assert GPU_SPLIT not in _files(chain), chain


def test_gpu_split_toggle_without_the_overlay_file_falls_back_silently(tmp_path: Path):
    """Same `[ -f ... ]` fallthrough shape as the Blackwell overlay: an install predating
    the manifest entry, or one whose download 404'd, must not crash — it just runs
    without the split (i.e. every job stays on the shared 'gpu' queue)."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        overlays=(BASE, PROD, GPU, NGINX, BACKUP),  # no GPU_SPLIT on disk
        env_lines=("ENGINE_GPU_SPLIT=true",),
    )
    assert _files(chain) == [BASE, PROD, GPU], chain


def test_gpu_split_combines_with_the_native_diarization_sidecar(tmp_path: Path):
    """The two overlays are independent selections; nothing about gpu-split should
    suppress diar-native or vice versa."""
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("ENGINE_GPU_SPLIT=true",),
        diar_weights=True,
    )
    assert _files(chain) == [BASE, PROD, GPU, DIAR, DIAR_GPU, GPU_SPLIT], chain


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
        "ISSUE #680 INVALIDATED THIS ENTRY'S ORIGINAL REASON and it is deliberately "
        "rewritten rather than deleted. It used to read 'no shipped command can build "
        "this chain' — true until #680 added docker-compose.lite.yml to "
        "release-manifest.txt, gave get_compose_files() a lite branch, and made "
        "`docker-build-push.sh all` publish opentranscribe-backend-lite. "
        "test_lite_mode_is_reachable_by_a_shipped_deployment below now asserts all "
        "three, so the old justification is gone. What keeps the exemption is a "
        "different fact: this scenario must pin ONE fixed chain regardless of what the "
        "host has (a GPU here would make the shipped selector add the GPU overlay, and "
        "the whole point is the no-GPU shape), and it is also the live positive case "
        "for test_the_hand_built_bringup_detector_actually_fires — a detector with no "
        "positive case silently stops detecting. Driving it through "
        "`DEPLOYMENT_MODE=lite ./opentranscribe.sh start` with FORCE_CPU_MODE is now "
        "POSSIBLE and is the right follow-up; it needs its own change, not a drive-by. "
        "Since issue #660 the chain also adds docker-compose.diar-native.yml (the "
        "CPU-EP speaker-embedding sidecar)."
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


def test_lite_mode_is_reachable_by_a_shipped_deployment():
    """Issue #680 turned finding E's tripwire around: lite is now genuinely shippable.

    This test used to assert the OPPOSITE — that `--lite` was a repo/dev-only shape —
    and it existed to fail the moment anyone made it real, so the three facts it named
    could not silently go stale. All three changed in one place, so it is now the
    positive invariant on the same three facts:

      1. `docker-compose.lite.yml` is in release-manifest.txt, so a curl install
         downloads it;
      2. `opentranscribe.sh:get_compose_files()` has a branch that can select it;
      3. `scripts/docker-build-push.sh all` builds the lite image, so a release
         publishes `opentranscribe-backend-lite`.

    All three are load-bearing for arm64, where the full/CUDA image publishes no
    manifest at all: `arm64_deployment_preflight()` defaults an arm64 host to
    DEPLOYMENT_MODE=lite, which is a no-op unless every one of them holds. Losing any
    one turns that default into a deployment that resolves nothing.
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
    builder_code = "\n".join(
        line
        for line in (REPO_ROOT / "scripts" / "docker-build-push.sh")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("#")
    )

    in_manifest = "docker-compose.lite.yml" in listed
    in_selector = "docker-compose.lite.yml" in manager_code
    # `all)` must build lite, not merely know the word — build_backend_lite is the
    # function that produces the image, so its presence in the all/auto dispatch is
    # the fact, and a bare `lite` string anywhere in the file is not.
    builds_lite = "build_backend_lite" in builder_code

    assert in_manifest and in_selector and builds_lite, (
        "lite is no longer reachable by a shipped deployment "
        f"(manifest={in_manifest}, selector={in_selector}, built={builds_lite}). "
        "arm64 hosts default to DEPLOYMENT_MODE=lite because the full/CUDA image "
        "publishes no arm64 manifest — with any of these three missing, that default "
        "points at nothing. See scripts/release-tests/test-lite-mode.sh's scope note."
    )


def test_the_lite_overlay_covers_every_service_that_would_pull_the_full_image():
    """A lite deployment must not pull the full/CUDA image for ANY service it starts.

    `flower` was the one that slipped: it has no `profiles:` and no `scale: 0`, so it
    starts on every deployment, and `docker-compose.lite.yml` did not override its
    image. On amd64 that quietly drags the ~4.4 GB CUDA image into a deployment whose
    premise is ~2 GB. On arm64 — where `arm64_deployment_preflight()` now DEFAULTS to
    lite precisely because the full image publishes no arm64 manifest — `docker compose
    up` fails outright with "no matching manifest for linux/arm64", so the branch's
    headline behaviour would not work at all.

    Enumerated from the compose files rather than listed here, so a service added to
    prod.yml later cannot be forgotten. A service is covered if lite.yml overrides its
    `image`, scales it to 0, or the base file puts it behind a `profiles:` gate.
    """
    yaml = pytest.importorskip("yaml")

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    prod = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    lite = yaml.safe_load((REPO_ROOT / "docker-compose.lite.yml").read_text(encoding="utf-8"))

    base_services = base.get("services", {})
    lite_services = lite.get("services", {})

    def _replicas(svc: dict) -> int | None:
        deploy = svc.get("deploy") or {}
        return deploy.get("replicas")

    full_image_services = {
        name
        for name, svc in (prod.get("services") or {}).items()
        if "opentranscribe-backend:" in str((svc or {}).get("image", ""))
    }
    # Must-fire control: if the scan finds nothing, every assertion below is vacuous.
    assert full_image_services, (
        "no service in docker-compose.prod.yml references the full backend image — the "
        "detector matched nothing and this test proves nothing. Did the image reference "
        "change shape (e.g. a new variable) without this scan being updated?"
    )

    uncovered = []
    for name in sorted(full_image_services):
        if (base_services.get(name) or {}).get("profiles"):
            continue  # gpu-scale / gpu-split: not started by a lite deployment
        override = lite_services.get(name) or {}
        if override.get("image") or _replicas(override) == 0:
            continue
        if _replicas(base_services.get(name) or {}) == 0:
            continue
        uncovered.append(name)

    assert not uncovered, (
        f"docker-compose.lite.yml does not cover {uncovered}: these services start on a "
        "lite deployment and would pull davidamacey/opentranscribe-backend (the full "
        "CUDA image). On arm64 that image has no manifest at all, so `up` fails. Give "
        "each one a ${BACKEND_LITE_IMAGE:-...} image override, or scale it to 0."
    )


# ── The shipped lite permutation, driven through the REAL selector (issue #667) ──────────
#
# `scripts/validate-deployments.sh` covers ~20 permutations, but every row of it resolves via
# `./opentr.sh <args> --dry-run` — the DEV launcher. The shipped selector is
# `opentranscribe.sh:get_compose_files()`, keyed on DEPLOYMENT_MODE, and `opentranscribe.sh`
# has no `--dry-run` at all, so that harness structurally cannot reach it. Adding one there
# would mean either giving the production launcher a new user-facing flag (a change to a
# script real users curl, deserving its own review) or re-implementing the chain resolution
# inside the test — the exact "never a re-implementation" anti-pattern verify-install-paths.sh
# documents.
#
# So the permutation lives here instead, where `_resolve` already runs the real
# `get_compose_files()` out of the real script against a fake install directory. Until now
# lite's shipped reachability was asserted only by SUBSTRING (the three facts in
# `test_lite_mode_is_reachable_by_a_shipped_deployment`); these run it.


def test_deployment_mode_lite_actually_selects_the_lite_overlay(tmp_path):
    chain, _ = _resolve(tmp_path, env_lines=("DEPLOYMENT_MODE=lite",))
    assert "docker-compose.lite.yml" in chain, (
        "DEPLOYMENT_MODE=lite did not put the lite overlay in the compose chain, so a shipped "
        f"lite install would run the full CUDA images. Chain was: {chain}"
    )


def test_the_default_deployment_does_not_select_the_lite_overlay(tmp_path):
    """Must-stay-clean control: without it the test above passes on a selector that always
    adds the overlay, which would push every full install onto the CPU-only image."""
    chain, _ = _resolve(tmp_path, env_lines=("DEPLOYMENT_MODE=full",))
    assert "docker-compose.lite.yml" not in chain, (
        f"a full deployment picked up the lite overlay. Chain was: {chain}"
    )


def test_lite_skips_the_gpu_overlay_even_on_a_host_with_the_nvidia_runtime(tmp_path):
    """The lite image carries no CUDA runtime, so a GPU reservation over it can only fail.

    This is the permutation a dev-launcher harness cannot express: `opentr.sh --lite` clears
    the GPU flags itself, whereas the shipped path has to decide from DEPLOYMENT_MODE alone,
    on a host that genuinely reports an nvidia runtime.
    """
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=True,
        compute_cap="8.6",
        env_lines=("DEPLOYMENT_MODE=lite",),
    )
    assert "docker-compose.lite.yml" in chain, f"lite overlay missing from: {chain}"
    assert "docker-compose.gpu.yml" not in chain, (
        "a lite deployment loaded the GPU overlay on an nvidia-runtime host. The lite image "
        f"has no CUDA runtime, so those services cannot start. Chain was: {chain}"
    )


def test_all_overlays_covers_every_overlay_the_selector_can_choose():
    """Guard the FIXTURE, not the script — an omission here deletes a branch silently.

    `get_compose_files()` guards each overlay behind `[ -f <file> ]`, and `_make_deployment`
    only writes the files in `ALL_OVERLAYS`. So an overlay the selector knows about but the
    fixture never creates is unreachable by every test in this module: the branch is skipped,
    no test fails, and the coverage simply is not there. That is exactly what happened to
    `docker-compose.lite.yml` — present in the shipped selector, absent from this tuple, and
    therefore never once exercised here despite lite being the only backend an arm64 host can
    install (issue #667).

    Deriving the expected set from the script means the next overlay added to the selector
    fails HERE, loudly, instead of quietly reducing what this module covers.
    """
    source = MANAGER.read_text(encoding="utf-8")
    # Anchor on the DEFINITION, not the name. `get_compose_files()` also appears inside the
    # prose comments above it, and slicing from the first textual match captured 58 lines of
    # documentation and zero code — which made `referenced` empty and every entry in
    # ALL_OVERLAYS look dead. A guard that resolves to an empty set is the failure mode this
    # whole module exists to catch, so it gets an explicit anchor and the control below.
    marker = "\nget_compose_files() {\n"
    assert marker in source, (
        "opentranscribe.sh no longer defines get_compose_files() at top level; this guard "
        "would silently scan nothing"
    )
    start = source.index(marker)
    end = source.index("\n}", start + len(marker))
    body = source[start:end]
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())

    referenced = set(re.findall(r"docker-compose[a-z0-9.-]*\.yml", body))
    assert len(referenced) >= len(ALL_OVERLAYS), (
        f"only found {sorted(referenced)} inside get_compose_files() — the slice is not "
        f"reaching the function body, so both assertions below are vacuous"
    )
    missing = sorted(referenced - set(ALL_OVERLAYS))
    assert not missing, (
        f"get_compose_files() can select {missing}, but ALL_OVERLAYS does not list them, so "
        f"_make_deployment never writes those files and every test in this module silently "
        f"skips that branch via the `[ -f ... ]` guard. Add them to ALL_OVERLAYS."
    )
    # And the reverse would be a fixture writing files nothing reads — harmless, but it means
    # a renamed overlay leaves a dead constant behind that looks like coverage.
    unused = sorted(set(ALL_OVERLAYS) - referenced)
    assert not unused, (
        f"ALL_OVERLAYS lists {unused}, which get_compose_files() never mentions — either the "
        f"overlay was renamed and this tuple was not updated, or it is dead."
    )
