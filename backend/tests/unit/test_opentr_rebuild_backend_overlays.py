"""``opentr.sh rebuild-backend`` must not drop the native-diarization overlay.

``docker-compose.diar-native.yml`` is the ONLY file that sets ``DIAR_NATIVE_URL`` on
``celery-worker`` and shares the pipeline_scratch handoff mount with the sidecar
(``pipeline_scratch:/scratch/opentranscribe`` — issue #661 E2 consolidated what used to
be a dedicated ``diar-native-tmp`` volume at ``/tmp/diar-native`` into a namespace,
``diar/``, of that one volume; this test is keyed on the SERVICE/behaviour the overlay
wires, not on which volume name happens to carry it today, precisely because that name
already changed once). ``rebuild-backend`` assembled its own compose chain
(``docker-compose.yml`` + the dev override + GPU + NAS) and never included it, so a
rebuild recreated the worker with the sidecar unreachable at all.

Measured on the live stack (pre-E2 shape, when the missing piece was the WAV mount
rather than the URL): the worker wrote the handoff WAV to its own filesystem, the
sidecar could not see it, and ``/diarize`` answered ::

    HTTP 422  opening /tmp/diar-native/probe.wav: No such file or directory

which the worker classified as a mid-job sidecar failure and answered by falling back
to the in-process PyAnnote fork. Diarization silently degraded — slower, and no
speaker gender — with nothing surfaced to the user beyond one log line. The DEFECT this
test guards (rebuild-backend dropping the overlay) is unchanged by E2; only the volume
name in the reproduction narrative moved.

Same shape as the NAS overlay bug the ``add_nas_overlay`` call site already documents:
a rebuilt container that looks correct and is bound to the wrong storage.

The fix has a second failure mode of its own, which is why the project-name tests below
are as pointed as they are. The first version of the sidecar probe filtered on
``${COMPOSE_PROJECT_NAME:-opentranscribe}``. ``COMPOSE_PROJECT_NAME`` is never exported
globally by ``opentr.sh``, so compose falls back to **its** default — the directory
basename — and on the machine that had the bug that is ``transcribe-app``. The probe
matched 0 containers and the overlay was dropped exactly as before: a fix that was a
no-op in production, with a green suite behind it, because the ``docker ps`` stub
ignored the project filter and answered "yes" to any project at all.

These run the REAL script with ``docker`` and ``nvidia-smi`` stubbed on ``PATH``, so
the whole compose-chain decision is exercised in milliseconds with no daemon, no GPU
and no containers — same convention as ``test_compose_file_selection.py``. The stub
models the daemon's filter semantics rather than approximating them; a stub more
permissive than the thing it stands in for cannot falsify anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)

BASE = "docker-compose.yml"
OVERRIDE = "docker-compose.override.yml"
DIAR = "docker-compose.diar-native.yml"

#: Overlays a dev checkout has on disk. Only their presence matters here — the chain
#: is assembled from filenames, and every service list is passed explicitly.
OVERLAYS = (BASE, OVERRIDE, DIAR, "docker-compose.gpu.yml", "docker-compose.nas.yml")

#: `docker` stand-in. Records every invocation (and the sidecar-relevant environment
#: compose would interpolate) instead of talking to a daemon.
#:
#: ⚠️ `docker ps` HONOURS THE PROJECT FILTER, exactly as the daemon does. The first
#: version of this stub keyed only off the *service* filter and OT_STUB_DIAR_PRESENT,
#: ignoring which project was asked for — so it answered "yes" to any project name at
#: all. That made every rebuild test pass against a probe filtering on a project that
#: does not exist on the real host, and the suite reported GREEN for a fix that was a
#: no-op in production. A stub more permissive than the thing it stands in for cannot
#: falsify anything. It now returns the fake container only when the requested project
#: matches OT_STUB_DIAR_PROJECT.
#:
#: `docker volume ls` stays silent so fix_pipeline_scratch_permissions returns early.
DOCKER_STUB = r"""#!/bin/bash
printf 'ARGV: %s\n' "$*" >> "$OT_DOCKER_LOG"
case "$1" in
  info)
    echo "Runtimes: runc"
    exit 0
    ;;
  compose)
    printf 'ENV: DIAR_NATIVE_IMAGE=%s\n' "${DIAR_NATIVE_IMAGE:-<unset>}" >> "$OT_DOCKER_LOG"
    printf 'ENV: DIAR_NATIVE_MODELS_DIR=%s\n' "${DIAR_NATIVE_MODELS_DIR:-<unset>}" >> "$OT_DOCKER_LOG"
    exit 0
    ;;
  ps)
    if [[ "$*" == *"com.docker.compose.service=diar-native"* ]] \
       && [ "${OT_STUB_DIAR_PRESENT:-0}" = "1" ]; then
      # The project the caller filtered on, i.e. what compose would have to agree with.
      # Assigned to a scalar FIRST on purpose: `${*#pattern}` strips the pattern from
      # each positional parameter individually and rejoins, which yields the wrong
      # token here. `argv` makes the removal operate on the joined string.
      argv="$*"
      requested="${argv#*com.docker.compose.project=}"
      requested="${requested%% *}"
      printf 'PROBE-PROJECT: %s\n' "$requested" >> "$OT_DOCKER_LOG"
      if [ "$requested" = "${OT_STUB_DIAR_PROJECT:-}" ]; then
        echo "deadbeefcafe"
      fi
    fi
    exit 0
    ;;
esac
exit 0
"""

#: No GPU on the test host, whatever the real host has: keeps the chain deterministic
#: (detect_and_configure_hardware takes the CPU branch, so no GPU overlay is added).
NVIDIA_SMI_STUB = "#!/bin/bash\nexit 1\n"


#: Default sandbox directory name. Deliberately neither "opentranscribe" (the wrong
#: fallback that shipped) nor "transcribe-app" (this host's real one): a probe that
#: hardcodes either must fail here, and the only way to pass is to derive it.
CHECKOUT_DIR_NAME = "some-ot-checkout"


def _make_checkout(
    tmp_path: Path, *, diar_weights: bool = False, checkout_name: str = CHECKOUT_DIR_NAME
) -> Path:
    """A directory shaped like a dev checkout, with the real script dropped in it."""
    checkout = tmp_path / checkout_name
    (checkout / "scripts").mkdir(parents=True)
    for name in OVERLAYS:
        (checkout / name).write_text("services: {}\n", encoding="utf-8")
    (checkout / "VERSION").write_text("0.0.0-test\n", encoding="utf-8")
    shutil.copy2(OPENTR, checkout / "opentr.sh")
    (checkout / "opentr.sh").chmod(0o755)
    shutil.copy2(COMMON, checkout / "scripts" / "common.sh")
    if diar_weights:
        weights = checkout / "models" / "diar-native"
        weights.mkdir(parents=True)
        (weights / "segmentation-3.0.onnx").write_text("onnx\n", encoding="utf-8")
    return checkout


def _make_stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)
    smi = bin_dir / "nvidia-smi"
    smi.write_text(NVIDIA_SMI_STUB, encoding="utf-8")
    smi.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    sidecar_deployed: bool = False,
    diar_weights: bool = False,
    pin_models_dir: bool = True,
    checkout_name: str = CHECKOUT_DIR_NAME,
    sidecar_project: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Run ``./opentr.sh <args>`` in a sandbox. Returns (stdout, docker-stub log lines).

    ``sidecar_project`` is the compose project the fake diar-native container belongs
    to. It defaults to the checkout directory name, which is what real compose would
    use with ``COMPOSE_PROJECT_NAME`` unset — so the probe only sees the container if
    it derives the project the same way.
    """
    checkout = _make_checkout(tmp_path, diar_weights=diar_weights, checkout_name=checkout_name)
    bin_dir = _make_stubs(tmp_path)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "OT_DOCKER_LOG": str(log),
        "OT_STUB_DIAR_PRESENT": "1" if sidecar_deployed else "0",
        "OT_STUB_DIAR_PROJECT": sidecar_project if sidecar_project is not None else checkout_name,
        **(extra_env or {}),
    }
    if pin_models_dir:
        # Pinned so resolve_diar_native_models_dir cannot pick up this workstation's
        # legacy sibling-repo export and make the run host-dependent. Turned OFF by
        # the one test that asserts the script resolves the path itself.
        env["DIAR_NATIVE_MODELS_DIR"] = str(checkout / "models" / "diar-native")
    proc = subprocess.run(
        ["./opentr.sh", *args],
        cwd=str(checkout),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"./opentr.sh {' '.join(args)} exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return proc.stdout, log.read_text(encoding="utf-8").splitlines()


def _up_command(docker_log: list[str]) -> str:
    """The single ``compose ... up -d --build --no-deps <services>`` line rebuild runs."""
    ups = [
        line
        for line in docker_log
        if line.startswith("ARGV: compose ") and " up -d --build --no-deps" in line
    ]
    assert len(ups) == 1, f"expected exactly one rebuild `up`, got {ups}"
    return ups[0]


def _chain(command: str) -> list[str]:
    """The compose files, in order, from a ``-f a -f b`` command line."""
    return re.findall(r"-f\s+(\S+)", command)


def _dry_run_chain(stdout: str) -> list[str]:
    """The overlay list `start --dry-run` prints, in order."""
    files: list[str] = []
    collecting = False
    for line in stdout.splitlines():
        if line.strip() == "Compose files:":
            collecting = True
            continue
        if collecting:
            stripped = line.strip()
            if stripped.startswith("- "):
                files.append(stripped[2:])
            else:
                break
    return files


# --------------------------------------------------------------------------- #
# rebuild-backend: the defect
# --------------------------------------------------------------------------- #


def test_rebuild_backend_keeps_the_diar_native_overlay_when_the_sidecar_is_deployed(
    tmp_path: Path,
):
    """The bug. Without the overlay celery-worker loses DIAR_NATIVE_URL (and the shared
    pipeline_scratch/diar/ handoff namespace) entirely — it can no longer reach the sidecar
    at all, let alone hand it a WAV."""
    stdout, docker_log = _run(tmp_path, ["rebuild-backend"], sidecar_deployed=True)
    command = _up_command(docker_log)

    assert DIAR in _chain(command), (
        f"rebuild-backend dropped {DIAR} from a deployment that HAS the sidecar. "
        f"celery-worker comes back with no DIAR_NATIVE_URL set, /diarize is never even "
        f"attempted, and diarization silently falls back to PyAnnote. Chain: "
        f"{_chain(command)}"
    )
    assert "celery-worker" in command, command
    assert "keeping its overlay" in stdout, (
        f"the decision must be announced, like the NAS overlay's: {stdout}"
    )


def test_rebuild_backend_omits_the_diar_native_overlay_with_no_sidecar_deployed(
    tmp_path: Path,
):
    """Control: the fix must not be "always add it".

    Loading the overlay on a deployment that never asked for the sidecar puts a
    diar-native service (and its ~4.1 GB warm ORT arena on a GPU) into the merged
    config of every subsequent plain `docker compose up` in that chain.
    """
    stdout, docker_log = _run(tmp_path, ["rebuild-backend"], sidecar_deployed=False)
    command = _up_command(docker_log)

    assert DIAR not in _chain(command), (
        f"rebuild-backend added {DIAR} to a deployment with no sidecar container: {_chain(command)}"
    )
    assert _chain(command) == [BASE, OVERRIDE], _chain(command)
    assert "keeping its overlay" not in stdout, stdout


def test_rebuild_backend_never_lists_the_sidecar_among_the_services_it_recreates(
    tmp_path: Path,
):
    """Adding the overlay must not start or restart diar-native.

    `up -d --no-deps <explicit services>` is what makes the overlay safe to load:
    diar-native is not in the list, so a rebuild cannot bring one up, and cannot
    burn a fresh ORT warm-up on a sidecar that is already serving.
    """
    _, docker_log = _run(tmp_path, ["rebuild-backend"], sidecar_deployed=True)
    command = _up_command(docker_log)

    services = command.split("--no-deps", 1)[1].split()
    assert "diar-native" not in services, f"rebuild-backend would recreate the sidecar: {services}"
    assert "celery-worker" in services, services
    assert "--no-deps" in command, command


def test_rebuild_backend_pins_the_sidecar_to_the_local_dev_image(tmp_path: Path):
    """DIAR_NATIVE_IMAGE must be exported the same way `start` does it.

    The overlay defaults to the PUBLISHED backend image. On a dev checkout that image
    does not exist locally, so the rebuilt worker and the sidecar would disagree about
    which build they are — the reason `start` exports the local tag at all.
    """
    _, docker_log = _run(tmp_path, ["rebuild-backend"], sidecar_deployed=True)
    assert "ENV: DIAR_NATIVE_IMAGE=opentranscribe-backend:latest" in docker_log, docker_log


def test_rebuild_backend_resolves_the_models_dir_the_overlay_interpolates(tmp_path: Path):
    """The overlay binds ${DIAR_NATIVE_MODELS_DIR} read-only at /models.

    `resolve_diar_native_models_dir` has to run on the rebuild path too, or the
    variable reaches compose unset and the sidecar's weights bind silently moves to
    the overlay's own default. Deliberately does NOT pre-set the variable — that is
    what makes this falsifiable: on a script that never resolves it, the stub sees
    `<unset>`.
    """
    _, docker_log = _run(
        tmp_path,
        ["rebuild-backend"],
        sidecar_deployed=True,
        diar_weights=True,
        pin_models_dir=False,
        extra_env={"MODEL_CACHE_DIR": str(tmp_path / CHECKOUT_DIR_NAME / "models")},
    )
    expected = str(tmp_path / CHECKOUT_DIR_NAME / "models" / "diar-native")
    assert f"ENV: DIAR_NATIVE_MODELS_DIR={expected}" in docker_log, docker_log


def test_no_diar_native_suppresses_the_rebuild_autodetect(tmp_path: Path):
    """The escape hatch, mirroring `start`'s."""
    _, docker_log = _run(tmp_path, ["rebuild-backend", "--no-diar-native"], sidecar_deployed=True)
    assert DIAR not in _chain(_up_command(docker_log))


def test_with_diar_native_forces_the_overlay_when_the_sidecar_is_down(tmp_path: Path):
    """The opposite override: keep the mount even with no container to observe."""
    _, docker_log = _run(
        tmp_path, ["rebuild-backend", "--with-diar-native"], sidecar_deployed=False
    )
    assert DIAR in _chain(_up_command(docker_log))


def _probe_project(docker_log: list[str]) -> str:
    """The compose project the probe actually filtered on."""
    projects = [
        line.split("PROBE-PROJECT: ", 1)[1]
        for line in docker_log
        if line.startswith("PROBE-PROJECT: ")
    ]
    assert projects, f"no diar-native probe was issued at all: {docker_log}"
    return projects[0]


def test_the_probe_resolves_the_project_from_the_checkout_directory(tmp_path: Path):
    """The regression this file exists for, second edition.

    The first fix filtered on `${COMPOSE_PROJECT_NAME:-opentranscribe}`. That variable
    is never exported globally by opentr.sh — only locally inside the fresh-deployment
    helpers — so compose falls back to ITS default, the **directory basename**.
    Measured against the live daemon: the sidecar's project label is `transcribe-app`
    (checkout `/mnt/nvm/repos/transcribe-app`), the probe asked for `opentranscribe`,
    matched 0 containers, and the overlay was dropped. The fix was a no-op on the one
    machine known to have the bug, and the suite was green.

    The sandbox directory is deliberately named neither of those, so a probe that
    hardcodes either value fails here and only a derived one passes.
    """
    _, docker_log = _run(
        tmp_path, ["rebuild-backend"], sidecar_deployed=True, checkout_name="ot-checkout-alpha"
    )
    assert _probe_project(docker_log) == "ot-checkout-alpha", (
        "the probe must resolve the compose project from the checkout directory, the "
        "way compose itself does with COMPOSE_PROJECT_NAME unset"
    )
    assert DIAR in _chain(_up_command(docker_log))


def test_an_explicit_compose_project_name_still_wins(tmp_path: Path):
    """Control for the above: the derivation is a FALLBACK, not a replacement.

    A stack started with COMPOSE_PROJECT_NAME set (every `--fresh` deployment) must be
    probed under that name, not under its directory.
    """
    _, docker_log = _run(
        tmp_path,
        ["rebuild-backend"],
        sidecar_deployed=True,
        checkout_name="ot-checkout-beta",
        sidecar_project="otfresh-demo",
        extra_env={"COMPOSE_PROJECT_NAME": "otfresh-demo"},
    )
    assert _probe_project(docker_log) == "otfresh-demo", (
        "an explicit COMPOSE_PROJECT_NAME must take precedence over the directory name"
    )
    assert DIAR in _chain(_up_command(docker_log))


def test_a_sidecar_belonging_to_another_compose_project_is_not_ours(tmp_path: Path):
    """Negative control: the probe must be scoped, not "any diar-native anywhere".

    This is a real configuration on the development host — a `--fresh` stack's
    `otfresh-demo-diar-native` runs alongside the main stack's. Counting it as ours
    would load the overlay onto a deployment that has no sidecar of its own.
    """
    _, docker_log = _run(
        tmp_path,
        ["rebuild-backend"],
        sidecar_deployed=True,
        checkout_name="ot-checkout-gamma",
        sidecar_project="otfresh-demo",
    )
    assert _probe_project(docker_log) == "ot-checkout-gamma", docker_log
    assert DIAR not in _chain(_up_command(docker_log)), (
        "another project's sidecar was mistaken for this deployment's"
    )


def test_the_sidecar_probe_is_label_scoped_and_state_agnostic(tmp_path: Path):
    """How the sidecar is detected, not just that it is.

    Two properties, both of which have bitten this repo:
      * label-scoped, never a name prefix — this host runs unrelated compose stacks
        whose container names begin `opentranscribe-`, and a prefix grep in
        `opentr.sh stop` once destroyed one of them. The service also declares no
        `container_name`, so a `name=^<project>-diar-native$` filter matches nothing;
      * `-a`, so a `restarting` (crash-looping, `restart: unless-stopped`) or
        temporarily stopped sidecar still counts as deployed.
    """
    _, docker_log = _run(tmp_path, ["rebuild-backend"], sidecar_deployed=True)
    probes = [
        line
        for line in docker_log
        if line.startswith("ARGV: ps ") and "com.docker.compose.service=diar-native" in line
    ]
    assert probes, f"no diar-native probe was issued at all: {docker_log}"
    probe = probes[0]
    assert "label=com.docker.compose.project=" in probe, probe
    assert "--filter name=" not in probe, f"must not match on container name: {probe}"
    assert "ps -a" in probe, f"probe must accept any container state: {probe}"
    assert "status=running" not in probe, probe


def test_the_probe_uses_the_same_project_resolution_as_the_port_preflight():
    """One resolution, two readers — pinned so they cannot drift apart.

    `preflight_ports_or_die` already had the correct expression. Nothing connected the
    two, which is how the probe came to ship a different (wrong) one. Deliberately NOT
    fixed by refactoring both onto a shared helper: nothing in the suite exercises
    `preflight_ports_or_die`, so that edit could not be proven, and it guards every
    `start`. If it is ever extracted, this test is the thing that says so.
    """
    source = OPENTR.read_text(encoding="utf-8")
    resolutions = re.findall(r'"\$\{COMPOSE_PROJECT_NAME:-\$\(basename "\$\(pwd\)"\)\}"', source)
    assert len(resolutions) == 2, (
        f"expected diar_native_container_present and preflight_ports_or_die to use the "
        f"identical project resolution; found {len(resolutions)} occurrences"
    )
    body = source.split("diar_native_container_present() {", 1)[1].split("\n}\n", 1)[0]
    assert 'COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")' in body, body
    assert "opentranscribe" not in body, (
        "the probe must never hardcode a project name — that fallback was the bug"
    )


def test_rebuild_backend_still_honours_the_nas_overlay(tmp_path: Path):
    """Regression guard on the neighbour: the diar block sits next to add_nas_overlay."""
    _, docker_log = _run(
        tmp_path,
        ["rebuild-backend", "--nas"],
        sidecar_deployed=False,
        extra_env={"MINIO_NAS_PATH": str(tmp_path)},
    )
    assert "docker-compose.nas.yml" in _chain(_up_command(docker_log))


# --------------------------------------------------------------------------- #
# start: must be byte-for-byte what it was before the helper was extracted
# --------------------------------------------------------------------------- #

#: The four lines `start` prints when it loads the overlay. Pinned verbatim, because
#: `start` is the path everyone uses and a regression there is worse than the bug
#: being fixed; the helper reuses this block rather than paraphrasing it.
#: The GPU line is conditional now: the reservation moved to
#: docker-compose.diar-native-gpu.yml so the base overlay stays loadable on a CPU-only or
#: --lite host (#660), and it is only appended when the nvidia runtime was detected. These
#: tests stub `docker` without an nvidia runtime, so they take the CPU branch.
#:
#: ⚠️ 2.2 GB, not the 4.1 GB this pinned for a year. Measured with
#: `nvidia-smi --query-compute-apps`: diar-server 0.3.1 holds 2,248 MiB idle where the
#: pre-0.3.1 binary held 4,762 MiB, both under SPEAKRS_LAZY_SESSIONS=1 — so the halving is
#: the binary, not the flag. The old figure was repeated in four places and measured in none.
#:
#: ⚠️ The CPU line no longer says "identical output". That claim was RETRACTED upstream
#: (#679): speaker embeddings ARE bit-identical across devices (max centroid delta 0.0 on
#: every clip tested), but diarization segment boundaries can differ by up to one
#: segmentation frame (0.016875 s) when a posterior lands on the binarisation threshold.
#: The wording matters enough to pin because an operator told "identical output" would be
#: entitled to diff a CPU run against a GPU run and expect a match.
START_BANNER = (
    "🎙️  Adding native diarization sidecar (docker-compose.diar-native.yml)",
    "   diar-server on CPU (no nvidia runtime detected) — slower; embeddings identical, "
    "diarization boundaries may differ by up to 0.016875s (#679).",
    "   Used when engine.diarizer_backend=native (DB) / ENGINE_DIARIZER_BACKEND=native (env);",
    "   without the sidecar that config falls back to the in-process PyAnnote fork.",
)

#: The GPU branch's line, for the test that drives the nvidia path explicitly.
START_BANNER_GPU_LINE = "   diar-server on GPU 0 — ~2.2 GB warm ORT arena while up."

AUTOLOAD_LINE = (
    "🎙️  diar-native sidecar AUTO-LOADED (engine.diarizer_backend defaults to native; "
    "models present). Use --no-diar-native to skip."
)


def test_start_autodetects_the_sidecar_from_config_not_from_a_container(tmp_path: Path):
    """`start`'s predicate is unchanged: engine default + a populated models dir.

    Note `sidecar_deployed=False` — `start` must still load the overlay with no
    container anywhere, which is precisely why it cannot share rebuild's predicate.
    """
    stdout, _ = _run(
        tmp_path, ["start", "dev", "--dry-run"], sidecar_deployed=False, diar_weights=True
    )
    assert _dry_run_chain(stdout) == [BASE, OVERRIDE, DIAR], stdout
    assert AUTOLOAD_LINE in stdout, stdout
    for line in START_BANNER:
        assert line in stdout, f"missing banner line: {line!r}\n{stdout}"


def test_start_does_not_autoload_the_sidecar_without_a_populated_models_dir(
    tmp_path: Path,
):
    """The guard that keeps `up --wait` from failing on a checkout with no export."""
    stdout, _ = _run(
        tmp_path, ["start", "dev", "--dry-run"], sidecar_deployed=False, diar_weights=False
    )
    assert _dry_run_chain(stdout) == [BASE, OVERRIDE], stdout
    assert AUTOLOAD_LINE not in stdout, stdout


def test_start_with_the_explicit_flag_loads_the_overlay_without_a_models_dir(
    tmp_path: Path,
):
    """`--with-diar-native` bypasses the auto-detect entirely, as it always has."""
    stdout, _ = _run(
        tmp_path,
        ["start", "dev", "--with-diar-native", "--dry-run"],
        sidecar_deployed=False,
        diar_weights=False,
    )
    assert _dry_run_chain(stdout) == [BASE, OVERRIDE, DIAR], stdout
    assert AUTOLOAD_LINE not in stdout, "explicit flag must not print the auto-load banner"
    for line in START_BANNER:
        assert line in stdout, f"missing banner line: {line!r}\n{stdout}"


def test_start_no_diar_native_suppresses_the_autoload(tmp_path: Path):
    stdout, _ = _run(
        tmp_path,
        ["start", "dev", "--no-diar-native", "--dry-run"],
        sidecar_deployed=False,
        diar_weights=True,
    )
    assert _dry_run_chain(stdout) == [BASE, OVERRIDE], stdout
    assert AUTOLOAD_LINE not in stdout, stdout


# --------------------------------------------------------------------------- #
# Static: one decision point, not three
# --------------------------------------------------------------------------- #


def test_only_the_shared_helper_appends_the_diar_native_overlay():
    """The whole point of the refactor.

    Before it, `start_app` and `reset_and_init` each carried a verbatim copy of the
    block and `rebuild-backend` carried none — which is how the paths came to
    disagree in the first place. A fourth caller must reuse the helper, not paste it.
    """
    source = OPENTR.read_text(encoding="utf-8")
    appends = [
        line
        for line in source.splitlines()
        if f'COMPOSE_FILES -f {DIAR}"' in line and not line.lstrip().startswith("#")
    ]
    assert len(appends) == 1, (
        f"{DIAR} is appended to COMPOSE_FILES in {len(appends)} places; it must only "
        f"happen inside add_diar_native_overlay: {appends}"
    )
    assert source.count("add_diar_native_overlay start") == 2, (
        "expected exactly the start_app and reset_and_init callers"
    )
    assert source.count("add_diar_native_overlay rebuild") == 1


def test_a_fresh_stack_participates_in_the_start_autodetect():
    """`--fresh` is no longer excluded, and the exclusion must not come back.

    It was excluded while a fresh stack had no route to its own model export: loading
    the overlay there could only produce a sidecar crash-looping on an empty /models.
    Provisioning now runs from the backend's lifespan on every stack including a fresh
    one, so the exclusion would do the opposite of its purpose — it would make the
    fresh-install rehearsal the one deployment shape that never rehearses the sidecar.

    Static rather than executed: `start --fresh` refuses (exit 1) when the main stack
    holds the standard dev ports, so a live-stack-dependent test here would pass or fail
    on what else is running, which is not a measurement.
    """
    source = OPENTR.read_text(encoding="utf-8")
    body = source.split("add_diar_native_overlay() {", 1)[1].split("\n}\n", 1)[0]
    assert '[ -z "${FRESH_FLAG:-}" ]' not in body, (
        "the start-mode predicate excludes fresh deployments again; a --fresh stack "
        "would silently run PyAnnote, which is precisely what it exists to rehearse"
    )
    # The guard that replaced it: nothing can produce the weights without a token, so
    # loading the overlay with neither weights nor token is the crash-loop the old
    # exclusion was really protecting against.
    assert "HUGGINGFACE_TOKEN" in body, (
        "the autoload predicate no longer consults a token, so it can load the overlay "
        "on a stack that has no way to produce the weights"
    )
