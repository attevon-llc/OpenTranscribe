"""``--fresh`` must isolate the native diarizer's model export, not just names/ports/volumes.

Adversarial-audit finding on ``feat/diar-native-e2e``. The contract in the root ``CLAUDE.md``
is that a fresh deployment "runs in its own ``otfresh-<name>`` compose project (separate
containers AND named volumes) and the NAS/bind overlay is never loaded, so the real dataset
can't be touched." Two independent changes on this branch broke half of that for the diar-native
model set:

1. ``add_diar_native_overlay`` no longer excludes ``--fresh`` — deliberately, so a fresh stack
   with a ``HUGGINGFACE_TOKEN`` rehearses the sidecar's own provisioning. That part is correct
   and this file does not touch it.
2. ``docker-compose.yml`` bind-mounts ``${DIAR_NATIVE_MODELS_DIR:-...}`` into ``backend``
   **READ-WRITE**, unconditionally — not gated behind the diar-native overlay at all — because
   the backend is what EXPORTS the model set (``native_provision.py``).

Combined: a ``--fresh`` stack's backend inherited whatever ``DIAR_NATIVE_MODELS_DIR`` resolved to
for the MAIN stack — live, populated, ~462MB, requiring a ``HUGGINGFACE_TOKEN`` to rebuild — and
could re-export over, or corrupt, it. Container names, ports and named volumes were all already
correctly isolated (the sidecar declares neither); the gap was purely this one host bind path,
which is not namespaced by ``COMPOSE_PROJECT_NAME`` the way everything else is.

The fix (see ``fresh_diar_native_models_dir`` / ``fresh_prepare_diar_native_models_dir`` in
``opentr.sh``, and the ``--fresh`` block in ``start_app``): a fresh deployment gets its OWN
export directory under ``.fresh/<name>/diar-native-models``, created and chowned to
``CONTAINER_UID_GID`` before ``compose up`` ever runs (avoiding the same NOT_WRITABLE hazard
``scripts/common.sh``'s ``fix_model_cache_permissions`` was just fixed for on the main path).
The override wins EVEN over an explicit ``DIAR_NATIVE_MODELS_DIR`` in ``.env`` — the same way
fresh mode already forces NAS off regardless of ``.env`` — because an operator-pinned value in
``.env`` is set for the MAIN stack and would otherwise apply just as ambiently to every fresh one.

Part A drives the two new shell functions directly (subprocess bash, real function bodies —
same technique ``test_opentr_diar_native_models_dir_resolution.py`` uses), so a regression in
the actual script fails here, not in a reimplementation. Part B drives the REAL
``./opentr.sh start dev --fresh <name>`` end-to-end in a sandboxed checkout with `docker` /
`nvidia-smi` stubbed on PATH — same convention as ``test_opentr_rebuild_backend_overlays.py``
— which is what actually proves the live-data hazard is closed: it pre-populates a "live"
models directory, runs the real fresh-mode code path, and asserts that directory is left
completely untouched while the isolated one is created and used instead.
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


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


# ---------------------------------------------------------------------------------------------
# Part A: the two new functions in isolation (fast, hermetic, no Docker).
# ---------------------------------------------------------------------------------------------


def _run_fresh_helpers(tmp_path: Path, script: str, *, env: dict[str, str] | None = None) -> str:
    """Run ``script`` with the REAL bodies of the fresh diar-native helpers prepended.

    Includes ``FRESH_OVERLAY_DIR``'s real assignment (extracted, not retyped, so a rename of the
    constant cannot silently desync this test from the source) plus ``fresh_sanitize_name``
    (``fresh_diar_native_models_dir`` calls it) ahead of the two functions under test.
    """
    text = OPENTR.read_text(encoding="utf-8")
    overlay_dir_line = re.search(r'^FRESH_OVERLAY_DIR=".*"$', text, re.MULTILINE)
    assert overlay_dir_line, "FRESH_OVERLAY_DIR assignment moved -- update this test"
    preamble = "\n".join(
        [
            overlay_dir_line.group(0),
            _function_body(text, "fresh_sanitize_name"),
            _function_body(text, "fresh_diar_native_models_dir"),
            _function_body(text, "fresh_prepare_diar_native_models_dir"),
        ]
    )
    full_env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", **(env or {})}
    # Absolute path, resolved from the REAL (unfiltered) environment -- `env=full_env` below
    # replaces the child's PATH entirely, including for locating argv[0] itself, so a test that
    # deliberately narrows PATH (to exclude `docker`) must not also make `bash` unresolvable.
    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(
        [bash, "-c", f"{preamble}\n{script}"],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_fresh_diar_native_models_dir_is_scoped_under_dot_fresh_and_the_deployment_name(
    tmp_path: Path,
):
    out = _run_fresh_helpers(tmp_path, 'fresh_diar_native_models_dir "audit1"')
    assert out.strip() == ".fresh/audit1/diar-native-models"


def test_fresh_diar_native_models_dir_sanitizes_the_name(tmp_path: Path):
    """Must go through the same sanitizer as the compose project name, or a name with
    spaces/uppercase produces a directory that ``fresh_project_name`` can't line up with."""
    out = _run_fresh_helpers(tmp_path, 'fresh_diar_native_models_dir "My Test Run!"')
    assert out.strip() == ".fresh/my-test-run/diar-native-models"


def test_fresh_diar_native_models_dir_differs_between_two_deployments(tmp_path: Path):
    """The isolation is PER-DEPLOYMENT, not just "off the live path once"."""
    out = _run_fresh_helpers(
        tmp_path,
        'fresh_diar_native_models_dir "one"; fresh_diar_native_models_dir "two"',
    )
    one, two = out.strip().splitlines()
    assert one != two
    assert one == ".fresh/one/diar-native-models"
    assert two == ".fresh/two/diar-native-models"


def _path_without_docker(tmp_path: Path) -> str:
    """A PATH with just the coreutils `fresh_prepare_diar_native_models_dir` needs
    (`mkdir`/`stat`/`chown`/`chmod`), with NO `docker` on it -- forces `command -v docker` to
    fail. Built from symlinks rather than filtering real PATH directories: `docker` and the
    coreutils commonly live in the SAME directory (e.g. `/usr/bin` on this distro, with `/bin`
    a symlink to it), so removing "any directory containing docker" would have removed `mkdir`
    and `chown` too.
    """
    bin_dir = tmp_path / "no-docker-bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("mkdir", "stat", "chown", "chmod"):
        real = shutil.which(tool)
        assert real, f"{tool} not found on the real PATH -- cannot build the test sandbox"
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(real)
    return str(bin_dir)


def test_fresh_prepare_diar_native_models_dir_creates_a_missing_directory(tmp_path: Path):
    """Must exist BEFORE `compose up` runs — an absent bind-mount source is auto-created
    root-owned by dockerd, which is the exact NOT_WRITABLE hazard this function exists to avoid.
    Ownership success itself is not asserted: it depends on host privilege (root access is what
    makes `chown` to a different uid succeed), which this hermetic test does not assume either
    way -- only that the directory gets created and the function does not abort the caller even
    when every chown tier fails.
    """
    target = tmp_path / "sub" / "diar-native-models"
    assert not target.exists()
    _run_fresh_helpers(
        tmp_path,
        f'fresh_prepare_diar_native_models_dir "{target}" || true',
        env={"PATH": _path_without_docker(tmp_path)},
    )
    assert target.is_dir()


def test_fresh_prepare_diar_native_models_dir_is_idempotent(tmp_path: Path):
    """A re-up (`start dev --fresh <name>` again) must not fail just because the
    directory already exists from a previous run."""
    target = tmp_path / "diar-native-models"
    target.mkdir()
    (target / "already-here.onnx").write_text("x", encoding="utf-8")
    _run_fresh_helpers(
        tmp_path,
        f'fresh_prepare_diar_native_models_dir "{target}" || true',
        env={"PATH": _path_without_docker(tmp_path)},
    )
    assert (target / "already-here.onnx").exists(), "must not wipe pre-existing content"


# ---------------------------------------------------------------------------------------------
# Part B: the real ``./opentr.sh start dev --fresh`` end-to-end, `docker`/`nvidia-smi` stubbed.
# ---------------------------------------------------------------------------------------------

BASE = "docker-compose.yml"
OVERRIDE = "docker-compose.override.yml"
DIAR = "docker-compose.diar-native.yml"
OVERLAYS = (BASE, OVERRIDE, DIAR, "docker-compose.gpu.yml", "docker-compose.nas.yml")

#: `docker` stand-in. `info` answers with no `nvidia` runtime (belt-and-suspenders with the
#: nvidia-smi stub below, which alone already forces the CPU branch of
#: `detect_and_configure_hardware`). `compose` logs the ARGV and the ENV that would be
#: interpolated into the compose files, then exits 0 -- no real containers, no real daemon.
#: Every other subcommand (`image inspect`, `run`, `volume ls`, `pull`) reaches the fall-through
#: `exit 0`: `ensure_opensearch_models`/`ensure_nltk_corpora` treat that as "nothing downloaded"
#: and degrade with a warning, which is fine -- this test is about DIAR_NATIVE_MODELS_DIR, not
#: about actually provisioning models.
DOCKER_STUB = r"""#!/bin/bash
printf 'ARGV: %s\n' "$*" >> "$OT_DOCKER_LOG"
case "$1" in
  info)
    echo "Runtimes: runc"
    exit 0
    ;;
  compose)
    printf 'ENV: DIAR_NATIVE_MODELS_DIR=%s\n' "${DIAR_NATIVE_MODELS_DIR:-<unset>}" >> "$OT_DOCKER_LOG"
    exit 0
    ;;
esac
exit 0
"""

#: No GPU on the test host, whatever the real host has -- keeps hardware detection
#: deterministic (CPU branch), same convention as the rebuild-backend test.
NVIDIA_SMI_STUB = "#!/bin/bash\nexit 1\n"

CHECKOUT_DIR_NAME = "some-ot-checkout"

#: Sentinel content for the "live" export -- distinctive enough that finding it anywhere it
#: shouldn't be (or NOT finding it where it should still be) is unambiguous.
LIVE_EXPORT_SENTINEL = "LIVE-EXPORT-DO-NOT-TOUCH"


def _make_checkout(
    tmp_path: Path, *, live_models: bool = False, checkout_name: str = CHECKOUT_DIR_NAME
) -> Path:
    """Idempotent on purpose: `test_two_fresh_deployments_get_two_different_isolated_directories`
    calls this twice against the SAME checkout name (two `--fresh` deployments genuinely do
    share one checkout in practice), so re-creating an already-populated checkout must not raise.
    """
    checkout = tmp_path / checkout_name
    (checkout / "scripts").mkdir(parents=True, exist_ok=True)
    for name in OVERLAYS:
        (checkout / name).write_text("services: {}\n", encoding="utf-8")
    (checkout / "VERSION").write_text("0.0.0-test\n", encoding="utf-8")
    shutil.copy2(OPENTR, checkout / "opentr.sh")
    (checkout / "opentr.sh").chmod(0o755)
    shutil.copy2(COMMON, checkout / "scripts" / "common.sh")
    if live_models:
        # The STANDARD path, not the workstation-specific legacy fallback -- populating this
        # is what makes the "populated export already exists" branch of
        # resolve_diar_native_models_dir true regardless of which host runs this test.
        live = checkout / "models" / "diar-native"
        live.mkdir(parents=True, exist_ok=True)
        (live / "segmentation-3.0.onnx").write_text(LIVE_EXPORT_SENTINEL, encoding="utf-8")
    return checkout


def _make_stubs(tmp_path: Path) -> Path:
    # exist_ok: test_two_fresh_deployments_get_two_different_isolated_directories calls this
    # twice against the SAME tmp_path (two --fresh deployments against one checkout).
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)
    smi = bin_dir / "nvidia-smi"
    smi.write_text(NVIDIA_SMI_STUB, encoding="utf-8")
    smi.chmod(0o755)
    return bin_dir


def _free_port_offset() -> int:
    """Return a ``--port-offset`` whose whole port block is currently unbound.

    These tests hardcoded 100/200/300/400. ``opentr.sh`` refuses to start a fresh deployment
    when ANY port it needs is already bound (deliberately — see issue #343), so a hardcoded
    offset makes the test assert something about the machine rather than about the code: it
    fails for any developer, agent or CI runner that happens to have something on those ports.
    Observed three separate ways in one afternoon — another agent's ``--fresh`` stack on 200
    and 400, and unrelated host ``node``/``python`` processes on 5475-5477 for offset 300.

    A fresh stack publishes a contiguous block from the base ports; probing 5173..5195 + offset
    covers it with room to spare. Bind-testing (rather than reading ``ss``) is what actually
    proves availability, and matching ``opentr.sh``'s own check means we cannot disagree with it.
    """
    for offset in range(1200, 4000, 100):
        if all(_port_is_free(base + offset) for base in range(5173, 5196)):
            return offset
    pytest.skip("no free port-offset block available on this host")
    raise AssertionError("unreachable")  # pragma: no cover - satisfies type checkers


def _port_is_free(port: int) -> bool:
    """True when nothing holds ``port`` on any interface."""
    import socket

    # 127.0.0.1, not 0.0.0.0: a --fresh stack publishes to the loopback interface
    # (`127.0.0.1:5376->5432/tcp`), so that is the interface whose availability actually
    # matters. It is also strictly more accurate — a bind here fails when the port is held on
    # either loopback or all-interfaces, so nothing is missed by not binding wildcard.
    # No SO_REUSEADDR: without it a port in TIME_WAIT reads as busy, which is the
    # conservative direction (we simply pick the next offset) rather than reporting a port
    # free that opentr.sh will then refuse.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    live_models: bool = False,
    checkout_name: str = CHECKOUT_DIR_NAME,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, list[str], Path]:
    """Run ``./opentr.sh <args>`` for real in a sandboxed checkout.

    Returns (stdout, docker-stub log lines, checkout path). Deliberately NOT ``--dry-run``: the
    whole point is to prove the directory this fix creates actually gets created, and that the
    live directory is left alone, which a dry run cannot demonstrate either way.
    """
    checkout = _make_checkout(tmp_path, live_models=live_models, checkout_name=checkout_name)
    bin_dir = _make_stubs(tmp_path)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "OT_DOCKER_LOG": str(log),
        **(extra_env or {}),
    }
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
    return proc.stdout, log.read_text(encoding="utf-8").splitlines(), checkout


def _resolved_models_dir(docker_log: list[str]) -> str:
    lines = [line for line in docker_log if line.startswith("ENV: DIAR_NATIVE_MODELS_DIR=")]
    assert lines, f"docker compose was never invoked with DIAR_NATIVE_MODELS_DIR: {docker_log}"
    # Every compose invocation in one `start` run shares the same exported value; take the last.
    return lines[-1].split("=", 1)[1]


def test_fresh_deployment_does_not_resolve_to_a_populated_live_models_directory(tmp_path: Path):
    """The finding, reproduced and closed.

    Before the fix this resolved to ``<checkout>/models/diar-native`` -- the exact directory
    the (simulated) main stack already exported its models into -- and that path would have
    been bind-mounted READ-WRITE into the fresh stack's backend.
    """
    stdout, docker_log, checkout = _run(
        tmp_path,
        ["start", "dev", "--fresh", "audit1", "--port-offset", str(_free_port_offset())],
        live_models=True,
    )
    resolved = _resolved_models_dir(docker_log)
    live = str(checkout / "models" / "diar-native")

    assert resolved != live, (
        f"a --fresh deployment resolved DIAR_NATIVE_MODELS_DIR to the LIVE, populated export "
        f"({live}) -- its backend would mount that directory READ-WRITE and could corrupt it. "
        f"stdout:\n{stdout}"
    )
    assert resolved == ".fresh/audit1/diar-native-models", resolved


def test_fresh_deployment_leaves_the_live_export_directory_completely_untouched(tmp_path: Path):
    """Stronger than comparing paths: prove the live directory's CONTENTS never moved."""
    _, _, checkout = _run(
        tmp_path,
        ["start", "dev", "--fresh", "audit2", "--port-offset", str(_free_port_offset())],
        live_models=True,
    )
    live = checkout / "models" / "diar-native"
    contents = sorted(p.name for p in live.iterdir())
    assert contents == ["segmentation-3.0.onnx"], (
        f"the live export directory changed during a --fresh start: {contents}"
    )
    assert (live / "segmentation-3.0.onnx").read_text(encoding="utf-8") == LIVE_EXPORT_SENTINEL


def test_fresh_deployment_creates_its_own_isolated_directory(tmp_path: Path):
    """The mechanism, not just the decision: `fresh_prepare_diar_native_models_dir` must
    actually run before `compose up`, or provisioning fails NOT_WRITABLE on first use."""
    _, _, checkout = _run(
        tmp_path,
        ["start", "dev", "--fresh", "audit3", "--port-offset", str(_free_port_offset())],
        live_models=True,
    )
    isolated = checkout / ".fresh" / "audit3" / "diar-native-models"
    assert isolated.is_dir(), f"{isolated} was never created before `compose up`"


def test_fresh_deployment_ignores_an_explicit_env_override_pointed_at_the_live_directory(
    tmp_path: Path,
):
    """Even an operator-pinned DIAR_NATIVE_MODELS_DIR must not leak into a --fresh stack.

    `.env` is the MAIN stack's config; `opentr.sh` sources it once at startup and exports
    whatever it finds into every subsequent command's environment, `--fresh` included. Without
    the override this pin -- set for exactly one reason: to point the MAIN stack at its real
    export -- would apply just as ambiently to every fresh deployment too, reintroducing the
    exact hazard this file exists to close.
    """
    checkout_name = "pinned-checkout"
    live_path = str(tmp_path / checkout_name / "models" / "diar-native")
    _, docker_log, checkout = _run(
        tmp_path,
        ["start", "dev", "--fresh", "audit4", "--port-offset", str(_free_port_offset())],
        live_models=True,
        checkout_name=checkout_name,
        extra_env={"DIAR_NATIVE_MODELS_DIR": live_path},
    )
    resolved = _resolved_models_dir(docker_log)
    assert resolved != live_path, (
        f"a --fresh deployment inherited an explicit DIAR_NATIVE_MODELS_DIR pin ({live_path}) "
        f"instead of redirecting to its own isolated copy"
    )
    assert resolved == ".fresh/audit4/diar-native-models", resolved


def test_two_fresh_deployments_get_two_different_isolated_directories(tmp_path: Path):
    """Isolation is PER-DEPLOYMENT: two --fresh stacks must not share the models directory
    either, or one could still corrupt the export the other is provisioning."""
    _, log_a, checkout = _run(
        tmp_path, ["start", "dev", "--fresh", "team-a", "--port-offset", "500"]
    )
    _, log_b, _ = _run(
        tmp_path,
        ["start", "dev", "--fresh", "team-b", "--port-offset", "600"],
        checkout_name=checkout.name,
    )
    resolved_a = _resolved_models_dir(log_a)
    resolved_b = _resolved_models_dir(log_b)
    assert resolved_a != resolved_b, (resolved_a, resolved_b)
    assert resolved_a == ".fresh/team-a/diar-native-models"
    assert resolved_b == ".fresh/team-b/diar-native-models"


#: Deliberately far from anything a real dev stack (or this suite's own --fresh port offsets,
#: which top out around 5183+600) would ever publish, and env-var driven the same way
#: `fresh_apply_port_offset` moves them -- `preflight_ports_or_die` reads `${!var:-default}`,
#: so exporting these sidesteps the LIVE dev stack's bound 5173-5183 ports without needing
#: `--fresh` at all, which is the point: this test is proving the NON-fresh path.
_NON_FRESH_TEST_PORTS = {
    "FRONTEND_PORT": "25173",
    "BACKEND_PORT": "25174",
    "FLOWER_PORT": "25175",
    "POSTGRES_PORT": "25176",
    "REDIS_PORT": "25177",
    "MINIO_PORT": "25178",
    "MINIO_CONSOLE_PORT": "25179",
    "OPENSEARCH_PORT": "25180",
    "OPENSEARCH_ADMIN_PORT": "25181",
    "DOCS_PORT": "25183",
}


def test_non_fresh_start_is_unaffected_by_the_fresh_only_override(tmp_path: Path):
    """Control, mirroring the pinned resolution test's own control: the ORDINARY path must
    still honour an explicit DIAR_NATIVE_MODELS_DIR exactly as before. This proves the
    redirect lives inside the `--fresh` block and cannot leak into a plain `start`.
    """
    explicit = str(tmp_path / "wherever-the-operator-said")
    _, docker_log, _ = _run(
        tmp_path,
        ["start", "dev"],
        extra_env={"DIAR_NATIVE_MODELS_DIR": explicit, **_NON_FRESH_TEST_PORTS},
    )
    assert _resolved_models_dir(docker_log) == explicit
