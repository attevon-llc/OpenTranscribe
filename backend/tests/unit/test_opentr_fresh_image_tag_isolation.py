"""``--fresh`` must isolate the docker IMAGE TAG a dev build writes to, not just the
compose project, named volumes, ports and container_names.

Reproduced live twice: ``./opentr.sh start dev --fresh <name>`` builds through
``docker-compose.override.yml``, which hard-coded a bare ``opentranscribe-backend:latest``
(and ``-frontend``/``-docs``) for every dev service. The build can be **implicit** — an
unbuilt ``--with-*`` overlay (e.g. ``--with-diar-native``) triggers one with no ``--build``
flag given. Because the tag string is shared across every compose PROJECT on the host, a
fresh-project build re-tags the SAME ``:latest`` the MAIN dev stack's already-running
containers are pinned to by image ID. Docker resolves running containers to that ID and
they are unaffected, so the hazard is invisible until the next
``./opentr.sh restart-backend`` on the main stack, which then silently picks up the fresh
branch's code. Measured: control image ``30013f376fdb`` -> branch build ``dd96d1584629``
under ``:latest``.

This is the same family the repo already guards for aux overlays
(``test_opentr_fresh_aux_isolation.py``, issue #347) and container name/DB scoping
(``test_opentr_stop_container_scoping.py``, issue #693): a shared namespace that
``--fresh`` claims to isolate but a specific plane of which slips through.

Fix: ``docker-compose.override.yml``'s backend/frontend/docs ``image:`` keys interpolate
``${OT_DEV_IMAGE_TAG:-latest}``; ``opentr.sh``'s ``--fresh`` block exports
``OT_DEV_IMAGE_TAG="$FRESH_PROJECT"`` (e.g. ``otfresh-tagiso``) before any build/up, and the
diar-native dev-default mirrors the same variable so the sidecar it may build stays paired
with the same tag as the workers it serves. The non-fresh path explicitly resets it to
``"latest"`` (not merely leaving it unset) — an ``OT_DEV_IMAGE_TAG`` inherited from a prior
fresh shell session, a CI job, or a wrapper script must not silently carry over into a plain
``start dev`` and point it at a stale fresh tag instead of the shared ``:latest``.
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
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


@pytest.fixture(scope="module")
def opentr_source() -> str:
    return OPENTR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def override_source() -> str:
    return OVERRIDE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Static: no hard-coded shared tag left in the dev override
# --------------------------------------------------------------------------- #


def _image_lines(text: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if re.match(r"\s*image:\s*opentranscribe-", line)
    ]


def test_no_dev_override_image_line_hardcodes_latest(override_source):
    """Every backend/frontend/docs ``image:`` line must key off ``OT_DEV_IMAGE_TAG``.

    A bare ``:latest`` here is exactly the defect: any project's build recreates the
    literal tag every other project's already-running containers are pinned to.
    """
    lines = _image_lines(override_source)
    assert lines, "expected image: lines for backend/frontend/docs in docker-compose.override.yml"
    bad = [line for line in lines if "OT_DEV_IMAGE_TAG" not in line]
    assert not bad, (
        f"these docker-compose.override.yml image lines hard-code a shared tag instead of "
        f"interpolating ${{OT_DEV_IMAGE_TAG:-latest}}: {bad}"
    )


def test_dev_override_has_all_thirteen_backend_image_lines(override_source):
    """Must-fire control: proves the scan actually looks at all of them, not a subset."""
    backend_lines = [
        line for line in _image_lines(override_source) if "opentranscribe-backend:" in line
    ]
    assert len(backend_lines) == 13, (
        f"expected 13 backend image: lines (one per backend-based dev service), "
        f"found {len(backend_lines)}: {backend_lines}"
    )


def test_the_hardcoded_tag_scanner_would_notice_a_regression():
    """Must-fire control for ``_image_lines``/the bad-line filter itself."""
    regressed = "services:\n  backend:\n    image: opentranscribe-backend:latest\n"
    fixed = "services:\n  backend:\n    image: opentranscribe-backend:${OT_DEV_IMAGE_TAG:-latest}\n"

    def bad_lines(text: str) -> list[str]:
        return [line for line in _image_lines(text) if "OT_DEV_IMAGE_TAG" not in line]

    assert bad_lines(regressed) == ["image: opentranscribe-backend:latest"]
    assert bad_lines(fixed) == []


# --------------------------------------------------------------------------- #
# Static: opentr.sh's --fresh block actually exports the var
# --------------------------------------------------------------------------- #


def test_fresh_block_exports_ot_dev_image_tag_pinned_to_the_fresh_project(opentr_source):
    """The export must exist, and must be unconditional (``=`` not ``:-``) — an ambient
    leftover ``OT_DEV_IMAGE_TAG`` from a previous fresh shell session must not survive
    into a different fresh deployment."""
    assert 'export OT_DEV_IMAGE_TAG="$FRESH_PROJECT"' in opentr_source, (
        "opentr.sh's --fresh block must export OT_DEV_IMAGE_TAG=$FRESH_PROJECT so a fresh "
        "build never writes the shared opentranscribe-backend:latest tag"
    )


def test_ot_dev_image_tag_export_sits_in_the_same_block_as_compose_project_name(opentr_source):
    """Anchors the export to the *fresh* code path, not merely present anywhere in the
    file — a stray export elsewhere (e.g. only in rebuild-backend) would not isolate the
    tag for a plain ``start dev --fresh``."""
    anchor = 'export COMPOSE_PROJECT_NAME="$FRESH_PROJECT"'
    idx = opentr_source.index(anchor)
    # The two exports are adjacent lines in the --fresh block (see start_app).
    window = opentr_source[idx : idx + 1200]
    assert 'export OT_DEV_IMAGE_TAG="$FRESH_PROJECT"' in window, (
        "OT_DEV_IMAGE_TAG must be exported alongside COMPOSE_PROJECT_NAME in the --fresh "
        "block, not somewhere unrelated"
    )


def test_diar_native_dev_default_reads_the_same_tag_variable(opentr_source):
    """The diar-native sidecar's dev-default image must track OT_DEV_IMAGE_TAG too, or a
    fresh deployment's implicitly-built sidecar pairs against the MAIN stack's :latest
    while the workers it serves pair against the fresh tag (issue: the diar_native/lite
    pairing bug, same shape, different plane)."""
    match = re.search(
        r'export DIAR_NATIVE_IMAGE="\$\{DIAR_NATIVE_IMAGE:-opentranscribe-backend:(.*)\}"$',
        opentr_source,
        re.MULTILINE,
    )
    assert match, "could not find the dev-mode DIAR_NATIVE_IMAGE default in opentr.sh"
    assert match.group(1) == "${OT_DEV_IMAGE_TAG:-latest}", (
        f"DIAR_NATIVE_IMAGE's dev default must resolve through OT_DEV_IMAGE_TAG, got: "
        f"{match.group(1)!r}"
    )


# --------------------------------------------------------------------------- #
# Dynamic: run the real script, prove the resolved value differs by path
# --------------------------------------------------------------------------- #

#: No GPU on the test host, whatever the real host has.
NVIDIA_SMI_STUB = "#!/bin/bash\nexit 1\n"

#: Enough of `docker` for detect_and_configure_hardware / port-probe helpers to get
#: through --dry-run without touching a daemon. `--dry-run` itself never calls docker,
#: but code upstream of it (hardware detection, `docker info`) does.
DOCKER_STUB = r"""#!/bin/bash
case "$1" in
  info)
    echo "Runtimes: runc"
    exit 0
    ;;
  ps)
    exit 0
    ;;
  volume)
    exit 0
    ;;
  image)
    exit 1
    ;;
esac
exit 0
"""

BASE_OVERLAYS = (
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.gpu.yml",
    "docker-compose.nas.yml",
)


def _make_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    for name in BASE_OVERLAYS:
        (checkout / name).write_text("services: {}\n", encoding="utf-8")
    (checkout / "VERSION").write_text("0.0.0-test\n", encoding="utf-8")
    shutil.copy2(OPENTR, checkout / "opentr.sh")
    (checkout / "opentr.sh").chmod(0o755)
    shutil.copy2(COMMON, checkout / "scripts" / "common.sh")
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


def _dry_run(tmp_path: Path, args: list[str], *, extra_env: dict[str, str] | None = None) -> str:
    checkout = _make_checkout(tmp_path)
    bin_dir = _make_stubs(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
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
    return proc.stdout


def _dry_run_tag(stdout: str) -> str:
    """The FIRST whitespace-delimited token after ``OT_DEV_IMAGE_TAG:`` — the resolved
    tag itself, not the trailing explanatory parenthetical opentr.sh prints after it."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("OT_DEV_IMAGE_TAG:"):
            return stripped.split(":", 1)[1].strip().split()[0]
    raise AssertionError(f"no OT_DEV_IMAGE_TAG line in dry-run output:\n{stdout}")


def test_non_fresh_dry_run_resolves_to_the_shared_latest_tag(tmp_path: Path):
    """Control: the non-fresh path must be UNCHANGED — plain ``latest``, not a project tag."""
    stdout = _dry_run(tmp_path, ["start", "dev", "--dry-run"])
    assert _dry_run_tag(stdout) == "latest"


def test_fresh_dry_run_resolves_to_a_project_scoped_tag(tmp_path: Path):
    """The fix. A fresh deployment named ``tagiso`` must resolve to a tag namespaced
    under its own compose project, never the shared ``latest``.

    ``--port-offset`` is required here because the port-collision guard probes the
    REAL host's ports before --dry-run ever prints anything — this repo's own dev
    stack (or another test's fresh deployment) may hold the standard ports.
    """
    stdout = _dry_run(
        tmp_path, ["start", "dev", "--fresh", "tagiso", "--port-offset", "4100", "--dry-run"]
    )
    tag = _dry_run_tag(stdout)
    assert tag == "otfresh-tagiso", (
        f"expected the fresh deployment's OT_DEV_IMAGE_TAG to be its own compose project "
        f"(otfresh-tagiso), got {tag!r} — a fresh build would write to the shared tag"
    )
    assert tag != "latest"


def test_non_fresh_dry_run_resets_a_stale_inherited_tag_from_a_prior_fresh_session(
    tmp_path: Path,
):
    """The mirrored hazard: OT_DEV_IMAGE_TAG left exported in the INVOKING shell (a
    prior fresh session, a CI job, a wrapper script) must not leak into a plain,
    non-fresh ``start dev`` and make it build/run the shared backend against a stale
    fresh tag instead of ``:latest``. The non-fresh branch must reset it explicitly,
    not merely rely on it being unset — a `${VAR:-latest}`-only read fails this the
    moment anything upstream of opentr.sh has exported the variable.
    """
    stdout = _dry_run(
        tmp_path,
        ["start", "dev", "--dry-run"],
        extra_env={"OT_DEV_IMAGE_TAG": "otfresh-stale"},
    )
    tag = _dry_run_tag(stdout)
    assert tag == "latest", (
        f"a non-fresh start dev must reset an inherited OT_DEV_IMAGE_TAG back to "
        f"'latest', not leave the stale value '{tag}' in place — this is the SAME "
        f"cross-contamination this file exists to prevent, in the opposite direction"
    )


def test_two_different_fresh_names_resolve_to_two_different_tags(tmp_path: Path):
    """Two independent fresh stacks must not collide with each other either."""
    stdout_a = _dry_run(
        tmp_path / "a", ["start", "dev", "--fresh", "alpha", "--port-offset", "4200", "--dry-run"]
    )
    stdout_b = _dry_run(
        tmp_path / "b", ["start", "dev", "--fresh", "bravo", "--port-offset", "4300", "--dry-run"]
    )
    tag_a = _dry_run_tag(stdout_a)
    tag_b = _dry_run_tag(stdout_b)
    assert tag_a == "otfresh-alpha"
    assert tag_b == "otfresh-bravo"
    assert tag_a != tag_b
